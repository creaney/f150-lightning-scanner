"""scanner/sources/cars.py -- Cars.com sweep and VDP enrichment."""
import asyncio
import concurrent.futures
import json
import random
import re
import time
from typing import Optional

from bs4 import BeautifulSoup

from scanner.config import (
    log, S, CARS_BASE_URL, CARS_KEYWORDS, CARS_BASE_PARAMS,
    _VIN_RE, TODAY, YESTERDAY, SOLD,
    _BAD_TITLES, _ALLOWED_TRIMS,
)
from scanner.detect import (
    detect_er, detect_511a, detect_color, detect_azure_gray,
    extract_price, extract_miles, parse_vdp_history, parse_autocheck_panel,
    _is_trim_excluded, _price_valid, needs_backfill, detect_seller_type,
    _text_first,
)
from scanner.models import _extract_ld_json, _build_listing, _partial_to_listing
from scanner.browser import _pw_fetch, _nd_fetch, _quiet_exception_handler


def _cars_extract_uuid(href: str) -> Optional[str]:
    m = re.search(r"/(?:listing|vehicledetail)/([^/?#]+)", href or "")
    return m.group(1) if m else None


def _cars_fetch_html(keyword: str, page: int = 1) -> Optional[BeautifulSoup]:
    params = dict(CARS_BASE_PARAMS, keyword=keyword)
    if page > 1:
        params["page"] = str(page)
    extra_headers = {
        "Referer":                  "https://www.cars.com/",
        "Connection":               "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest":           "document",
        "Sec-Fetch-Mode":           "navigate",
        "Sec-Fetch-Site":           "same-origin",
        "Sec-Fetch-User":           "?1",
    }
    for attempt in range(2):
        try:
            r = S.get(CARS_BASE_URL, params=params, timeout=60, headers=extra_headers)
            r.raise_for_status()
            return BeautifulSoup(r.text, "lxml")
        except Exception as exc:
            log.warning("cars.com fetch attempt %d failed (kw=%r page=%d): %s",
                        attempt + 1, keyword, page, exc)
            if attempt == 0:
                time.sleep(3)
    return None


def _cars_try_embedded_json(soup: BeautifulSoup) -> Optional[list]:
    """Attempt to pull listing data from any JSON blob embedded in the page."""
    for script in soup.find_all("script"):
        raw = script.string or ""
        if len(raw) < 100:
            continue
        if raw.strip().startswith("{"):
            try:
                data = json.loads(raw)
                for key in ("listings", "searchResults", "vehicles", "data"):
                    val = data.get(key)
                    if isinstance(val, list) and val:
                        return val
                for outer_key in ("initialState", "state", "pageData"):
                    nested = data.get(outer_key)
                    if isinstance(nested, dict):
                        for key in ("listings", "searchResults", "vehicles"):
                            val = nested.get(key)
                            if isinstance(val, list) and val:
                                return val
            except (json.JSONDecodeError, AttributeError):
                pass
        m = re.search(r"window\.__\w+\s*=\s*(\{.+?\});?\s*$", raw, re.S)
        if m:
            try:
                data = json.loads(m.group(1))
                for key in ("listings", "searchResults", "vehicles"):
                    val = data.get(key)
                    if isinstance(val, list) and val:
                        return val
            except (json.JSONDecodeError, AttributeError):
                pass
    return None


def _cars_parse_cards(soup: BeautifulSoup) -> list:
    """Parse listing cards from search results HTML."""
    results = []

    cards = (
        soup.select("[data-listing-id]")
        or soup.select(".vehicle-card")
        or soup.select(".listing-row")
        or soup.select("article[class*='listing']")
        or soup.select("div[class*='vehicle-card']")
    )

    for card in cards:
        uuid = card.get("data-listing-id") or card.get("data-id")
        if not uuid:
            link_el = card.find("a", href=re.compile(r"/listing/|/vehicledetail/"))
            if link_el:
                uuid = _cars_extract_uuid(link_el.get("href", ""))
        if not uuid:
            continue

        title_el = card.find(class_=re.compile(r"title|heading|name", re.I)) or card.find("h2")
        title = title_el.get_text(strip=True) if title_el else card.get("data-title", "")

        year_m = re.search(r"\b(20\d\d)\b", title)
        if year_m and int(year_m.group(1)) > 2024:
            log.debug("Skipping 2025+ leak: %s", title)
            continue

        if _is_trim_excluded(title):
            log.debug("Skipping non-Lariat trim: %s", title[:60])
            continue

        price_el = card.find(class_=re.compile(r"primary-price|price", re.I))
        price = extract_price(price_el.get_text(strip=True) if price_el else "")

        miles_el = card.find(class_=re.compile(r"mileage|miles", re.I))
        miles = extract_miles(miles_el.get_text(strip=True) if miles_el else "")

        loc_el = card.find(class_=re.compile(r"dealer-name|location|city", re.I))
        location = loc_el.get_text(strip=True) if loc_el else ""

        card_text = card.get_text(" ", strip=True)
        color = detect_color(card_text)

        results.append({
            "id":       uuid,
            "source":   "cars.com",
            "title":    title,
            "price":    price,
            "miles":    miles,
            "color":    color or "",
            "location": location,
            "link":     f"https://www.cars.com/vehicledetail/{uuid}/",
            "_partial": True,
        })

    return results


def _cars_fetch_pw(keyword: str, page: int = 1) -> Optional[BeautifulSoup]:
    """Fetch cars.com search results via Playwright.
    Uses headless=False -- cars.com's bot detection blocks chrome-headless-shell
    reliably regardless of stealth patches (TLS/behavioral fingerprinting).
    headless=False with stealth + realistic UA/viewport consistently returns cards.
    """
    params = dict(CARS_BASE_PARAMS, keyword=keyword)
    if page > 1:
        params["page"] = str(page)
    from urllib.parse import urlencode
    url = CARS_BASE_URL + "?" + urlencode(params, doseq=True)
    content = _pw_fetch(url, wait_ms=8000, headless=False)
    if content:
        return BeautifulSoup(content, "lxml")
    return None


def sweep_cars_com() -> dict:
    """Three keyword sweeps -> dict of {uuid: partial_listing}, deduplicated."""
    all_uuids: dict = {}
    pw_available = True

    for keyword in CARS_KEYWORDS:
        log.info("cars.com sweep: %r", keyword)
        pg = 1
        while True:
            soup = None
            if pw_available:
                soup = _cars_fetch_pw(keyword, pg)
                if soup is None:
                    log.warning("cars.com Playwright failed -- falling back to requests")
                    pw_available = False

            if soup is None:
                soup = _cars_fetch_html(keyword, pg)
            if soup is None:
                break

            json_listings = _cars_try_embedded_json(soup)
            if json_listings and pg == 1:
                log.info("  cars.com: found %d listings in embedded JSON", len(json_listings))
                for item in json_listings:
                    if not isinstance(item, dict):
                        continue
                    uuid = (
                        item.get("id") or item.get("listingId")
                        or item.get("uuid") or item.get("stockNumber")
                    )
                    if not uuid:
                        continue
                    uuid = str(uuid)
                    year = item.get("year") or item.get("modelYear", 0)
                    if year and int(year) > 2024:
                        continue
                    if uuid not in all_uuids:
                        price = item.get("price") or item.get("listPrice")
                        _title = f"{year} {item.get('make','')} {item.get('model','')} {item.get('trim','')}".strip()
                        if _is_trim_excluded(_title):
                            continue
                        all_uuids[uuid] = {
                            "id":       uuid,
                            "source":   "cars.com",
                            "title":    _title,
                            "price":    int(price) if price else None,
                            "miles":    item.get("mileage") or item.get("miles"),
                            "location": item.get("dealerName") or item.get("city", ""),
                            "vin":      item.get("vin", ""),
                            "link":     f"https://www.cars.com/vehicledetail/{uuid}/",
                            "_partial": True,
                        }
                break  # JSON gave us all results, no need to page

            cards = _cars_parse_cards(soup)
            if not cards:
                log.debug("  cars.com: no cards on page %d for %r", pg, keyword)
                break

            new_on_page = sum(1 for c in cards if c["id"] not in all_uuids)
            for c in cards:
                if c["id"] not in all_uuids:
                    all_uuids[c["id"]] = c

            log.info("  page %d: %d cards, %d new", pg, len(cards), new_on_page)

            next_btn = soup.find("a", {"aria-label": re.compile(r"next|page \d+", re.I)})
            if not next_btn or new_on_page == 0:
                break
            pg += 1
            time.sleep(1.5)

        time.sleep(2.0)

    if not all_uuids:
        log.warning(
            "cars.com: 0 results. If this persists, cars.com may be blocking this network. "
            "Run from a home/residential connection."
        )
    else:
        log.info("cars.com: %d unique UUIDs across all sweeps", len(all_uuids))
    return all_uuids


def _parse_cars_vdp_soup(soup: BeautifulSoup, uuid: str, partial: Optional[dict]) -> Optional[dict]:
    """Extract a full listing dict from a parsed cars.com VDP soup object."""
    url = f"https://www.cars.com/vehicledetail/{uuid}/"
    full_text = soup.get_text(" ", strip=True)

    autocheck_text = ""
    for el in soup.find_all(string=re.compile(r"autocheck", re.I)):
        parent = el.find_parent()
        if parent:
            section = parent.find_parent()
            if section:
                autocheck_text = section.get_text(" ", strip=True).lower()
                break

    ld = _extract_ld_json(soup, types=("Car", "Vehicle", "Product"))

    vin_m = _VIN_RE.search(full_text)
    vin = (
        ld.get("vehicleIdentificationNumber") or ld.get("vin")
        or (vin_m.group(1) if vin_m else "")
        or (partial or {}).get("vin", "")
    )

    title = (
        ld.get("name")
        or _text_first(soup, ["h1", ".listing-title", "[data-qa='listing-title']"])
        or (partial or {}).get("title", "F-150 Lightning")
    )
    if title and title.strip().lower() in _BAD_TITLES:
        log.warning("VDP parser: rejecting error/bot page '%s' for %s", title, url)
        return None

    year_m = re.search(r"\b(20\d\d)\b", title or "")
    if year_m and int(year_m.group(1)) > 2024:
        return None

    _page_title_el = soup.find("title")
    _page_title_text = _page_title_el.get_text(" ", strip=True) if _page_title_el else ""
    _ERROR_PAGE_SIGNALS = (
        "your connection is not private",
        "connect to wi-fi",
        "wi-fi required",
        "err_",
        "access denied",
        "403 forbidden",
        "just a moment",
        "attention required",
    )
    _ptitle_lower = _page_title_text.strip().lower()
    if _ptitle_lower and not any(s in _ptitle_lower for s in ("cars.com", "lightning", "f-150")):
        if any(signal in _ptitle_lower for signal in _ERROR_PAGE_SIGNALS):
            log.warning("VDP parser: rejecting error page '%s' for %s",
                        _page_title_text[:80], url)
            return None
    _ptitle_trim_m = re.search(r'\bLightning\s+(\w+)', _page_title_text, re.I)
    if _ptitle_trim_m:
        _ptitle_trim = _ptitle_trim_m.group(1).lower()
        if _ptitle_trim not in ("lariat", "platinum"):
            log.info("Filtering non-Lariat trim %r for listing %s",
                     _ptitle_trim_m.group(1), uuid)
            return None

    _title_lower = (title or "").lower()
    if _title_lower:
        _trim_rejected = any(t in f" {_title_lower} " for t in ("pro", "xlt"))
        _trim_allowed  = any(t in _title_lower for t in _ALLOWED_TRIMS)
        if _trim_rejected and not _trim_allowed:
            log.info("VDP trim filter: rejecting non-Lariat/Platinum listing '%s' (%s)", title, url)
            return None

    offers = ld.get("offers") or {}
    raw_price = offers.get("price") if isinstance(offers, dict) else None
    price = (
        (int(float(raw_price)) if raw_price else None)
        or extract_price(_text_first(soup, [
            ".price-section", ".primary-price", "[data-qa='price']",
            "[class*='price']",
        ]) or "")
        or (partial or {}).get("price")
    )
    if price and not _price_valid(price):
        log.warning("Unrealistic price $%s for VDP %s -- discarding", f"{price:,}", uuid[:12])
        price = None

    if _is_trim_excluded(title or "", price=price):
        log.debug("VDP trim filter: %s (price=$%s)", (title or "")[:60],
                  f"{price:,}" if price else "?")
        return None

    raw_miles = ld.get("mileageFromOdometer")
    if isinstance(raw_miles, dict):
        raw_miles = raw_miles.get("value")
    miles = (
        (int(raw_miles) if raw_miles else None)
        or extract_miles(_text_first(soup, [
            ".mileage", "[data-qa='mileage']", "[class*='mileage']",
        ]) or "")
        or (partial or {}).get("miles")
    )

    color = ld.get("color") or ld.get("vehicleColor")
    if not color:
        for el in soup.find_all(string=re.compile(r"exterior\s+color", re.I)):
            parent = el.find_parent()
            if parent:
                sibling = parent.find_next_sibling()
                if sibling:
                    color = detect_color(sibling.get_text(strip=True))
                    if color:
                        break
                gp = parent.find_parent()
                if gp and not color:
                    nxt = gp.find_next_sibling()
                    if nxt:
                        color = detect_color(nxt.get_text(strip=True))
    if not color:
        for dt in soup.find_all("dt"):
            if re.search(r"exterior\s+color", dt.get_text(strip=True), re.I):
                dd = dt.find_next_sibling("dd")
                if dd:
                    color = detect_color(dd.get_text(strip=True))
                    break
    if not color:
        title_tag = soup.find("h1")
        if title_tag:
            color_m = re.search(
                r"\b(Agate Black|Azure Gray|Antimatter Blue|Carbonized Gray|"
                r"Oxford White|Iconic Silver|Star White|Area 51|Dark Matter|"
                r"Rapid Red|Atlas Blue|Avalanche|Space White)\b",
                title_tag.get_text(), re.I
            )
            if color_m:
                color = color_m.group(1)
    if not color:
        color = detect_color(full_text)
    if not color and (partial or {}).get("color"):
        color = partial["color"]

    seller_info = ld.get("seller") or {}
    seller_name = seller_info.get("name", "")
    seller_addr = seller_info.get("address") or {}
    city  = seller_addr.get("addressLocality", "")
    state = seller_addr.get("addressRegion", "")

    if not seller_name or not city:
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                blob = json.loads(script.string or "")
                for item in (blob if isinstance(blob, list) else [blob]):
                    if not isinstance(item, dict):
                        continue
                    if item.get("@type") in ("AutoDealer", "Organization", "LocalBusiness"):
                        seller_name = seller_name or item.get("name", "")
                        addr = item.get("address") or {}
                        if isinstance(addr, dict):
                            city  = city or addr.get("addressLocality", "")
                            state = state or addr.get("addressRegion", "")
                        if seller_name:
                            break
            except (json.JSONDecodeError, AttributeError):
                pass

    if not seller_name:
        seller_name = _text_first(soup, [
            ".dealer-name", "[data-qa='dealer-name']", "[data-qa='seller-name']",
            "[class*='dealer-name']", ".seller-name",
            "[class*='sds-provider-banner__name']", "[class*='provider-name']",
        ]) or ""
    if not city:
        addr_text = _text_first(soup, [
            "[data-qa='dealer-address']", "[data-qa='seller-address']",
            ".dealer-address", ".seller-address", "[class*='dealer-location']",
            "[class*='sds-provider-banner__address']", "[class*='provider-address']",
        ]) or ""
        city_m = re.search(r"([A-Za-z ]+),\s*([A-Z]{2})\b", addr_text)
        if city_m:
            city  = city_m.group(1).strip()
            state = city_m.group(2)

    if not seller_name or not city:
        for txt in (
            (soup.find("title") or soup.new_tag("x")).get_text(strip=True),
            (soup.find("meta", attrs={"name": "description"}) or {}).get("content", ""),
        ):
            at_m = re.search(
                r"\bat\s+([^|·\n]+?)\s+in\s+([A-Za-z ]+),\s*([A-Z]{2})\b", txt, re.I
            )
            if at_m:
                seller_name = seller_name or at_m.group(1).strip()
                city  = city or at_m.group(2).strip()
                state = state or at_m.group(3)
                break

    if seller_name and city and state:
        location = f"{seller_name} · {city}, {state}"
    elif seller_name and city:
        location = f"{seller_name} · {city}"
    elif city and state:
        location = f"{city}, {state}"
    elif seller_name:
        location = seller_name
    else:
        location = (partial or {}).get("location", "")

    seller_type     = detect_seller_type(full_text)
    er_confirmed, er_note       = detect_er(vin or "", full_text)
    equip_confirmed, equip_note = detect_511a(full_text)
    hist_flag, hist_note = parse_autocheck_panel(autocheck_text)
    if hist_flag is None:
        hist_flag, hist_note = parse_vdp_history(full_text)
    if hist_flag and hist_flag != "❓ Unknown":
        log.info("History resolved for %s: Unknown -> %s (source: %s)",
                 uuid[:16], hist_flag, hist_note or "VDP text")
    is_azure = detect_azure_gray(color or "") or detect_azure_gray(full_text)

    result = _build_listing(
        uid=uuid, source="cars.com", vin=vin, title=title, color=color,
        location=location, seller_type=seller_type, miles=miles, price=price,
        hist_flag=hist_flag, hist_note=hist_note,
        er_confirmed=er_confirmed, er_note=er_note,
        equip_confirmed=equip_confirmed, equip_note=equip_note,
        is_azure=is_azure, link=url,
    )
    result["_vdp_visited"] = True
    return result


async def _async_vdp_worker(
    uuid: str, partial: dict, semaphore: asyncio.Semaphore,
    executor: concurrent.futures.ThreadPoolExecutor,
) -> Optional[dict]:
    """Fetch and parse one cars.com VDP; one browser launch per call via run_in_executor."""
    url = f"https://www.cars.com/vehicledetail/{uuid}/"
    async with semaphore:
        await asyncio.sleep(random.uniform(1, 3))
        content = await asyncio.wait_for(
            _nd_fetch(url, goto_timeout_ms=115_000,
                      wait_selector='script[type="application/ld+json"]'),
            timeout=125.0,
        )
    if not content:
        return _partial_to_listing(partial or {}, uuid, "cars.com", url)
    soup = BeautifulSoup(content, "lxml")
    return _parse_cars_vdp_soup(soup, uuid, partial)


async def visit_all_cars_vdps_parallel(
    cars_uuids: dict, seen: dict
) -> tuple:
    """Fetch all cars.com VDPs concurrently; one browser launch per VDP.
    Returns (results_list, timed_out_uuids_set, tier1_uuids_set)."""
    asyncio.get_running_loop().set_exception_handler(_quiet_exception_handler)

    CONCURRENCY = 4
    semaphore = asyncio.Semaphore(CONCURRENCY)
    timed_out: set = set()
    tier1_uuids: set = set()
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=CONCURRENCY)

    async def process_one(uuid: str, partial: dict) -> Optional[dict]:
        existing = seen.get(uuid)

        if existing and existing.get("status") in (SOLD, "filtered"):
            return existing
        if existing and existing.get("dismissed"):
            return existing

        if (
            existing
            and existing.get("timeout_count", 0) >= 5
            and not existing.get("price")
            and not existing.get("location")
            and not existing.get("color")
        ):
            log.debug("Parallel: skipping chronic timeout %s (count=%d)",
                      uuid[:12], existing["timeout_count"])
            return existing

        is_tier1 = (
            existing is None
            or needs_backfill(existing)
            or existing.get("extended_range_confirmed") is None
            or existing.get("equipment_511a_confirmed") is None
        )

        if is_tier1:
            tier1_uuids.add(uuid)
        else:
            if existing.get("last_vdp_visit") in (TODAY, YESTERDAY):
                log.debug("VDP skip (fresh): %s", uuid[:12])
                return existing

        try:
            result = await _async_vdp_worker(uuid, partial, semaphore, executor)
        except asyncio.TimeoutError:
            log.warning("VDP timeout after 125s: %s", uuid[:12])
            timed_out.add(uuid)
            return existing
        return result

    tasks = [process_one(uuid, partial) for uuid, partial in cars_uuids.items()]
    try:
        gathered = await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        executor.shutdown(wait=False)

    results = []
    for item in gathered:
        if isinstance(item, dict):
            results.append(item)
        elif isinstance(item, Exception):
            log.warning("VDP worker exception: %s", item)
    return results, timed_out, tier1_uuids
