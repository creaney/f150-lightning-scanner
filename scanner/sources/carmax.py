"""scanner/sources/carmax.py -- CarMax sweep."""
import json
import re

from bs4 import BeautifulSoup

from scanner.config import log, CARMAX_BROWSE_URL, _VIN_RE
from scanner.detect import (
    detect_er, detect_511a, detect_color, detect_azure_gray,
    extract_price, extract_miles, parse_history, _is_trim_excluded, _url_hash,
)
from scanner.models import _build_listing
from scanner.browser import _pw_fetch


def sweep_carmax() -> list:
    """Fetch CarMax F-150 Lightning listings via national keyword search page."""
    log.info("CarMax sweep...")
    return _carmax_pw()


def _carmax_pw() -> list:
    """Parse CarMax national Lightning search page.
    CarMax embeds inventory as `const cars = [...]` in a script tag.
    URL: /cars?search=F-150+Lightning returns all Lightning listings nationally (~24 total)."""
    content = _pw_fetch(CARMAX_BROWSE_URL, wait_ms=5000, headless=False)
    if not content:
        log.warning("CarMax: Playwright fetch failed")
        return []

    soup = BeautifulSoup(content, "lxml")
    results = []

    # Primary: extract from embedded `const cars = [...]` JSON
    cars_data = None
    for s in soup.find_all("script"):
        txt = s.string or ""
        if "const cars = [" not in txt:
            continue
        m = re.search(r"const cars = (\[.+?\]);", txt, re.S)
        if m:
            try:
                cars_data = json.loads(m.group(1))
            except (json.JSONDecodeError, ValueError):
                pass
            break

    if cars_data is not None:
        log.info("CarMax: found %d cars in embedded JSON", len(cars_data))
        for item in cars_data:
            if not isinstance(item, dict):
                continue
            year = item.get("year", 0)
            if year < 2023 or year > 2024:
                continue
            vin = item.get("vin") or ""
            fuel = str(item.get("fuelType") or item.get("engineType") or "").lower()
            battery_range = (item.get("originalBatteryRangeInMiles")
                             or item.get("batteryRange") or item.get("rangeEstimate"))
            model_str = str(item.get("model", "")).lower()
            series_str = str(item.get("series", "")).lower()
            trim_str = str(item.get("trim", "")).lower()
            # Lightning-specific: VIN match OR model/series/trim name OR explicitly electric F-150.
            # Use _VIN_RE.search (not .match) -- vin may have leading whitespace.
            # Use regex for "f-150" / "f150" since CarMax uses both spellings.
            _is_electric = "electric" in fuel or "bev" in fuel or "\bev\b" in fuel or battery_range
            is_lightning = (
                bool(_VIN_RE.search(vin))
                or "lightning" in model_str
                or "lightning" in series_str
                or "lightning" in trim_str
                or (re.search(r"f.?150", model_str) and _is_electric)
            )
            if not is_lightning:
                continue
            title_parts = [str(year), str(item.get("make", "")), str(item.get("model", "")),
                           str(item.get("trim", ""))]
            title = " ".join(p for p in title_parts if p and p != "None").strip()
            if _is_trim_excluded(title):
                continue
            price = item.get("basePrice")
            miles = item.get("mileage")
            color = detect_color(item.get("exteriorColor") or "")
            stock = item.get("stockNumber") or _url_hash(vin or title)
            uid = f"carmax-{stock}"
            store_name = item.get("storeName") or "CarMax"
            store_city = item.get("storeCity") or ""
            store_state = item.get("stateAbbreviation") or ""
            location = f"{store_name} · {store_city}, {store_state}".strip(" ·,")
            link = f"https://www.carmax.com/car/{stock}"
            er_confirmed, er_note = detect_er(vin, title)
            equip_confirmed, equip_note = detect_511a(title)
            hist_flag, hist_note = parse_history(str(item))
            is_azure = detect_azure_gray(color or "") or detect_azure_gray(title)
            results.append(_build_listing(
                uid=uid, source="carmax", vin=vin,
                title=title, color=color or "",
                location=location, seller_type="CarMax",
                miles=int(miles) if miles else None,
                price=int(price) if price else None,
                hist_flag=hist_flag, hist_note=hist_note,
                er_confirmed=er_confirmed, er_note=er_note,
                equip_confirmed=equip_confirmed, equip_note=equip_note,
                is_azure=is_azure, link=link,
            ))
        log.info("CarMax Playwright (JSON): %d Lightning listings parsed", len(results))
        if results:
            return results  # Only short-circuit if we actually found Lightnings
        # The embedded JSON contains non-Lightning vehicles (location-based inventory).
        # Fall through to CSS card parsing, which reads the actual rendered page.
        log.info("CarMax: embedded JSON had no Lightning -- trying CSS card parsing")

    # Fallback: CSS card selectors
    cards = (
        soup.select("[data-clickprops*='YMM']")
        or soup.select("[data-qa='car-block']")
        or soup.select(".car-block")
        or soup.select("[class*='car-block']")
    )
    if not cards:
        log.warning("CarMax: no listing cards or JSON found")
        return []

    log.info("CarMax: found %d CSS cards", len(cards))
    for card in cards:
        clickprops = card.get("data-clickprops", "")
        stock_m = re.search(r"StockNumber:\s*(\d+)", clickprops)
        if not stock_m:
            continue
        stock = stock_m.group(1)
        link = f"https://www.carmax.com/car/{stock}"
        uid = f"carmax-{stock}"
        full_text = card.get_text(" ", strip=True) + " " + clickprops
        # Skip non-Lightning vehicles (Bronco Sports, Escapes, etc.)
        if "lightning" not in full_text.lower():
            continue
        year_m = re.search(r"\b(20\d\d)\b", full_text)
        if not year_m or int(year_m.group(1)) not in range(2023, 2025):
            continue
        title_el = card.find(["h2", "h3", "h4"])
        title = title_el.get_text(strip=True) if title_el else full_text[:80]
        if _is_trim_excluded(title):
            continue
        price = extract_price(full_text)
        miles = extract_miles(full_text)
        color = detect_color(full_text)
        vin_m = _VIN_RE.search(full_text)
        vin = vin_m.group(1) if vin_m else ""
        er_confirmed, er_note = detect_er(vin, full_text)
        equip_confirmed, equip_note = detect_511a(full_text)
        hist_flag, hist_note = parse_history(full_text)
        is_azure = detect_azure_gray(color or "") or detect_azure_gray(full_text)
        results.append(_build_listing(
            uid=uid, source="carmax", vin=vin,
            title=title, color=color or "",
            location="CarMax", seller_type="CarMax",
            miles=miles, price=price,
            hist_flag=hist_flag, hist_note=hist_note,
            er_confirmed=er_confirmed, er_note=er_note,
            equip_confirmed=equip_confirmed, equip_note=equip_note,
            is_azure=is_azure, link=link,
        ))

    log.info("CarMax Playwright (CSS): %d listings parsed", len(results))
    return results
