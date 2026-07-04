"""scanner/sources/cargurus.py -- CarGurus sweep and VDP backfill."""
import asyncio
import json
import re
from typing import Optional

import requests
from bs4 import BeautifulSoup

from scanner.config import log, S, CARGURUS_URL, _VIN_RE
from scanner.detect import (
    detect_er, detect_511a, detect_color, detect_azure_gray,
    extract_price, extract_miles, parse_history, parse_vdp_history,
    parse_autocheck_panel, _is_trim_excluded, _price_valid, needs_backfill,
    detect_seller_type, _url_hash,
)
from scanner.models import _extract_ld_json, _build_listing
from scanner.browser import _quiet_exception_handler, _pw_fetch


def _cargurus_parse_tiles(tiles: list) -> list:
    """Convert a CarGurus 'tiles' array (from __remixContext or _data API) into listings."""
    results = []
    for tile in tiles:
        if not isinstance(tile, dict):
            continue
        if "LISTING" not in tile.get("type", ""):
            continue
        item = tile.get("data", {})
        if not isinstance(item, dict):
            continue

        onto = item.get("ontologyData") or {}
        year_raw = onto.get("carYear") or ""
        try:
            year = int(year_raw)
        except (ValueError, TypeError):
            continue
        if year < 2023 or year > 2024:
            continue

        title = item.get("listingTitle") or (
            f"{year} {onto.get('makeName','')} {onto.get('modelName','')} {onto.get('trimName','')}".strip()
        )
        if _is_trim_excluded(title):
            continue

        price_data = item.get("priceData") or {}
        price_raw = price_data.get("current")
        price = int(price_raw) if price_raw else None
        if price and price > 65_000:
            continue

        miles_data = item.get("mileageData") or {}
        miles = miles_data.get("value")

        color = detect_color((item.get("exteriorColorData") or {}).get("name") or "")
        vin = item.get("vin") or ""

        seller = item.get("sellerData") or {}
        seller_name = seller.get("serviceProviderName") or ""
        location_str = seller.get("displayLocation") or ""
        location = f"{seller_name} · {location_str}".strip(" ·") if seller_name else location_str or "CarGurus"

        seller_type = "CPO" if item.get("isCpo") else "Dealer"

        listing_id = item.get("id")
        link = f"https://www.cargurus.com/details/{listing_id}" if listing_id else ""
        uid = f"cargurus-{listing_id}" if listing_id else f"cargurus-{_url_hash(link)}"

        er_confirmed, er_note = detect_er(vin, title)
        equip_confirmed, equip_note = detect_511a(title)
        hist_flag, hist_note = parse_history(str(item))
        is_azure = detect_azure_gray(color or "") or detect_azure_gray(title)

        results.append(_build_listing(
            uid=uid, source="cargurus", vin=vin,
            title=title, color=color or "",
            location=location, seller_type=seller_type,
            miles=int(miles) if miles else None, price=price,
            hist_flag=hist_flag, hist_note=hist_note,
            er_confirmed=er_confirmed, er_note=er_note,
            equip_confirmed=equip_confirmed, equip_note=equip_note,
            is_azure=is_azure, link=link,
        ))
    return results


def _cargurus_parse_remix_context(content: str) -> tuple:
    """Extract listings + page count from CarGurus __remixContext JSON (Remix SSR).

    Returns (listings: list, page_count: int).
    """
    m = re.search(r"window\.__remixContext\s*=\s*(\{)", content)
    if not m:
        return [], 1
    start = m.start(1)
    depth = 0
    end = start
    for i, c in enumerate(content[start:start + 1_200_000]):
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = start + i + 1
                break
    if end == start:
        return [], 1
    try:
        ctx = json.loads(content[start:end])
    except (json.JSONDecodeError, ValueError):
        return [], 1

    loader = ctx.get("state", {}).get("loaderData", {})
    route_key = next(
        (k for k in loader if "Cars" in k and "seoPath" in k), None
    )
    if not route_key:
        return [], 1
    search = loader[route_key].get("search", {})
    tiles = search.get("tiles", [])
    page_count = search.get("pageCount", 1)
    return _cargurus_parse_tiles(tiles), int(page_count)


async def _nd_cargurus_sweep(base_url: str) -> list:
    """Fetch all CarGurus pages in one browser session.

    Page 1: full headless load + __remixContext parse.
    Pages 2-N: in-browser fetch() to the Remix _data endpoint
               (inherits session cookies -- avoids 406 from bare requests).
    """
    import nodriver as uc
    browser = None
    all_results: dict = {}
    try:
        browser = await uc.start(
            headless=True,
            browser_args=["--disable-http2", "--no-sandbox"],
            sandbox=False,
        )
        tab = await asyncio.wait_for(browser.get(base_url), timeout=60.0)

        # Wait for pagination to appear (confirms page rendered), fall back to delay
        try:
            await asyncio.wait_for(tab.select('[class*="_pagination_"]'), timeout=20.0)
        except asyncio.TimeoutError:
            await asyncio.sleep(5.0)
        except Exception:
            await asyncio.sleep(5.0)

        content = await tab.get_content()
        if not content or len(content) < 5000:
            log.warning("CarGurus: page 1 returned short/empty content")
            return []

        page1_results, page_count = _cargurus_parse_remix_context(content)
        for r in page1_results:
            all_results[r["id"]] = r
        log.info(
            "CarGurus page 1: %d listings  (total pages: %d, unique so far: %d)",
            len(page1_results), page_count, len(all_results),
        )

        if page_count < 2:
            return list(all_results.values())

        # Pages 2-N via in-browser fetch (uses existing session cookies)
        # The _data Remix endpoint returns JSON for the same route.
        # We fetch as text (not .json()) so we can log the raw body on parse failure.
        data_url_base = base_url + "&_data=routes%2F(%24intl).Cars.%24seoPath"
        for page in range(2, min(page_count + 1, 16)):
            page_url = data_url_base + f"&page={page}"
            win_var = f"_cgPD_{page}"
            await tab.evaluate(
                f"window.{win_var} = null;"
                f"fetch({json.dumps(page_url)}, {{headers: {{'Accept': 'application/json'}}}})"
                f".then(r => r.text())"
                f".then(t => {{ window.{win_var} = t || 'EMPTY'; }})"
                f".catch(e => {{ window.{win_var} = 'ERROR:' + e.message; }});"
            )

            result_str = None
            for _ in range(20):
                await asyncio.sleep(0.5)
                try:
                    val = await asyncio.wait_for(
                        tab.evaluate(f"window.{win_var} || null"),
                        timeout=3.0,
                    )
                except asyncio.TimeoutError:
                    continue
                if val and str(val) not in ("null", "None", ""):
                    result_str = str(val)
                    break

            if not result_str or result_str.startswith("ERROR:"):
                log.debug("CarGurus page %d: in-browser fetch failed: %s -- "
                          "falling back to full page navigation", page, result_str)
                result_str = None  # triggers fallback below

            data = None
            if result_str:
                try:
                    data = json.loads(result_str)
                except (json.JSONDecodeError, ValueError):
                    log.debug(
                        "CarGurus page %d: JSON parse failed (first 500 chars): %s",
                        page, result_str[:500],
                    )
                    data = None  # triggers fallback below

            # Fallback: navigate the tab directly to page N and re-parse __remixContext
            if data is None:
                log.debug("CarGurus page %d: using full-page navigation fallback", page)
                page_nav_url = base_url + f"&page={page}"
                try:
                    tab2 = await asyncio.wait_for(browser.get(page_nav_url), timeout=45.0)
                except asyncio.TimeoutError:
                    log.warning("CarGurus page %d: navigation fallback timed out", page)
                    break
                await asyncio.sleep(4.0)
                fallback_content = await tab2.get_content()
                if not fallback_content or len(fallback_content) < 5000:
                    log.warning("CarGurus page %d: fallback page returned short content", page)
                    break
                fb_results, _ = _cargurus_parse_remix_context(fallback_content)
                if not fb_results:
                    log.warning("CarGurus page %d: fallback found no listings", page)
                    break
                new_count = sum(1 for r in fb_results if r["id"] not in all_results)
                for r in fb_results:
                    all_results[r["id"]] = r
                log.info(
                    "CarGurus page %d (fallback nav): %d listings, %d new  (total: %d)",
                    page, len(fb_results), new_count, len(all_results),
                )
                if new_count == 0:
                    break
                # Restore tab reference for next iteration's in-browser fetch
                tab = tab2
                await asyncio.sleep(0.5)
                continue

            tiles = data.get("search", {}).get("tiles", [])
            page_results = _cargurus_parse_tiles(tiles)
            new_count = sum(1 for r in page_results if r["id"] not in all_results)
            for r in page_results:
                all_results[r["id"]] = r
            log.info(
                "CarGurus page %d: %d listings, %d new  (total unique: %d)",
                page, len(page_results), new_count, len(all_results),
            )
            if not page_results or new_count == 0:
                log.debug("CarGurus: stopping early at page %d (no new results)", page)
                break
            await asyncio.sleep(0.3)

    except asyncio.TimeoutError:
        log.warning("CarGurus: browser navigation timed out")
    except Exception as exc:
        log.warning("CarGurus: unexpected error in sweep: %s", exc)
    finally:
        if browser:
            try:
                await browser.stop()
            except Exception:
                pass

    return list(all_results.values())


def sweep_cargurus() -> list:
    """Fetch all CarGurus 2023-2024 F-150 Lightning listings (all pages)."""
    log.info("CarGurus sweep...")
    loop = asyncio.new_event_loop()
    loop.set_exception_handler(_quiet_exception_handler)
    asyncio.set_event_loop(loop)
    try:
        results = loop.run_until_complete(_nd_cargurus_sweep(CARGURUS_URL))
    except Exception as exc:
        log.warning("CarGurus sweep failed: %s", exc)
        results = []
    finally:
        try:
            pending = asyncio.all_tasks(loop)
            if pending:
                for task in pending:
                    task.cancel()
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        except Exception:
            pass
        loop.close()
    log.info("CarGurus: %d total listings found", len(results))
    return results


# -- CarGurus VDP backfill -----------------------------------------------------

def _parse_cargurus_vdp(url: str) -> dict:
    """Fetch a CarGurus VDP and extract enrichment fields.

    Returns a dict with any subset of: vin, color, miles, price, history_flag,
    history_note, extended_range_confirmed, er_note, equipment_511a_confirmed,
    equip_note, seller_type, location, azure_gray.
    Returns {} on fetch failure.
    """
    result: dict = {}
    content = _pw_fetch(url, wait_ms=4000, headless=False,
                        wait_selector='[data-cg-ft="vdp-listing-price"]')
    if not content:
        log.debug("CG VDP: fetch returned nothing for %s", url)
        return result

    soup = BeautifulSoup(content, "lxml")
    full_text = soup.get_text(" ", strip=True)

    # -- VIN ------------------------------------------------------------------
    ld = _extract_ld_json(soup, types=("Car", "Vehicle", "Product"))
    vin_m = _VIN_RE.search(full_text)
    vin = ld.get("vehicleIdentificationNumber") or (vin_m.group(1) if vin_m else "")
    if vin:
        result["vin"] = vin.upper()

    # -- Price ----------------------------------------------------------------
    price_el = soup.select_one(
        '[data-cg-ft="vdp-listing-price"], '
        '[class*="listingPrice"], '
        '[class*="price-section"] span'
    )
    price_text = price_el.get_text(strip=True) if price_el else ""
    price = extract_price(price_text) or extract_price(full_text[:2000])
    if price and _price_valid(price):
        result["price"] = price

    # -- Miles ----------------------------------------------------------------
    miles_el = soup.select_one('[data-cg-ft="vdp-mileage"], [class*="mileage"]')
    miles = extract_miles(miles_el.get_text() if miles_el else "") or extract_miles(full_text[:3000])
    if miles:
        result["miles"] = miles

    # -- Color ----------------------------------------------------------------
    color_el = soup.select_one('[class*="exteriorColor"], [data-cg-ft*="color"]')
    color_text = color_el.get_text(strip=True) if color_el else ""
    color = detect_color(color_text) or detect_color(full_text[:3000])
    if color:
        result["color"] = color
    if detect_azure_gray(color_text) or detect_azure_gray(full_text[:5000]):
        result["azure_gray"] = True

    # -- ER / 511A detection --------------------------------------------------
    er_confirmed, er_note = detect_er(vin or "", full_text)
    if er_confirmed is not None:
        result["extended_range_confirmed"] = er_confirmed
        result["er_note"] = er_note

    equip_confirmed, equip_note = detect_511a(full_text)
    if equip_confirmed is not None:
        result["equipment_511a_confirmed"] = equip_confirmed
        result["equip_note"] = equip_note

    # -- History --------------------------------------------------------------
    # Look for the AutoCheck panel first (more structured), then fall back to
    # full-page VDP history parse.
    autocheck_el = soup.select_one('[class*="autocheck"], [id*="autocheck"]')
    autocheck_text = autocheck_el.get_text(" ", strip=True) if autocheck_el else ""
    ac_flag, ac_note = parse_autocheck_panel(autocheck_text)
    if ac_flag:
        result["history_flag"] = ac_flag
        result["history_note"] = ac_note
    else:
        hist_flag, hist_note = parse_vdp_history(full_text)
        if hist_flag:
            result["history_flag"] = hist_flag
            result["history_note"] = hist_note

    # -- Seller / location ----------------------------------------------------
    seller_el = soup.select_one('[data-cg-ft="vdp-seller-name"], [class*="sellerName"]')
    loc_el    = soup.select_one('[data-cg-ft="vdp-seller-location"], [class*="sellerLocation"]')
    seller_name = seller_el.get_text(strip=True) if seller_el else ""
    loc_text    = loc_el.get_text(strip=True) if loc_el else ""
    if seller_name or loc_text:
        result["location"] = f"{seller_name} · {loc_text}".strip(" ·") if seller_name else loc_text
    result["seller_type"] = detect_seller_type(full_text)

    return result
