"""scanner/detect.py -- VIN/text detection helpers."""
import hashlib
import re
from datetime import date
from typing import Optional

import requests
from bs4 import BeautifulSoup

from scanner.config import (
    log,
    _PW_UA,
    _VIN_ER_CHAR, _VIN_SR_CHAR,
    _ER_NEGATIVE, _ER_STRONG, _ER_HINT,
    _PATTERNS_511A,
    _AZURE_GRAY, _KNOWN_COLORS,
    _PRICE_MIN, _PRICE_MAX,
    NEEDS_BACKFILL_FIELDS,
)


def _vin_battery(vin: str) -> Optional[str]:
    """Return 'ER', 'SR', or None from VIN position 4 (index 3).

    '6' at VIN[3] -> Extended Range 131 kWh
    'V' at VIN[3] -> Standard Range  98 kWh
    """
    if not vin or len(vin) < 4:
        return None
    c = vin[3].upper()
    if c == _VIN_ER_CHAR:
        return "ER"
    if c == _VIN_SR_CHAR:
        return "SR"
    return None


def _fetch_window_sticker_er(vin: str) -> tuple:
    """Fetch Ford Monroney window sticker PDF and return (er_confirmed, note).

    URL: https://www.windowsticker.forddirect.com/windowsticker.pdf?vin={VIN}
    Returns:
        (True,  'window sticker: 511A')              -- Extended Range confirmed
        (False, 'window sticker: 510A')              -- Standard Range confirmed
        (True,  'window sticker: extended range bat') -- ER from battery label
        (False, 'window sticker: standard range bat') -- SR from battery label
        (None,  '')                                  -- unavailable / inconclusive
    """
    try:
        import io
        import pdfplumber
        url = f"https://www.windowsticker.forddirect.com/windowsticker.pdf?vin={vin}"
        resp = requests.get(url, timeout=15, headers={"User-Agent": _PW_UA})
        if resp.status_code != 200 or "pdf" not in resp.headers.get("Content-Type", ""):
            return None, ""
        with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
            text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        # Primary: equipment group code (authoritative)
        if re.search(r"EQUIPMENT\s+GROUP\s+511\s*[-]?\s*A", text, re.I):
            return True, "window sticker: 511A"
        if re.search(r"EQUIPMENT\s+GROUP\s+510\s*[-]?\s*A", text, re.I):
            return False, "window sticker: 510A"
        # Fallback: battery label in the vehicle description block
        if re.search(r"EXTND\s+RANGE\s+BAT", text, re.I):
            return True, "window sticker: extended range bat"
        if re.search(r"STANDARD\s+RANGE\s+BAT|STD\s+RANGE\s+BAT", text, re.I):
            return False, "window sticker: standard range bat"
        return None, ""
    except Exception:
        return None, ""


def _apply_vin_er(entry: dict) -> None:
    """Update extended_range_confirmed from VIN position 4 (index 3) if not already set.

    If ER is already confirmed by a stronger signal (VDP text, battery capacity),
    logs a WARNING on conflict rather than overwriting.
    Modifies entry in-place; safe to call multiple times.
    """
    vin = entry.get("vin", "")
    if not vin:
        return
    bat = _vin_battery(vin)
    if bat is None:
        return  # VIN too short or unrecognised character at position 4

    er_from_vin = (bat == "ER")
    existing_er = entry.get("extended_range_confirmed")

    if existing_er is None:
        entry["extended_range_confirmed"] = er_from_vin
        entry["er_note"] = "VIN position 4 (%s)" % bat
    elif existing_er != er_from_vin:
        # VIN is authoritative for SR. A truck with VIN[3]='V' cannot be ER.
        # Exception: window sticker is the only source that outranks VIN pos-4
        # (it is a scanned document with the VIN on it -- a conflict there needs
        # human review, so we leave it alone).
        if not er_from_vin and (entry.get("er_note") or "").startswith("window sticker"):
            log.warning(
                "VIN pos-4 says SR for %s but window sticker says ER -- keeping "
                "window sticker (investigate manually). VIN=%s, note=%s",
                entry.get("id", vin[:12]), vin, entry.get("er_note"),
            )
        elif not er_from_vin:
            # VIN says SR; prior ER flag came from text or a stale VIN -- override.
            log.warning(
                "VIN pos-4 SR override for %s: was er_confirmed=%s (%s), VIN=%s says SR -- correcting",
                entry.get("id", vin[:12]),
                "ER" if existing_er else "SR",
                entry.get("er_note", "?"),
                vin,
            )
            entry["extended_range_confirmed"] = False
            entry["er_note"] = "corrected: VIN[3]=%s (SR) overrides prior %r" % (
                vin[3], entry.get("er_note", ""),
            )
        else:
            # VIN says ER but existing says SR -- ER truck mistakenly flagged SR.
            # Less certain; log only, don't override.
            log.warning(
                "VIN pos-4 conflict for %s: VIN says ER but entry has er_confirmed=SR (%s) -- keeping SR",
                entry.get("id", vin[:12]), entry.get("er_note", "?"),
            )


def detect_er(vin: str, text: str) -> tuple:
    """
    Returns (confirmed: bool|None, note: str).
      True  = confirmed Extended Range
      False = confirmed Standard Range (caller should exclude)
      None  = unconfirmed (include with flag)

    Resolution order:
      1. Authoritative: VIN position 4 ('6'=ER, 'V'=SR).  Always wins.
      2. Strong text: 131 kWh / ER battery label / 511A code.  Confirms if no VIN
         and no negative signal.
      3. Negative text: explicit SR label, 98 kWh, 510A.  Wins over any hint,
         and wins over strong when both appear (explicit contradiction takes priority).
      4. Hint text: feature mentions (bluecruise, moonroof, etc.).  Raises likelihood
         but never confirms alone.
      5. No signal: unconfirmed.
    """
    bat = _vin_battery(vin or "")
    if bat == "ER":
        return True, "VIN-confirmed ER"
    if bat == "SR":
        return False, "SR"

    t = text or ""
    has_neg    = any(p.search(t) for p in _ER_NEGATIVE)
    has_strong = any(p.search(t) for p in _ER_STRONG)
    has_hint   = any(p.search(t) for p in _ER_HINT)

    # Explicit negative always wins -- a clear SR label beats any positive signal.
    if has_neg:
        return False, "SR"

    # Strong signal (131 kWh, ER battery label, 511A) confirms without VIN.
    if has_strong:
        return True, "text-confirmed ER"

    # Hints raise likelihood but cannot confirm alone.
    if has_hint:
        return None, "text suggests ER (unconfirmed)"

    return None, "Unconfirmed"


def detect_511a(text: str) -> tuple:
    """Returns (confirmed: bool|None, note: str)."""
    t = text or ""
    if re.search(r"511\s*[-]?\s*A", t, re.I):
        return True, "511A explicit"
    hits = sum(1 for p in _PATTERNS_511A if p.search(t))
    if hits >= 2:
        return True, f"{hits} keyword signals"
    if hits == 1:
        return None, "1 signal (unconfirmed)"
    return None, "Unconfirmed"


def detect_azure_gray(text: str) -> bool:
    return bool(_AZURE_GRAY.search(text or ""))


def extract_price(text: str) -> Optional[int]:
    t = text or ""
    # Dollar-sign format: $42,995 or $42995
    m = re.search(r"\$\s*([\d,]+)", t)
    if m:
        try:
            return int(m.group(1).replace(",", ""))
        except ValueError:
            pass
    # AT format: 42,995 without dollar sign (e.g. "42,995 See payment")
    # Match XX,XXX or XXX,XXX -- 2-3 digits, comma, 3 digits -- in vehicle price range
    m = re.search(r"\b(\d{2,3}),(\d{3})\b", t)
    if m:
        try:
            val = int(m.group(1) + m.group(2))
            if 15_000 <= val <= 199_000:   # sanity: realistic vehicle price
                return val
        except ValueError:
            pass
    # Bare 5-6 digit number fallback
    m = re.search(r"\b([\d]{5,6})\b", t)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return None


def extract_miles(text: str) -> Optional[int]:
    t = text or ""
    # AT format: "20K mi" or "20k mi" -- number in thousands
    m = re.search(r"\b(\d{1,3})[Kk]\s*mi\b", t)
    if m:
        try:
            val = int(m.group(1)) * 1000
            if 0 <= val <= 500_000:
                return val
        except ValueError:
            pass
    # "Miles: 11,595" or "Mileage: 43,200" (label before number)
    m = re.search(r"(?:miles?|mileage|odometer)\s*:?\s*([\d,]+)", t, re.I)
    if not m:
        # "11,595 mi" or "11,595 miles" (number before label)
        m = re.search(r"([\d,]+)\s*(?:mi(?:les?)?|k\s*mi)\b", t, re.I)
    if m:
        raw = m.group(1).replace(",", "")
        try:
            val = int(raw)
            # sanity-check: ignore implausible values
            if val > 500_000 or val < 0:
                return None
            return val
        except ValueError:
            pass
    return None


def detect_color(text: str) -> Optional[str]:
    for color in _KNOWN_COLORS:
        if re.search(re.escape(color), text or "", re.I):
            return color
    return None


def _price_valid(price) -> bool:
    return price is not None and _PRICE_MIN <= price <= _PRICE_MAX


def _is_trim_excluded(title: str, price: Optional[int] = None) -> bool:
    """Return True if listing should be excluded by trim filter.
    Pass price=None at card/sweep stage to defer 2023 Platinum check to VDP stage."""
    t = title or ""
    # "Pro" trim: exclude when "Pro" appears as a standalone word, UNLESS it's
    # "Pro Power"/"ProPower" (a feature/generator option, not a trim designation).
    if re.search(r"\bPro\b", t, re.I) and not re.search(r"\bPro\s*Power\b", t, re.I):
        return True
    # Other non-Lariat trims: Platinum, Flash, XLT
    if not re.search(r"\b(Platinum|Flash|XLT)\b", t, re.I):
        return False
    if re.search(r"\bLariat\b", t, re.I):
        return False
    # Exception: 2023 Platinum under $50k passes through
    if re.search(r"\bPlatinum\b", t, re.I):
        year_m = re.search(r"\b(20\d\d)\b", t)
        if year_m and int(year_m.group(1)) == 2023:
            if price is None:
                return False  # defer to VDP stage for confirmed price
            if _price_valid(price) and price < 50_000:
                return False
    return True


def parse_history(text: str) -> tuple:
    t = (text or "").lower()
    # Salvage / rebuilt title -- flagged separately from buyback; treated as harshest category.
    # Check before the generic buyback block so "salvage" doesn't fall into it.
    if re.search(r"\bsalvage\b|rebuilt\s+title", t):
        note = "salvage" if "salvage" in t else "rebuilt title"
        return "\U0001f6ab Salvage", note
    if re.search(r"buyback|lemon\s*law|flood\s+damage|title\s*issue", t):
        note = next(
            (kw for kw in ("buyback", "lemon law", "flood", "title issue") if kw in t),
            "title issue",
        )
        return "\U0001f6ab Buyback", note
    # Only flag accident for explicit structured disclosures -- not boilerplate disclaimer text.
    # Negative lookahead excludes "no accidents reported" matches.
    if re.search(
        r"\b\d+\s+accident[s]?\s+reported"
        r"|(?<!no\s)(?<!no\s\s)\baccident[s]?\s+reported\b"
        r"|\breported\s+accident\b"
        r"|autocheck[^.]{0,80}accident"
        r"|carfax[^.]{0,80}accident"
        r"|accident[^.]{0,80}(?:carfax|autocheck)",
        t,
    ):
        return "⚠️ Accident", "reported"
    if re.search(r"clean\s+title|no\s+accident|no\s+reported|1-owner|one[\s-]owner", t, re.I):
        return "✅ Clean", ""
    return "❓ Unknown", ""


def parse_vdp_history(text: str) -> tuple:
    """Richer history extraction for full VDP page text.

    More permissive than parse_history() -- designed for structured VDP content
    where explicit vehicle-history signals appear in context paragraphs.
    Returns (flag, note) where note carries the matched signal phrase for logging.

    Priority (highest to lowest): Salvage > Buyback > Accident > Clean > Unknown.
    Informational-only signals ("1 owner", "personal use") are captured in note
    without changing the flag when no other signal is present.

    NOTE: Clean check runs before the Accident check.  "0 accidents reported
    by AutoCheck" is a clean signal, not an accident signal -- if clean is
    detected first we short-circuit so the accident patterns never see it.
    """
    t = (text or "").lower()

    # -- Severity-4: Salvage / rebuilt title --------------------------------------
    if re.search(r"\bsalvage\b|rebuilt\s+title", t):
        note = "salvage" if "salvage" in t else "rebuilt title"
        return "\U0001f6ab Salvage", note

    # -- Severity-3: Buyback / lemon law ------------------------------------------
    if re.search(r"lemon\s*law|manufacturer\s+buyback|\bbuyback\b|flood\s+damage|title\s*issue", t):
        kw = next(
            (k for k in ("lemon law", "manufacturer buyback", "buyback", "flood", "title issue")
             if k in t),
            "title issue",
        )
        return "\U0001f6ab Buyback", kw

    # -- Severity-1 (Clean): explicit no-accident / clean statements --------------
    # Checked BEFORE the accident patterns so "0 accidents reported by AutoCheck"
    # resolves as Clean rather than triggering the accident[...]autocheck pattern.
    clean_m = re.search(
        r"\b0\s+accidents?\b"                             # "0 accidents"
        r"|zero\s+accidents?\b"                           # "zero accidents"
        r"|no\s+accidents?\s+(?:reported|found|on\s+record|history)"
        r"|\bno\s+accident\s+history\b"
        r"|no\s+reported\s+accidents?"                    # "no reported accidents"
        r"|no\s+damage\s+reported"
        r"|clean\s+(?:title|history|record|vehicle\s+history)"
        r"|no\s+accident\b",                              # catch-all for "no accident"
        t,
    )
    if clean_m:
        # Guard: if there is ALSO an explicit non-zero accident count, accident wins.
        # (e.g. "1 accident reported -- no additional accidents found" -> Accident)
        # We only guard against unambiguous non-zero counts, not "reported accidents"
        # which is ambiguous ("no reported accidents" is clean).
        if not re.search(r"\b[1-9]\d*\s+accidents?\b|\bcollision\s+reported\b", t):
            return "✅ Clean", clean_m.group(0).strip()

    # -- Severity-2: Accident / collision -----------------------------------------
    # "N accidents" where N > 0, "collision reported", or CARFAX/AutoCheck disclosures.
    if re.search(
        r"\b[1-9]\d*\s+accidents?\b"                       # "1 accident", "2 accidents"
        r"|(?<!no\s)(?<!0\s)(?<!zero\s)\baccidents?\s+reported\b"
        r"|\breported\s+accidents?\b"
        r"|\bcollision\s+reported\b"
        r"|\bdamage\s+reported\b"
        r"|\baccident\s+reported\b"
        r"|carfax[^.]{0,100}(?:accident|collision)"
        r"|(?<!\bno\s)(?<!\b0\s)\baccident[^.]{0,80}(?:carfax|autocheck)",
        t,
    ):
        return "⚠️ Accident", "reported"

    # -- Informational-only signals -----------------------------------------------
    # "1 owner" / "personal use" don't change the flag but are useful notes.
    info_notes = []
    if re.search(r"\b1\s+owner\b|one\s+owner|single\s+owner|\b1-owner\b", t):
        info_notes.append("1 owner")
    if re.search(r"\bpersonal\s+use\b", t):
        info_notes.append("personal use")
    if info_notes:
        return "❓ Unknown", "; ".join(info_notes)

    return "❓ Unknown", ""


def parse_autocheck_panel(text: str) -> tuple:
    """Parse the structured AutoCheck summary box on cars.com VDPs."""
    t = (text or "").lower()
    if not t or "autocheck" not in t:
        return None, None
    if re.search(r"\bsalvage\b|rebuilt\s+title", t):
        return "\U0001f6ab Salvage", "salvage (autocheck)"
    if "title issue" in t:
        return "\U0001f6ab Buyback", "title issue (autocheck)"
    if re.search(
        r"\b0\s+accidents?\b|zero\s+accidents?\b"
        r"|no\s+accidents?\b|no\s+accident\s+or\s+damage|no\s+accidents\s+or\s+damage",
        t,
    ):
        return "✅ Clean", "0 accidents (autocheck)"
    if re.search(r"\b[1-9]\d*\s+accidents?\b|\baccident\b|\bdamage\s+reported\b", t):
        return "⚠️ Accident", "reported (autocheck)"
    return None, None


def needs_backfill(entry: dict) -> bool:
    """Return True if this listing is missing key fields that should be re-fetched."""
    if not _price_valid(entry.get("price")):
        return True
    for field in NEEDS_BACKFILL_FIELDS:
        if not entry.get(field):
            return True
    return False


def detect_seller_type(text: str) -> str:
    t = (text or "").lower()
    if re.search(r"certified\s+pre.?owned|cpo|ford\s+gold\s+certified|ford\s+ev\s+certified", t):
        return "CPO"
    if re.search(r"private\s+(?:seller|party)|by\s+owner|individual\s+seller", t):
        return "Private"
    return "Dealer"


def _text_first(soup: BeautifulSoup, selectors: list, sep: str = " ") -> Optional[str]:
    for sel in selectors:
        el = soup.select_one(sel)
        if el:
            return el.get_text(sep, strip=True)
    return None


def _url_hash(url: str) -> str:
    return hashlib.md5(url.encode()).hexdigest()[:12]
