"""scanner/sources/autotrader.py -- AutoTrader sweep and VDP backfill."""
import json
import re
import time
from typing import Optional

from bs4 import BeautifulSoup

from scanner.config import log, AT_SEARCH_URL, _VIN_RE
from scanner.detect import (
    detect_er, detect_511a, detect_color, detect_azure_gray,
    extract_price, extract_miles, parse_history, _is_trim_excluded, _price_valid,
    _text_first,
)
from scanner.models import _build_listing
from scanner.browser import _pw_fetch


def _at_parse_listing_objects(listings: list) -> list:
    """Convert a list of AutoTrader listing dicts to our standard format."""
    results = []
    _diag_logged = False
    for item in listings:
        if not isinstance(item, dict):
            continue

        # diagnostic: log field structure of the first real dict
        if not _diag_logged:
            _diag_logged = True
            _top_keys = list(item.keys())
            log.info("AT DIAG listing[0] top-level keys: %s", _top_keys)
            _cands = ["pricingDetail", "pricing", "owner", "dealer", "sellerInfo",
                      "vehicleInfo", "specs", "location"]
            for _ck in _cands:
                _cv = item.get(_ck)
                if _cv is not None:
                    if isinstance(_cv, dict):
                        log.info("AT DIAG   %s (dict) keys=%s", _ck, list(_cv.keys()))
                    else:
                        log.info("AT DIAG   %s = %r", _ck, _cv)
            _direct = ["listPrice", "salePrice", "price", "mileage", "trim",
                       "year", "make", "model", "vin", "exteriorColor",
                       "listingId", "id", "adId"]
            _direct_vals = {k: item.get(k) for k in _direct if item.get(k) is not None}
            log.info("AT DIAG   direct fields: %s", _direct_vals)

        listing_id = str(
            item.get("listingId") or item.get("id") or item.get("adId") or ""
        )
        if not listing_id:
            continue
        uid = f"at-{listing_id}"

        # Vehicle specs may be nested under vehicleInfo or at top level
        specs = item.get("vehicleInfo") or item.get("specs") or item
        year = specs.get("year") or item.get("year", 0)
        try:
            year = int(year)
        except (TypeError, ValueError):
            year = 0
        if not year or year < 2023 or year > 2024:
            continue

        make  = specs.get("make")  or item.get("make",  "Ford")
        model = specs.get("model") or item.get("model", "F-150 Lightning")
        trim  = specs.get("trim")  or item.get("trim",  "")
        title = f"{year} {make} {model} {trim}".strip()
        if _is_trim_excluded(title):
            continue
        if "lightning" not in title.lower() and "lightning" not in str(item).lower():
            continue

        vin = specs.get("vin") or item.get("vin", "")

        # Price -- may be under pricingDetail, pricing, or top-level
        price_src = item.get("pricingDetail") or item.get("pricing") or item
        price_raw = (
            price_src.get("salePrice") or price_src.get("price")
            or price_src.get("listPrice") or price_src.get("derivedPrice")
        )
        price = None
        if price_raw:
            try:
                price = int(str(price_raw).replace(",", "").replace("$", "").strip())
            except (ValueError, TypeError):
                pass
        if price and price > 65_000:
            continue

        miles_raw = specs.get("mileage") or item.get("mileage")
        miles = None
        if miles_raw:
            try:
                miles = int(str(miles_raw).replace(",", "").strip())
            except (ValueError, TypeError):
                pass

        color = detect_color(
            specs.get("exteriorColor") or specs.get("color")
            or item.get("exteriorColor", "")
        )

        # Dealer / location
        dealer = item.get("dealer") or item.get("sellerInfo") or {}
        if isinstance(dealer, dict):
            d_name  = dealer.get("name") or dealer.get("dealerName") or ""
            d_loc   = dealer.get("location") or {}
            d_city  = (d_loc.get("city") if isinstance(d_loc, dict) else None) or dealer.get("city") or ""
            d_state = (d_loc.get("state") if isinstance(d_loc, dict) else None) or dealer.get("state") or ""
            location = f"{d_name} · {d_city}, {d_state}".strip(" ·,")
        else:
            location = ""

        link = f"https://www.autotrader.com/cars-for-sale/vehicledetails/{listing_id}/"
        full_str = str(item)
        er_confirmed,    er_note    = detect_er(vin, title + " " + full_str)
        equip_confirmed, equip_note = detect_511a(full_str)
        hist_flag, hist_note        = parse_history(full_str)
        is_azure = detect_azure_gray(color or "") or detect_azure_gray(title)
        results.append(_build_listing(
            uid=uid, source="autotrader", vin=vin,
            title=title, color=color or "",
            location=location, seller_type="Dealer",
            miles=miles, price=price,
            hist_flag=hist_flag, hist_note=hist_note,
            er_confirmed=er_confirmed, er_note=er_note,
            equip_confirmed=equip_confirmed, equip_note=equip_note,
            is_azure=is_azure, link=link,
        ))
    return results


def _at_parse_preloaded_state(content: str) -> Optional[list]:
    """Try to extract listings from AutoTrader's embedded JSON state.

    AutoTrader embeds page data in one of several places depending on
    the deployment version. Returns a list of our standard listing
    dicts, or None if no recognisable data was found (so the caller
    can fall through to DOM card parsing).
    """
    # attempt 1: Next.js __NEXT_DATA__ script tag
    soup = BeautifulSoup(content, "lxml")
    next_el = soup.find("script", id="__NEXT_DATA__")
    if next_el and next_el.string:
        try:
            data = json.loads(next_el.string)
            # Listings live somewhere inside props.pageProps
            pp = data.get("props", {}).get("pageProps", {})
            raw_listings = (
                pp.get("listings")
                or pp.get("initialListings")
                or pp.get("initialState", {}).get("listings")
            )
            if raw_listings and isinstance(raw_listings, list):
                log.info("AT JSON: found %d listing objects via __NEXT_DATA__ (pageProps path)", len(raw_listings))
                return _at_parse_listing_objects(raw_listings)
        except Exception:
            pass

    # attempt 2: window.__PRELOADED_STATE__ = {...}
    m = re.search(
        r"window\.__PRELOADED_STATE__\s*=\s*(\{.+?\})\s*(?:;|</script)",
        content, re.S,
    )
    if m:
        try:
            data = json.loads(m.group(1))
            raw_listings = (
                data.get("listings")
                or data.get("inventory", {}).get("listings")
                or data.get("searchResults", {}).get("listings")
            )
            if raw_listings is None:
                def _hunt(obj, depth=0):
                    if depth > 6:
                        return None
                    if isinstance(obj, list) and obj and isinstance(obj[0], dict):
                        if "listingId" in obj[0] or "vin" in obj[0]:
                            return obj
                    if isinstance(obj, dict):
                        for v in obj.values():
                            r = _hunt(v, depth + 1)
                            if r:
                                return r
                    return None
                raw_listings = _hunt(data)
            if raw_listings and isinstance(raw_listings, list):
                log.info("AT JSON: found %d listing objects via __PRELOADED_STATE__", len(raw_listings))
                return _at_parse_listing_objects(raw_listings)
        except Exception:
            pass

    # attempt 3: any <script type="application/json"> with listingId
    for script_el in soup.find_all("script", type="application/json"):
        raw = script_el.string or ""
        if "listingId" not in raw and "vehicledetails" not in raw:
            continue
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                parsed = _at_parse_listing_objects(data)
                if parsed:
                    log.info("AT JSON: found %d listing objects via <script application/json> (list)", len(data))
                    return parsed
            elif isinstance(data, dict):
                for v in data.values():
                    if isinstance(v, list):
                        parsed = _at_parse_listing_objects(v)
                        if parsed:
                            log.info("AT JSON: found %d listing objects via <script application/json> (dict value)", len(v))
                            return parsed
        except Exception:
            continue

    return None  # signal: fall through to DOM card parsing


def _at_parse_cards(soup: BeautifulSoup) -> list:
    """Parse AutoTrader listing cards from rendered DOM.

    AutoTrader uses React and renders [data-cmp="inventoryListing"] divs.
    After React hydration the listing ID appears as the numeric `id` attribute
    (e.g. <div id="780311151" data-cmp="inventoryListing">).
    Placeholder ids like "_r_k_" indicate unhydrated cards -- these are skipped.

    VDP URL: https://www.autotrader.com/cars-for-sale/vehicledetails.xhtml?listingId={id}
    """
    cards = soup.select('[data-cmp="inventoryListing"]')
    if not cards:
        return []

    results = []
    skipped_placeholder = 0
    _at_card_diag_done = False   # log first real card only
    for card in cards:
        # Listing ID is the numeric id attribute set after React hydration.
        card_id = card.get("id", "")
        if not re.match(r'^\d{7,}$', card_id):
            skipped_placeholder += 1
            continue

        listing_id = card_id
        uid = f"at-{listing_id}"

        # Diagnostic: first valid card only
        if not _at_card_diag_done:
            _at_card_diag_done = True
            _diag_text = card.get_text(" ", strip=True)
            _diag_price = extract_price(_diag_text)
            _diag_miles = extract_miles(_diag_text)
            log.info("AT card[0] id=%s  full_text[:600]: %r", card_id, _diag_text[:600])
            log.info("AT card[0] extract_price=%r  extract_miles=%r", _diag_price, _diag_miles)
            for _el in card.find_all(True, recursive=False)[:8]:
                _el_text = _el.get_text(" ", strip=True)[:120]
                _el_cls  = " ".join(_el.get("class") or [])[:60]
                if _el_text:
                    log.info("  AT card child <%s class=%r>: %r", _el.name, _el_cls, _el_text)

        href = (
            f"https://www.autotrader.com/cars-for-sale/vehicledetails.xhtml"
            f"?listingId={listing_id}"
        )

        full_text = card.get_text(" ", strip=True)
        if "lightning" not in full_text.lower():
            continue

        title_el = (
            card.find(["h2", "h3", "h4"])
            or card.find(class_=re.compile(r"title|heading", re.I))
        )
        title = title_el.get_text(strip=True) if title_el else full_text[:80]

        # AT headings omit trim -- extract it from full card text and append.
        _trim_m = re.search(
            r'\bF-?150\s+Lightning\s+(Lariat|Platinum|Flash|XLT|Pro)\b',
            full_text, re.I,
        )
        if _trim_m:
            _trim_canonical = {
                "lariat": "Lariat", "platinum": "Platinum",
                "flash": "Flash", "xlt": "XLT", "pro": "Pro",
            }
            _trim_word = _trim_canonical.get(_trim_m.group(1).lower(), _trim_m.group(1))
            if _trim_word.lower() not in title.lower():
                title = title.rstrip() + " " + _trim_word

        year_m = re.search(r"\b(20\d\d)\b", title or full_text)
        if not year_m:
            continue
        year = int(year_m.group(1))
        if year < 2023 or year > 2024:
            continue

        if _is_trim_excluded(title):
            continue

        price = extract_price(full_text)
        if price and price > 65_000:
            continue

        miles = extract_miles(full_text)
        color = detect_color(full_text)
        vin_m = _VIN_RE.search(full_text)
        vin   = vin_m.group(1) if vin_m else ""

        # Extract dealer name from "Dealer Name N.NN mi. away" pattern.
        _dist_parts = re.split(
            r'\s+\d{1,4}(?:\.\d{1,2})?\s+mi\.?\s+away', full_text, maxsplit=1, flags=re.I
        )
        if len(_dist_parts) >= 2:
            _pre = _dist_parts[0]
            _segs = re.split(
                r'(?:No Accidents|Excellent|Good Deal|Great Deal|Fair Deal|'
                r'EV Battery|See payment|Dealer Fees|Electric|Hybrid|'
                r'\d+[Kk]\s*mi\b|[\d,]{4,})',
                _pre, flags=re.I,
            )
            location = _segs[-1].strip()[:60] if _segs else ""
        else:
            location = ""

        er_confirmed,    er_note    = detect_er(vin, full_text)
        equip_confirmed, equip_note = detect_511a(full_text)
        hist_flag, hist_note        = parse_history(full_text)
        is_azure = detect_azure_gray(color or "") or detect_azure_gray(full_text)
        results.append(_build_listing(
            uid=uid, source="autotrader", vin=vin,
            title=title, color=color or "",
            location=location, seller_type="Dealer",
            miles=miles, price=price,
            hist_flag=hist_flag, hist_note=hist_note,
            er_confirmed=er_confirmed, er_note=er_note,
            equip_confirmed=equip_confirmed, equip_note=equip_note,
            is_azure=is_azure, link=href,
        ))

    if skipped_placeholder:
        log.debug("AutoTrader: skipped %d unhydrated placeholder card(s)", skipped_placeholder)
    return results


def sweep_autotrader() -> list:
    """Fetch AutoTrader 2023-2024 F-150 Lightning Lariat listings.

    Tries embedded JSON (Next.js / preloaded state) then DOM card parsing.
    Pages through results until no new listings appear.
    """
    log.info("AutoTrader sweep...")
    all_results: dict = {}  # uid -> listing

    for page in range(6):   # hard cap: 6 pages x up to ~25 = ~150 results
        url = AT_SEARCH_URL + f"&firstRecord={page * 25}"
        content = _pw_fetch(
            url,
            wait_selector='[data-cmp="inventoryListing"]',
            wait_ms=6000,
            headless=False,
        )
        if not content:
            if page == 0:
                log.warning("AutoTrader: failed to fetch search page")
            break

        soup = BeautifulSoup(content, "lxml")

        page_results = _at_parse_preloaded_state(content)
        if page_results is None:
            log.info("AutoTrader: no embedded JSON on page %d, trying DOM cards", page + 1)
            page_results = _at_parse_cards(soup)

        if not page_results:
            if page == 0:
                _at_title = soup.find("title")
                _at_title_text = _at_title.get_text(strip=True) if _at_title else "no <title>"
                _at_body = soup.get_text(" ", strip=True)[:500]
                log.warning(
                    "AutoTrader: 0 listings on page 1 -- page title: %r -- body: %s",
                    _at_title_text, _at_body,
                )
            break

        new_on_page = sum(1 for r in page_results if r["id"] not in all_results)
        for r in page_results:
            if r["id"] not in all_results:
                all_results[r["id"]] = r

        log.info("AutoTrader page %d: %d listings, %d new",
                 page + 1, len(page_results), new_on_page)

        if new_on_page == 0 or len(page_results) < 15:
            break   # last page reached

        time.sleep(2.0)

    results = list(all_results.values())
    log.info("AutoTrader: %d total unique listings", len(results))
    return results


def _is_at_blocked(content: Optional[str]) -> bool:
    """Return True when AutoTrader returned a bot-challenge / error page instead of a VDP."""
    if content is None:
        return True
    if len(content) < 5_000:
        return True
    cl = content.lower()
    if "page unavailable" in cl:
        return True
    if "google.com/recaptcha" in cl:
        return True
    if "origin-trial" in cl and len(content) < 20_000:
        return True
    return False


def _parse_autotrader_vdp(url: str, content: Optional[str] = None) -> dict:
    """Fetch an AutoTrader VDP and extract price, mileage, location, trim, VIN, color.

    Tries embedded preloaded-state JSON first (same paths as search page), then
    falls back to DOM label scraping. Returns a partial dict of any fields found.

    Pass ``content`` to skip the HTTP fetch (used by the backfill loop which
    already fetched the page to check for bot-challenge responses).
    """
    if content is None:
        content = _pw_fetch(url, wait_ms=5000, headless=False)
    if not content:
        log.debug("AutoTrader VDP: no content for %s", url)
        return {}

    # Attempt 1: parse embedded JSON (reuse existing search-page extractor)
    preloaded = _at_parse_preloaded_state(content)
    if preloaded and len(preloaded) == 1:
        item = preloaded[0]
        return {
            k: v for k, v in item.items()
            if k in ("price", "miles", "location", "color", "vin", "title",
                      "extended_range_confirmed", "er_note",
                      "equipment_511a_confirmed", "equip_note",
                      "history_flag", "history_note", "seller_type")
            and v not in (None, "", 0)
        }

    # Attempt 2: VDP-specific JSON -- look for a single listing object
    soup = BeautifulSoup(content, "lxml")
    for script_el in soup.find_all("script", type="application/json"):
        raw = script_el.string or ""
        if "listingId" not in raw and "vehicleInfo" not in raw:
            continue
        try:
            data = json.loads(raw)
            items = []
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                def _hunt_vdp(obj, depth=0):
                    if depth > 5:
                        return None
                    if isinstance(obj, dict) and ("listingId" in obj or "vehicleInfo" in obj):
                        return obj
                    if isinstance(obj, dict):
                        for v in obj.values():
                            r = _hunt_vdp(v, depth + 1)
                            if r:
                                return r
                    return None
                found = _hunt_vdp(data)
                if found:
                    items = [found]
            parsed = _at_parse_listing_objects(items)
            if parsed:
                item = parsed[0]
                return {
                    k: v for k, v in item.items()
                    if k in ("price", "miles", "location", "color", "vin", "title",
                              "extended_range_confirmed", "er_note",
                              "equipment_511a_confirmed", "equip_note",
                              "history_flag", "history_note", "seller_type")
                    and v not in (None, "", 0)
                }
        except Exception:
            continue

    # Attempt 3: DOM label scraping
    result: dict = {}
    full_text = soup.get_text(" ", strip=True)

    vin_m = _VIN_RE.search(full_text)
    if vin_m:
        result["vin"] = vin_m.group(1).upper()
    else:
        vin_label_m = re.search(r"\bVIN\b[\s:]*([A-HJ-NPR-Z0-9]{17})\b", full_text, re.I)
        if vin_label_m:
            result["vin"] = vin_label_m.group(1).upper()

    price = extract_price(
        (_text_first(soup, [
            '[data-cmp="priceBadge"]', '[class*="primaryPrice"]',
            '[class*="price-section"]', '.price-section',
        ]) or "")
    ) or extract_price(full_text)
    if price and _price_valid(price):
        result["price"] = price

    miles_text = _text_first(soup, [
        '[data-cmp="mileage"]', '[class*="mileage"]', '[class*="Mileage"]',
    ]) or ""
    miles = extract_miles(miles_text) or extract_miles(full_text)
    if miles:
        result["miles"] = miles

    color = None
    for el in soup.find_all(string=re.compile(r"exterior\s*col", re.I)):
        parent = el.find_parent()
        if parent:
            sib = parent.find_next_sibling()
            if sib:
                color = detect_color(sib.get_text(strip=True))
                if color:
                    break
    if not color:
        color = detect_color(full_text)
    if color:
        result["color"] = color

    for el in soup.find_all(string=re.compile(r"battery\s*(capacity)?", re.I)):
        parent = el.find_parent()
        if parent:
            sib = parent.find_next_sibling()
            if sib:
                kwh_m = re.search(r"(98|131)[\s-]?kwh", sib.get_text(strip=True), re.I)
                if kwh_m:
                    _bat_kwh = int(kwh_m.group(1))
                    if _bat_kwh == 131:
                        result["extended_range_confirmed"] = True
                        result["er_note"] = "VDP battery: 131 kWh"
                    elif _bat_kwh == 98:
                        result["extended_range_confirmed"] = False
                        result["er_note"] = "VDP battery: 98 kWh (SR)"
                    break

    title_el = soup.find("title")
    title_text = title_el.get_text(" ", strip=True) if title_el else ""
    trim_m = re.search(r'\bLightning\s+(\w+)', title_text, re.I)
    if trim_m:
        result["trim"] = trim_m.group(1)

    dealer_el = (
        soup.find(attrs={"data-cmp": "dealerInfo"})
        or soup.find(class_=re.compile(r"dealer", re.I))
    )
    if dealer_el:
        dealer_text = dealer_el.get_text("\n", strip=True)
        parts = [p.strip() for p in dealer_text.splitlines() if p.strip()]
        if parts:
            dealer_name = parts[0]
            city_state = ""
            for part in parts[1:]:
                m = re.search(r'([A-Za-z][\w\s.\-]+),\s*([A-Z]{2})\b', part)
                if m:
                    city_state = f"{m.group(1).strip()}, {m.group(2)}"
                    break
            result["location"] = (
                f"{dealer_name} · {city_state}" if city_state else dealer_name
            )

    hist_flag, hist_note = parse_history(full_text)
    if hist_flag != "❓ Unknown":
        result["history_flag"] = hist_flag
        result["history_note"] = hist_note

    return result
