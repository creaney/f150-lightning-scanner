"""scanner/models.py -- listing data structures and builders."""
import json
import re
from typing import Optional

from bs4 import BeautifulSoup

from scanner.detect import (
    detect_er, detect_511a, detect_color, detect_azure_gray, detect_seller_type,
    _vin_battery,
)


def _extract_ld_json(soup: BeautifulSoup, types: tuple = ()) -> dict:
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            if isinstance(data, list):
                data = next((d for d in data if isinstance(d, dict) and d.get("@type") in types), {})
            if isinstance(data, dict) and (not types or data.get("@type") in types):
                return data
        except (json.JSONDecodeError, AttributeError):
            pass
    return {}


def _build_listing(
    uid, source, vin, title, color, location, seller_type,
    miles, price, hist_flag, hist_note, er_confirmed, er_note,
    equip_confirmed, equip_note, is_azure, link,
    dos_active=None,
) -> dict:
    # -- 2023/2024 Lariat 511A auto-confirm ---------------------------------------
    # 2023 Lariat ER = 511A always (Ford required 511A for ER Lariat orders).
    # 2024 Lariat = ER only (Ford dropped SR option) = 511A always.
    _title_l = (title or "").lower()
    _year_m  = re.search(r'\b(2023|2024)\b', title or "")
    _year    = int(_year_m.group(1)) if _year_m else 0
    _is_lariat = "lariat" in _title_l and "platinum" not in _title_l

    if _is_lariat and _year == 2024:
        # 2024 Lariat is ER + 511A by model year (Ford dropped the SR option).
        # This rule is authoritative and overrides text-based ER detection.
        # The only thing that outranks it is the VIN: VIN[3]='V' means SR.
        # A "2024 Lariat" with an SR VIN is a real conflict (suspect title or
        # data entry error), so surface it for review rather than silently
        # forcing ER or silently leaving a false SR label.
        _vin_bat = _vin_battery(vin or "")
        if _vin_bat == "SR":
            er_confirmed = False
            er_note = "CONFLICT: 2024 Lariat title but VIN says SR -- review"
        else:
            # ER VIN confirms it, or no VIN -- year rule is authoritative either way
            if er_confirmed is not True:
                er_confirmed = True
                er_note = "2024 Lariat (ER-only trim)"
            if equip_confirmed is not True:
                equip_confirmed = True
                equip_note = "2024 Lariat (511A always)"

    elif _is_lariat and _year == 2023 and er_confirmed is True:
        # 2023 Lariat ER = 511A always (Ford required 511A for ER Lariat orders).
        if equip_confirmed is not True:
            equip_confirmed = True
            equip_note = "2023 Lariat ER (511A always)"

    return {
        "id":                       uid,
        "source":                   source,
        "vin":                      (vin or "").upper(),
        "title":                    title or "",
        "color":                    color or "",
        "location":                 location or "",
        "seller_type":              seller_type or "Dealer",
        "miles":                    int(miles) if miles else None,
        "price":                    int(price) if price else None,
        "history_flag":             hist_flag,
        "history_note":             hist_note,
        "extended_range_confirmed": er_confirmed,
        "er_note":                  er_note,
        "equipment_511a_confirmed": equip_confirmed,
        "equip_note":               equip_note,
        "azure_gray":               bool(is_azure),
        "year":                     _year if _year else None,
        "link":                     link or "",
        "dos_active":               int(dos_active) if dos_active is not None else None,
    }


def _partial_to_listing(partial: dict, uid: str, source: str, link: str) -> dict:
    """Create a minimal listing from partial search-result data."""
    text = partial.get("title", "") + " " + partial.get("_seller_hint", "")
    er_confirmed, er_note     = detect_er("", text)
    equip_confirmed, equip_note = detect_511a(text)
    return _build_listing(
        uid=uid, source=source, vin="",
        title=partial.get("title", ""),
        color=detect_color(text) or "",
        location=partial.get("location", ""),
        seller_type=detect_seller_type(text),
        miles=partial.get("miles"), price=partial.get("price"),
        hist_flag="❓ Unknown", hist_note="",
        er_confirmed=er_confirmed, er_note=er_note,
        equip_confirmed=equip_confirmed, equip_note=equip_note,
        is_azure=detect_azure_gray(text), link=link,
    )
