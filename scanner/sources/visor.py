"""scanner/sources/visor.py -- Visor.vin sweep."""
import asyncio
import json
import time
from typing import Optional

from scanner.config import log
from scanner.detect import detect_er, detect_511a, detect_azure_gray
from scanner.detect import _vin_battery
from scanner.models import _build_listing
from scanner.browser import _quiet_exception_handler


_VISOR_LISTINGS_URL = "https://freeway.visor.vin/api/v1/listings"
_VISOR_VIN_URL      = "https://freeway.visor.vin/api/v1/vin/{vin}"
_VISOR_HEADERS      = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept":          "application/json, */*;q=0.9",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://visor.vin/",
    "Origin":          "https://visor.vin",
}


async def _nd_visor_fetch(url: str, wait_ms: int = 10_000) -> Optional[str]:
    """Navigate to a Visor API URL with real Chrome and return body text.

    Uses document.body.innerText rather than tab.get_content() because Chrome
    renders JSON API responses through its built-in viewer; innerText gives the
    raw JSON string reliably.
    """
    import nodriver as uc
    browser = None
    try:
        browser = await uc.start(
            headless=False,
            browser_args=["--disable-http2", "--no-sandbox"],
            sandbox=False,
        )
        tab = await asyncio.wait_for(browser.get(url), timeout=60.0)
        await asyncio.sleep(wait_ms / 1000)
        text = await tab.evaluate("document.body.innerText")
        return text if text else None
    except Exception as exc:
        log.warning("Visor: nodriver fetch failed for %s: %s", url, exc)
        return None
    finally:
        if browser:
            try:
                await browser.stop()
            except Exception:
                pass


def _visor_pw_json(url: str, wait_ms: int = 10_000) -> Optional[dict]:
    """Synchronous wrapper around _nd_visor_fetch. Returns parsed JSON or None.

    Retries once with a 20s wait if Cloudflare challenge is detected on the
    first attempt (the challenge page does not contain 'rows').
    """
    retry_wait_ms = 20_000
    for attempt in range(1, 3):
        loop = asyncio.new_event_loop()
        loop.set_exception_handler(_quiet_exception_handler)
        try:
            text = loop.run_until_complete(_nd_visor_fetch(url, wait_ms=wait_ms))
        finally:
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            try:
                loop.close()
            except Exception:
                pass

        if not text:
            return None
        # Cloudflare challenge pages don't contain "rows"
        if '"rows"' not in text:
            if attempt == 1:
                log.warning(
                    "Visor: Cloudflare not resolved at %dms -- retrying with %dms wait",
                    wait_ms, retry_wait_ms,
                )
                wait_ms = retry_wait_ms
                continue
            log.warning(
                "Visor: response does not contain 'rows' after retry -- "
                "Cloudflare still blocking. First 200 chars: %s", text[:200]
            )
            return None
        try:
            return json.loads(text)
        except Exception as exc:
            log.warning("Visor: JSON parse failed: %s -- text[:200]: %s", exc, text[:200])
            return None
    return None


def sweep_visor() -> list:
    """Fetch 2023-2024 F-150 Lightning Lariat used listings from Visor.vin.

    Uses nodriver (real Chrome) to bypass Cloudflare. Paginates until the
    API returns fewer rows than the page limit. VIN detail enrichment (color,
    dealer VDP URL) is skipped -- those endpoints are also Cloudflare-protected
    and color is a non-critical field. VIN is sufficient for ER detection.
    """
    from urllib.parse import urlencode

    log.info("Visor sweep (nodriver)...")
    all_rows: dict = {}   # vin -> row dict

    offset = 0
    limit  = 100
    while True:
        url = (
            _VISOR_LISTINGS_URL + "?"
            + urlencode([
                ("make",     "Ford"),
                ("model",    "F_150-Lightning"),
                ("year[]",   "2023"),
                ("year[]",   "2024"),
                ("trim",     "Lariat"),
                ("car_type", "used"),
                ("sort",     "newest"),
                ("limit",    str(limit)),
                ("offset",   str(offset)),
            ])
        )
        data = _visor_pw_json(url)
        if data is None:
            log.warning("Visor: failed to fetch listings at offset=%d -- stopping", offset)
            break

        rows = data.get("rows") or []
        log.info("Visor: fetched %d rows at offset %d", len(rows), offset)

        for row in rows:
            vin = str(row.get("vin") or "").upper().strip()
            if not vin or len(vin) < 4:
                continue

            # Early SR reject -- avoid window sticker fetch for obvious SR VINs.
            if _vin_battery(vin) == "SR":
                continue

            year = int(row.get("year") or 0)
            if year not in (2023, 2024):
                continue

            price = row.get("price")
            if price and int(price) > 65_000:
                continue

            dealer_name = row.get("dealerName") or ""
            city        = row.get("city") or ""
            state_code  = row.get("state") or ""
            location    = f"{dealer_name} · {city}, {state_code}".strip(" ·,")
            miles       = row.get("miles")
            dos         = row.get("dosActive")

            all_rows[vin] = {
                "uid":      f"visor-{vin}",
                "vin":      vin,
                "year":     year,
                "title":    f"{year} Ford F-150 Lightning Lariat",
                "price":    int(price) if price else None,
                "miles":    int(miles) if miles else None,
                "location": location,
                "dos":      dos,
            }

        if len(rows) < limit:
            break   # last page
        offset += limit
        time.sleep(2.0)   # pause between browser launches

    if not all_rows:
        log.warning("Visor: 0 listings found")
        return []

    log.info("Visor: %d unique VINs -- building listings...", len(all_rows))

    results = []
    for vin, row in all_rows.items():
        title = row["title"]

        er_confirmed, er_note = detect_er(vin, title)
        if er_confirmed is False:
            continue

        equip_confirmed, equip_note = detect_511a(title)
        hist_flag, hist_note = "? Unknown", ""
        is_azure = detect_azure_gray(title)
        link = f"https://visor.vin/search/Ford/F_150-Lightning/listings?listing={vin}"

        results.append(_build_listing(
            uid=row["uid"], source="visor", vin=vin,
            title=title, color="",
            location=row["location"], seller_type="Dealer",
            miles=row["miles"], price=row["price"],
            hist_flag=hist_flag, hist_note=hist_note,
            er_confirmed=er_confirmed, er_note=er_note,
            equip_confirmed=equip_confirmed, equip_note=equip_note,
            is_azure=is_azure, link=link,
            dos_active=row.get("dos"),
        ))

    log.info("Visor: %d listings parsed (after ER filter)", len(results))
    return results
