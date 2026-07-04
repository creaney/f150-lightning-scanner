"""scanner/sources/carvana.py -- Carvana sweep."""
import random
import re
import time

from bs4 import BeautifulSoup

from scanner.config import log, CARVANA_URL
from scanner.detect import (
    detect_er, detect_511a, detect_color, detect_azure_gray,
    extract_price, extract_miles, parse_history, _is_trim_excluded,
)
from scanner.detect import _url_hash
from scanner.models import _build_listing
from scanner.browser import _pw_fetch


def sweep_carvana() -> list:
    """Fetch Carvana 2023-2024 F-150 Lightning listings via Playwright."""
    log.info("Carvana sweep...")
    # Carvana is a heavy SPA -- try multiple wait strategies to ensure JS renders.
    # Try several wait_selectors that Carvana has used across different page versions.
    _carvana_wait_selectors = [
        "[data-testid='result-tile']",
        "[data-testid='vehicle-card']",
        "[data-qa='vehicle-card']",
        "[data-qa='inventory-card']",
        "a[href*='/vehicle/']",
    ]
    content = None
    for _sel in _carvana_wait_selectors:
        content = _pw_fetch(CARVANA_URL, wait_ms=12000, wait_selector=_sel, headless=False)
        if content:
            soup_test = BeautifulSoup(content, "lxml")
            if soup_test.select(_sel) or soup_test.select("a[href*='/vehicle/']"):
                log.debug("Carvana: got real page with selector %r", _sel)
                break
            content = None
    if not content:
        # Last resort: plain time-based wait, no selector gating
        content = _pw_fetch(CARVANA_URL, wait_ms=18000, headless=False)
    if not content:
        log.warning("Carvana: failed to fetch page")
        return []

    soup = BeautifulSoup(content, "lxml")
    results = []

    # Carvana listing cards -- try every selector variant we've ever seen
    _carvana_card_selectors = [
        "[data-testid='result-tile']",
        "[data-testid='vehicle-card']",
        "[data-testid='vehicle-tile']",
        "[data-qa='vehicle-card']",
        "[data-qa='inventory-card']",
        "[data-qa='vehicle-tile']",
        ".vehicle-card",
        "[class*='VehicleCard']",
        "[class*='vehicleCard']",
        "div[class*='vehicle-card']",
        "div[class*='result-tile']",
        "div[class*='inventory-card']",
    ]
    cards = []
    for _card_sel in _carvana_card_selectors:
        cards = soup.select(_card_sel)
        if cards:
            log.debug("Carvana: matched selector %r (%d cards)", _card_sel, len(cards))
            break

    if not cards:
        # Last resort: any anchor pointing to a /vehicle/ page
        _vehicle_links = soup.select("a[href*='/vehicle/']")
        if _vehicle_links:
            cards = [c.find_parent("div") for c in _vehicle_links if c.find_parent("div")]
            log.debug("Carvana: fell back to vehicle-link parents (%d)", len(cards))

    if not cards:
        _cv_title = soup.find("title")
        _cv_title_text = _cv_title.get_text(strip=True) if _cv_title else "no <title>"
        _cv_vehicle_links = len(soup.select("a[href*='/vehicle/']"))
        _cv_body = soup.get_text(" ", strip=True)[:500]
        log.warning(
            "Carvana: no listing cards found -- page title: %r, vehicle links on page: %d, "
            "body: %s",
            _cv_title_text, _cv_vehicle_links, _cv_body,
        )
        return []

    log.info("Carvana: found %d cards", len(cards))

    for card in cards:
        # Link and ID
        link_el = card.find("a", href=re.compile(r"/vehicle/"))
        if not link_el:
            link_el = card.find("a", href=True)
        if not link_el:
            continue
        href = link_el.get("href", "")
        if not href.startswith("http"):
            href = "https://www.carvana.com" + href
        # Derive a stable ID from the URL slug
        slug_m = re.search(r"/vehicle/([^/?#]+)", href)
        uid = f"carvana-{slug_m.group(1)}" if slug_m else f"carvana-{_url_hash(href)}"

        full_text = card.get_text(" ", strip=True)

        # Title
        title_el = card.find(["h2", "h3", "h4"]) or card.find(class_=re.compile(r"title|year|name", re.I))
        title = title_el.get_text(strip=True) if title_el else ""
        if not title:
            title = re.split(r"\s{3,}|\|", full_text)[0].strip()[:80]
        # Strip Carvana status prefixes ("Purchase in progress", "Recent", "Coming soon", etc.)
        title = re.sub(
            r"^(?:Purchase\s+in\s+progress|Coming\s+soon|Sale\s+pending|Recent|Sold|Reserved)\s+",
            "", title, flags=re.I,
        ).strip()

        # Year filter
        year_m = re.search(r"\b(20\d\d)\b", title or full_text)
        if year_m:
            yr = int(year_m.group(1))
            if yr < 2023 or yr > 2024:
                continue
        else:
            continue  # no recognizable year

        # Trim filter
        if _is_trim_excluded(title):
            continue

        price = extract_price(full_text)
        miles = extract_miles(full_text)
        color = detect_color(full_text)

        results.append({
            "id":       uid,
            "source":   "carvana",
            "title":    title,
            "price":    price,
            "miles":    miles,
            "color":    color or "",
            "location": "Carvana",
            "seller_type": "Dealer",
            "link":     href,
            "_partial": True,
        })
        time.sleep(random.uniform(0.3, 0.8))

    log.info("Carvana: %d listings parsed", len(results))

    # Build full listing entries from partial card data
    final = []
    for p in results:
        uid = p["id"]
        text = p["title"]
        er_confirmed, er_note = detect_er("", text + " " + p.get("color", ""))
        equip_confirmed, equip_note = detect_511a(text)
        hist_flag, hist_note = parse_history(text)
        is_azure = detect_azure_gray(p.get("color", "")) or detect_azure_gray(text)
        entry = _build_listing(
            uid=uid, source="carvana", vin="",
            title=p["title"], color=p.get("color", ""),
            location=p.get("location", "Carvana"),
            seller_type="Dealer",
            miles=p.get("miles"), price=p.get("price"),
            hist_flag=hist_flag, hist_note=hist_note,
            er_confirmed=er_confirmed, er_note=er_note,
            equip_confirmed=equip_confirmed, equip_note=equip_note,
            is_azure=is_azure, link=p["link"],
        )
        final.append(entry)

    return final
