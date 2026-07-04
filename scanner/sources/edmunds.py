"""scanner/sources/edmunds.py -- Edmunds sweep."""
import asyncio
import json
import re
from typing import Optional

from scanner.config import log, EDMUNDS_URL, _EDMUNDS_DIAG_DONE
from scanner.detect import (
    detect_er, detect_511a, detect_color, detect_azure_gray,
    _is_trim_excluded, parse_history, _url_hash,
)
from scanner.models import _build_listing
from scanner.browser import _quiet_exception_handler


def _parse_edmunds_preloaded_state(content: str) -> tuple:
    """Extract listings from window.EDM.preloadedState SSR JSON in Edmunds HTML.

    Edmunds injects full inventory data as:
        window.EDM.preloadedState = {...};
    We try several known paths into the JSON since the structure has shifted
    across Edmunds deployments.

    Returns (results, raw_count) where raw_count is the number of raw inventory
    entries before trim/year filtering and results is the filtered listing list.
    raw_count == 0 means no inventory data was found at all (stop paginating).
    """
    m = re.search(r"window\.EDM\.preloadedState\s*=\s*(\{)", content)
    if not m:
        log.warning("Edmunds: window.EDM.preloadedState not found in page HTML")
        return [], 0

    start = m.start(1)
    depth = 0
    end = start
    for i, ch in enumerate(content[start : start + 2_000_000]):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = start + i + 1
                break
    if end == start:
        log.warning("Edmunds: could not locate closing brace for preloadedState JSON")
        return [], 0

    try:
        state = json.loads(content[start:end])
    except (json.JSONDecodeError, ValueError) as exc:
        log.warning("Edmunds: JSON parse of preloadedState failed: %s", exc)
        return [], 0

    log.debug("Edmunds: preloadedState top-level keys: %s", list(state.keys())[:20])

    # Try each known path where Edmunds has placed inventory results.
    # Log what we find so future breakages are diagnosable.
    _candidate_paths = [
        # Current (2025): inventories nested under searchResults
        ("inventory", "searchResults", "inventories", "results"),
        # Alternate: flat results under searchResults
        ("inventory", "searchResults", "results"),
        # Alternate: inventories at top of searchResults
        ("inventory", "inventories", "results"),
        # Older layout
        ("inventory", "results"),
        # Very old layout
        ("searchResults", "inventories", "results"),
        ("searchResults", "results"),
    ]

    for path in _candidate_paths:
        node = state
        for key in path:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(key)
        if isinstance(node, list) and len(node) > 0:
            log.info("Edmunds: found %d inventories at path: %s", len(node), " -> ".join(path))
            return _parse_edmunds_inventories(node), len(node)
        elif isinstance(node, list):
            log.debug("Edmunds: path %s exists but is empty", " -> ".join(path))

    # Nothing found -- log diagnostics so we can find the real path
    inv_node = state.get("inventory")
    if isinstance(inv_node, dict):
        log.warning(
            "Edmunds: 'inventory' key exists but no results found. "
            "inventory sub-keys: %s", list(inv_node.keys())[:20]
        )
        sr = inv_node.get("searchResults")
        if isinstance(sr, dict):
            log.warning("Edmunds: searchResults sub-keys: %s", list(sr.keys())[:20])
    else:
        log.warning(
            "Edmunds: no 'inventory' key in preloadedState. "
            "Top-level keys: %s", list(state.keys())[:20]
        )
    return [], 0


def _edmunds_str(val) -> str:
    """Safely extract a string from an Edmunds field that may be a str or a dict."""
    if isinstance(val, str):
        return val.strip()
    if isinstance(val, dict):
        return (val.get("name") or val.get("niceName") or val.get("slug") or "").strip()
    return ""


def _edmunds_int(val) -> Optional[int]:
    """Safely coerce an Edmunds int-or-string field."""
    if val is None:
        return None
    try:
        return int(str(val).replace(",", "").replace("$", "").strip())
    except (ValueError, TypeError):
        return None


def _parse_edmunds_inventories(inventories: list) -> list:
    """Convert Edmunds inventory results[] entries to standard listing dicts.

    Edmunds' schema has shifted over time; this function handles both the
    nested-dict layout (vehicle.make.name) and flat-string layout (make="Ford").
    """
    if inventories:
        first = inventories[0]
        log.info("Edmunds: first result keys: %s", list(first.keys())[:30])
        log.debug("Edmunds: first result sample: %.600s", json.dumps(first, default=str))
        # -- Diagnostic: log actual Edmunds field structure (once per run) --
        global _EDMUNDS_DIAG_DONE
        if not _EDMUNDS_DIAG_DONE:
            _EDMUNDS_DIAG_DONE = True
            vehicle_info = first.get("vehicleInfo") or first.get("vehicle") or {}
            prices_info  = first.get("prices") or {}
            dealer_info  = first.get("dealerInfo") or first.get("dealer") or {}
            log.info("Edmunds vehicleInfo keys/values: %s",
                     {k: v for k, v in vehicle_info.items()} if isinstance(vehicle_info, dict) else vehicle_info)
            log.info("Edmunds prices keys/values: %s",
                     {k: v for k, v in prices_info.items()} if isinstance(prices_info, dict) else prices_info)
            log.info("Edmunds dealerInfo keys/values: %s",
                     {k: v for k, v in dealer_info.items()} if isinstance(dealer_info, dict) else dealer_info)

    results = []
    for item in inventories:
        if not isinstance(item, dict):
            continue

        # -- Year / Make / Model / Trim ----------------------------------------
        # All live inside vehicleInfo.styleInfo -- NOT at vehicleInfo.year etc.
        vehicle_info = item.get("vehicleInfo") or {}
        style_info   = vehicle_info.get("styleInfo") or {} if isinstance(vehicle_info, dict) else {}

        year = _edmunds_int(style_info.get("year"))
        if year is None:
            continue
        if year < 2023 or year > 2024:
            continue

        make_name  = _edmunds_str(style_info.get("make")  or "Ford")
        model_name = _edmunds_str(style_info.get("model") or "F-150 Lightning")
        trim_name  = _edmunds_str(style_info.get("trim")  or "")

        title = f"{year} {make_name} {model_name} {trim_name}".strip()
        if _is_trim_excluded(title):
            continue
        if "lightning" not in title.lower():
            continue

        # -- VIN (top-level) ---------------------------------------------------
        vin = str(item.get("vin") or "").upper().strip()

        # -- Price -------------------------------------------------------------
        prices    = item.get("prices") or {}
        price_raw = prices.get("displayPrice") or prices.get("advertisedPrice") if isinstance(prices, dict) else None
        price     = _edmunds_int(price_raw)
        if price and price > 65_000:
            continue

        # -- Mileage -----------------------------------------------------------
        miles = _edmunds_int(vehicle_info.get("mileage") if isinstance(vehicle_info, dict) else None)

        # -- Color -------------------------------------------------------------
        # vehicleInfo.vehicleColors.exterior.name or .genericName
        veh_colors = vehicle_info.get("vehicleColors") or {} if isinstance(vehicle_info, dict) else {}
        exterior   = veh_colors.get("exterior") or {} if isinstance(veh_colors, dict) else {}
        color_name = _edmunds_str(exterior.get("name") or exterior.get("genericName") or "")
        color      = detect_color(color_name)

        # -- ER detection ------------------------------------------------------
        # Use electricityRange from partsInfo (240 mi = ER, ~100 mi = SR).
        parts_info = vehicle_info.get("partsInfo") or {} if isinstance(vehicle_info, dict) else {}
        elec_range = _edmunds_int(parts_info.get("electricityRange") if isinstance(parts_info, dict) else None)

        # Full JSON text for signal scanning (detect_er, detect_511a, parse_history)
        full_text = json.dumps(item)

        if elec_range is not None:
            if elec_range >= 270:
                # Extended Range: ~300-320 miles EPA (131 kWh battery)
                er_confirmed = True
                er_note      = f"Edmunds electricityRange={elec_range}mi"
            else:
                # Standard Range: ~230-240 miles EPA (98 kWh battery)
                er_confirmed = False
                er_note      = f"Edmunds electricityRange={elec_range}mi (SR)"
        else:
            er_confirmed, er_note = detect_er(vin, title + " " + full_text)

        # -- 511A detection via cgf feature list --------------------------------
        # vehicleInfo.partsInfo.cgf: [{name, formattedName}, ...]
        cgf_list = parts_info.get("cgf") or [] if isinstance(parts_info, dict) else []
        cgf_text = " ".join(
            f.get("name", "") + " " + f.get("formattedName", "")
            for f in cgf_list if isinstance(f, dict)
        )
        equip_confirmed, equip_note = detect_511a(title + " " + cgf_text + " " + full_text)

        # -- History -----------------------------------------------------------
        hist_flag, hist_note = parse_history(full_text)
        # -- Edmunds historyInfo -- Experian/AutoCheck structured data ----------
        # Parse the structured historyInfo dict for history signals.
        # Only fills in history when it's still Unknown -- never downgrades.
        _hist_info = item.get("historyInfo") or {}
        if isinstance(_hist_info, dict) and hist_flag == "❓ Unknown":
            if _hist_info.get("lemonHistory") is True:
                hist_flag, hist_note = "\U0001f6ab Buyback", "lemon (autocheck)"
            elif _hist_info.get("salvageHistory") is True:
                hist_flag, hist_note = "\U0001f6ab Salvage", "salvage (autocheck)"
            elif _hist_info.get("frameDamage") is True:
                _title_ok = _hist_info.get("cleanTitle", True)
                if not _title_ok:
                    _td = _hist_info.get("titleDescription", "frame damage")
                    hist_flag, hist_note = "\U0001f6ab Buyback", f"{_td} (autocheck)"
                else:
                    hist_flag, hist_note = "⚠️ Accident", "frame damage (autocheck)"
            elif _hist_info.get("cleanTitle") is False:
                _td = _hist_info.get("titleDescription", "title issue")
                hist_flag, hist_note = "\U0001f6ab Buyback", f"{_td} (autocheck)"
            elif (
                _hist_info.get("noAccidents") is False
                or str(_hist_info.get("accidentText", "0")) not in ("0", "")
            ):
                hist_flag, hist_note = "⚠️ Accident", "reported (autocheck)"
            elif _hist_info.get("cleanTitle") is True and _hist_info.get("noAccidents") is True:
                hist_flag, hist_note = "✅ Clean", "clean (autocheck)"

        # -- Azure Gray --------------------------------------------------------
        is_azure = detect_azure_gray(color or "") or detect_azure_gray(color_name) or detect_azure_gray(title)

        # -- Dealer / Location -------------------------------------------------
        # dealerInfo.name, dealerInfo.address.{city, stateCode}
        dealer      = item.get("dealerInfo") or {}
        seller_name = ""
        city        = ""
        state_code  = ""
        if isinstance(dealer, dict):
            seller_name = _edmunds_str(dealer.get("name") or "")
            address     = dealer.get("address") or {}
            if isinstance(address, dict):
                city       = _edmunds_str(address.get("city") or "")
                state_code = _edmunds_str(address.get("stateCode") or address.get("state") or "")
        location_str = f"{city}, {state_code}".strip(", ")
        location = f"{seller_name} · {location_str}".strip(" ·") if seller_name else location_str or "Edmunds"

        seller_type = "CPO" if item.get("cpo") else "Dealer"

        # -- VDP link (use listingUrl directly) ---------------------------------
        link = item.get("listingUrl") or ""
        if not link and vin:
            make_slug  = re.sub(r"[^a-z0-9]+", "-", make_name.lower()).strip("-")
            model_slug = re.sub(r"[^a-z0-9]+", "-", model_name.lower()).strip("-")
            link = f"https://www.edmunds.com/{make_slug}/{model_slug}/{year}/vin/{vin}/"

        uid = f"edmunds-{vin}" if vin else f"edmunds-{_url_hash(link)}"

        results.append(_build_listing(
            uid=uid, source="edmunds", vin=vin,
            title=title, color=color or color_name,
            location=location, seller_type=seller_type,
            miles=miles, price=price,
            hist_flag=hist_flag, hist_note=hist_note,
            er_confirmed=er_confirmed, er_note=er_note,
            equip_confirmed=equip_confirmed, equip_note=equip_note,
            is_azure=is_azure, link=link,
        ))
    if inventories and not results:
        log.warning(
            "Edmunds: %d inventories found but 0 passed trim/year filter -- "
            "check EDMUNDS_URL trim path segment",
            len(inventories),
        )
    return results


async def _nd_edmunds_sweep(base_url: str) -> list:
    """Fetch all Edmunds listing pages in one browser session.

    Edmunds SSR injects full inventory into window.EDM.preloadedState.
    Pagination via ?pagenumber=N in the URL.
    """
    import nodriver as uc
    browser = None
    all_results: dict = {}
    try:
        browser = await uc.start(
            headless=False,
            browser_args=["--disable-http2", "--no-sandbox"],
            sandbox=False,
        )

        for page in range(1, 20):  # hard cap: 20 pages x ~25 = ~500
            if page == 1:
                url = base_url
            else:
                sep = "&" if "?" in base_url else "?"
                url = f"{base_url}{sep}pagenumber={page}"

            log.debug("Edmunds page %d: %s", page, url)
            try:
                tab = await asyncio.wait_for(browser.get(url), timeout=60.0)
            except asyncio.TimeoutError:
                log.warning("Edmunds page %d: navigation timed out", page)
                break

            # Wait for page content -- SSR means it's usually already there
            await asyncio.sleep(3.0)

            content = await tab.get_content()
            if not content or len(content) < 5000:
                log.warning("Edmunds page %d: short/empty content", page)
                break

            page_results, raw_count = _parse_edmunds_preloaded_state(content)
            if raw_count == 0:
                log.info("Edmunds page %d: no inventory data -- stopping", page)
                break
            if not page_results:
                log.info("Edmunds page %d: %d inventories found, 0 passed trim/year filter "
                         "-- continuing to next page", page, raw_count)
                await asyncio.sleep(1.0)
                continue

            new_count = sum(1 for r in page_results if r["id"] not in all_results)
            for r in page_results:
                all_results[r["id"]] = r
            log.info(
                "Edmunds page %d: %d listings (%d raw), %d new  (total unique: %d)",
                page, len(page_results), raw_count, new_count, len(all_results),
            )
            if new_count == 0:
                log.debug("Edmunds: stopping at page %d (no new results)", page)
                break
            await asyncio.sleep(1.0)

    except asyncio.TimeoutError:
        log.warning("Edmunds: browser navigation timed out")
    except Exception as exc:
        log.warning("Edmunds: unexpected error in sweep: %s", exc)
    finally:
        if browser:
            try:
                await browser.stop()
            except Exception:
                pass

    return list(all_results.values())


def sweep_edmunds() -> list:
    """Fetch all Edmunds 2023-2024 F-150 Lightning listings (all pages)."""
    log.info("Edmunds sweep...")
    loop = asyncio.new_event_loop()
    loop.set_exception_handler(_quiet_exception_handler)
    asyncio.set_event_loop(loop)
    try:
        results = loop.run_until_complete(_nd_edmunds_sweep(EDMUNDS_URL))
    except Exception as exc:
        log.warning("Edmunds sweep failed: %s", exc)
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
    log.info("Edmunds: %d total listings found", len(results))
    return results


def sweep_vroom() -> list:
    """Vroom ceased operations in January 2024 -- no inventory available."""
    log.info("Vroom: ceased operations January 2024 -- skipping")
    return []
