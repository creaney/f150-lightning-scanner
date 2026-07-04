"""scanner/sources/ebay.py -- eBay Motors sweep."""
import re
import time
from typing import Optional

from bs4 import BeautifulSoup

from scanner.config import log, S, _EBAY_BASE, EBAY_KEYWORDS, _VIN_RE
from scanner.detect import (
    detect_er, detect_511a, detect_color, detect_azure_gray,
    extract_price, extract_miles, parse_history, _is_trim_excluded,
)
from scanner.models import _extract_ld_json, _build_listing
from scanner.browser import _pw_fetch


def _ebay_parse_cards(soup: BeautifulSoup) -> list:
    """Parse eBay search result cards from a soup object."""
    cards = soup.select("li.s-card[data-listingid]") or soup.select("li[data-listingid]")
    results = []
    for card in cards:
        listing_id = card.get("data-listingid", "")

        # Real item link (not the ebay.com/itm/123456 placeholder)
        real_link = card.find("a", href=re.compile(r"www\.ebay\.com/itm/\d+"))
        if not real_link:
            continue
        href = real_link.get("href", "")
        item_id = _ebay_item_id(href)
        if not item_id:
            continue

        # Title and all text from card
        full_text = card.get_text(" ", strip=True)
        # Title is the first sentence before "Opens in a new window"
        title = re.split(r"\s*Opens in", full_text)[0].strip()
        title = re.sub(r"\s+", " ", title)

        # Skip non-Lightning and placeholder listings
        if not re.search(r"lightning", title, re.I):
            continue
        if "Shop on eBay" in title:
            continue

        # Exclude non-Lariat trims (XLT, Pro, Flash, Platinum) when explicitly stated
        if _is_trim_excluded(title):
            log.debug("Excluding non-Lariat trim: %s", title[:60])
            continue

        # Strict year filter -- title must explicitly contain 2023 or 2024
        if not re.search(r"\b202[34]\b", title):
            log.debug("eBay: no 2023/2024 in title, skipping: %s", title[:60])
            continue

        price = extract_price(full_text)
        if price and price < 28_000:
            log.debug("eBay: skipping %s -- price $%s below $28k floor", item_id, f"{price:,}")
            continue

        # Location: text after "Located in"
        loc_m = re.search(r"Located in\s+(.+?)(?:\s+Delivery|\s+Year:|\s+Miles:|\s*$)", full_text)
        location = loc_m.group(1).strip() if loc_m else "United States"

        # Seller hint: text before the title (dealer store name often appears there)
        seller_hint = full_text[len(title):len(title)+120]

        results.append({
            "id":           f"ebay-{item_id}",
            "source":       "ebay",
            "ebay_id":      item_id,
            "title":        title,
            "price":        price,
            "location":     location,
            "_seller_hint": seller_hint,
            "_card_text":   full_text[:500],
            "link":         _clean_ebay_url(href),
            "_partial":     True,
        })

    return results


def sweep_ebay() -> list:
    """Three-keyword eBay sweep, deduplicated by item ID."""
    log.info("eBay Motors sweep (%d keyword searches)...", len(EBAY_KEYWORDS))
    seen_ids: set = set()
    all_results: list = []

    for kw in EBAY_KEYWORDS:
        url = _EBAY_BASE + kw
        # wait_selector='li.s-card' waits up to 30s for real page to load past the bot challenge;
        # wait_ms=12000 adds 12s fallback -- 42s total covers eBay's JS challenge timing.
        content = _pw_fetch(url, wait_ms=12000, wait_selector='li.s-card')
        if not content:
            log.info("eBay: retrying kw=%r after Playwright failure", kw)
            time.sleep(3)
            content = _pw_fetch(url, wait_ms=12000, wait_selector='li.s-card')
            if not content:
                log.warning("eBay: Playwright fetch failed for kw=%r (gave up after retry)", kw)
                continue
        soup = BeautifulSoup(content, "lxml")
        batch = _ebay_parse_cards(soup)
        new = [r for r in batch if r.get("ebay_id") not in seen_ids]
        for r in new:
            seen_ids.add(r["ebay_id"])
        all_results.extend(new)
        log.info("eBay kw=%r: %d cards, %d new", kw, len(batch), len(new))

    log.info("eBay: %d total unique listings", len(all_results))
    return all_results


def visit_ebay_item(item_id: str, partial: dict) -> Optional[dict]:
    """Fetch eBay item page for VIN, mileage, full seller info.
    Tries Playwright first (handles bot detection), falls back to requests."""
    url = f"https://www.ebay.com/itm/{item_id}"

    content = _pw_fetch(url, wait_ms=3000, wait_selector='h1.x-item-title__mainTitle')
    # Bot-blocked pages return an ~11 KB shell -- treat as failed fetch
    if content and len(content) < 50_000:
        log.debug("eBay item page too small (%d bytes), likely bot-blocked; falling back", len(content))
        content = None
    if not content:
        try:
            r = S.get(url, timeout=20)
            r.raise_for_status()
            content = r.text
        except Exception as exc:
            log.warning("eBay item fetch error (%s): %s", item_id, exc)
            return _ebay_from_card_text(item_id, partial, url)
    # Still too small after requests fallback -> give up and use card data
    if len(content) < 50_000:
        log.debug("eBay item page still too small after requests fallback; using card data")
        return _ebay_from_card_text(item_id, partial, url)

    soup = BeautifulSoup(content, "lxml")
    full_text = soup.get_text(" ", strip=True)

    ld = _extract_ld_json(soup, types=("Car", "Vehicle", "Product"))

    vin_m = _VIN_RE.search(full_text)
    vin = ld.get("vehicleIdentificationNumber") or (vin_m.group(1) if vin_m else "")

    # Mileage -- look in item specifics table
    miles = None
    for row in soup.select(".ux-layout-section, .section-expansion-contents"):
        t = row.get_text(" ")
        if re.search(r"mileage|odometer", t, re.I):
            miles = extract_miles(t)
            break
    if not miles:
        miles = extract_miles(full_text[:2000])

    price_el = soup.select_one(".x-price-primary, #prcIsum, .notranslate.bold, [itemprop='price']")
    price = extract_price(price_el.get_text(strip=True) if price_el else "") or partial.get("price")

    color = ld.get("color") or ld.get("vehicleColor") or detect_color(full_text)

    # eBay seller type: store = Dealer, individual = Private
    store_el = soup.select_one(".ux-seller-section, [data-testid='x-seller-info']")
    store_text = store_el.get_text(strip=True) if store_el else ""
    seller_type = _ebay_seller_type(store_text, partial.get("_seller_hint", ""))

    er_confirmed, er_note     = detect_er(vin or "", full_text)
    equip_confirmed, equip_note = detect_511a(full_text)
    hist_flag, hist_note       = parse_history(full_text)
    is_azure = detect_azure_gray(color or "") or detect_azure_gray(full_text)

    return _build_listing(
        uid=f"ebay-{item_id}", source="ebay", vin=vin,
        title=partial.get("title", ""),
        color=color, location=partial.get("location", ""),
        seller_type=seller_type, miles=miles, price=price,
        hist_flag=hist_flag, hist_note=hist_note,
        er_confirmed=er_confirmed, er_note=er_note,
        equip_confirmed=equip_confirmed, equip_note=equip_note,
        is_azure=is_azure, link=url,
    )


def _ebay_from_card_text(item_id: str, partial: dict, url: str) -> dict:
    """Build a listing from the eBay search-card text when item page is unavailable."""
    text = partial.get("_card_text", "") or partial.get("title", "")
    er_confirmed, er_note     = detect_er("", text)
    equip_confirmed, equip_note = detect_511a(text)
    miles = extract_miles(text)
    color = detect_color(text)
    return _build_listing(
        uid=f"ebay-{item_id}", source="ebay", vin="",
        title=partial.get("title", ""),
        color=color or "", location=partial.get("location", ""),
        seller_type=_ebay_seller_type("", partial.get("_seller_hint", "")),
        miles=miles, price=partial.get("price"),
        hist_flag=parse_history(text)[0], hist_note=parse_history(text)[1],
        er_confirmed=er_confirmed, er_note=er_note,
        equip_confirmed=equip_confirmed, equip_note=equip_note,
        is_azure=detect_azure_gray(text), link=url,
    )


def _ebay_item_id(url: str) -> Optional[str]:
    m = re.search(r"/itm/(\d+)", url or "")
    return m.group(1) if m else None


def _clean_ebay_url(url: str) -> str:
    m = re.match(r"(https://www\.ebay\.com/itm/\d+)", url or "")
    return m.group(1) if m else url


def _ebay_seller_type(store_text: str, seller_hint: str) -> str:
    combined = (store_text + " " + seller_hint).lower()
    if re.search(r"ford\s+certified|cpo", combined):
        return "CPO"
    # eBay power sellers / stores -> Dealer; regular users -> Private
    if re.search(r"\bstore\b|\bdealer\b|\bmotors\b.*llc|\bauto\b.*sales", combined):
        return "Dealer"
    return "Private"
