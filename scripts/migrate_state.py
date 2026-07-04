#!/usr/bin/env python3
"""
One-shot migrator for the 17 historical boot migrations previously in main().

Run this once to clean up accumulated state artifacts. After a clean run,
every step below should report 0 changes on a second run (idempotency proof).

Usage:
    python3 scripts/migrate_state.py

Always backs up seen-listings.json before making any changes.
"""
import json
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.backup_state import backup
from scanner import (
    ACTIVE, SOLD, _VIN_SR_CHAR, _apply_vin_er, load_state, save_state,
    _fetch_window_sticker_er,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def run_migrations():
    print("Backing up state before migration...")
    backup()

    state = load_state()
    seen  = state["listings"]
    dirty = False
    totals = {}

    # ── 0a. Correct false-ER entries from old VIN position-8 logic ───────────
    n = 0
    for uid, entry in seen.items():
        if entry.get("er_note") != "VIN position 8 (ER)":
            continue
        vin = (entry.get("vin") or "").upper()
        if len(vin) < 4 or vin[3] != _VIN_SR_CHAR:
            continue
        entry["extended_range_confirmed"] = False
        entry["er_note"] = "VIN position 4 (SR)"
        if entry.get("status") not in ("sold", "filtered"):
            entry["status"] = "filtered"
        n += 1
    totals["0a VIN-pos8 false ER"] = n
    if n:
        dirty = True

    # ── 0b. Retroactive VIN position-4 ER detection ──────────────────────────
    n = 0
    for entry in seen.values():
        vin = (entry.get("vin") or "").upper()
        if not vin or len(vin) < 4:
            continue
        needs = (
            entry.get("extended_range_confirmed") is None
            or "position 8" in (entry.get("er_note") or "")
        )
        if not needs:
            continue
        entry["extended_range_confirmed"] = None  # let _apply_vin_er write cleanly
        _apply_vin_er(entry)
        if entry.get("extended_range_confirmed") is not None:
            n += 1
    totals["0b retroactive VIN pos-4"] = n
    if n:
        dirty = True

    # ── 0c. Pre-seed AT vdp_fail_count to 2 ──────────────────────────────────
    n = 0
    for cid, entry in seen.items():
        if not cid.startswith("at-"):
            continue
        if entry.get("status") in ("filtered", SOLD) or entry.get("dismissed"):
            continue
        if entry.get("vdp_fail_count", 0) < 2:
            entry["vdp_fail_count"] = 2
            n += 1
    totals["0c AT vdp_fail_count pre-seed"] = n
    if n:
        dirty = True

    # ── 0e. Un-filter AT shells that have search-result data ─────────────────
    n = 0
    for cid, entry in seen.items():
        if not cid.startswith("at-"):
            continue
        if entry.get("status") != "filtered":
            continue
        if entry.get("dismissed"):
            continue
        if not entry.get("vdp_fail_count"):
            continue
        if not (entry.get("price") or entry.get("location")):
            continue
        entry["status"] = ACTIVE
        n += 1
    totals["0e AT un-filter shells"] = n
    if n:
        dirty = True

    # ── 0f. Correct Edmunds 240mi entries mislabeled as ER ───────────────────
    n = 0
    for entry in seen.values():
        if entry.get("er_note") != "Edmunds electricityRange=240mi":
            continue
        if entry.get("extended_range_confirmed") is not True:
            continue
        entry["extended_range_confirmed"] = False
        entry["er_note"] = "Edmunds electricityRange=240mi (SR)"
        n += 1
    totals["0f Edmunds 240mi SR"] = n
    if n:
        dirty = True

    # ── 0g. Clear false lemon flags set by hotfix-21 ─────────────────────────
    n = 0
    for entry in seen.values():
        if entry.get("history_note") == "lemon (autocheck)":
            entry["history_flag"] = "Unknown"
            entry["history_note"] = ""
            n += 1
    totals["0g false lemon flags"] = n
    if n:
        dirty = True

    # ── 0i. Fix AT miles corruption (DMA overwrite) ───────────────────────────
    n_fix = n_clr = 0
    for cid, entry in seen.items():
        if not (cid.startswith("at-") or
                entry.get("source", "").lower() == "autotrader"):
            continue
        m = entry.get("miles")
        if m and isinstance(m, (int, float)):
            if 0 < m < 1000:
                entry["miles"] = int(m) * 1000
                n_fix += 1
            elif 1000 <= m < 5000:
                entry["miles"] = None
                n_clr += 1
    totals["0i AT miles (x1000)"] = n_fix
    totals["0i AT miles (clear DMA)"] = n_clr
    if n_fix or n_clr:
        dirty = True

    # ── 0j. Re-extract AT dealer names from long location strings ─────────────
    n = 0
    for cid, entry in seen.items():
        if not (cid.startswith("at-") or
                entry.get("source", "").lower() == "autotrader"):
            continue
        loc = entry.get("location") or ""
        if not loc or len(loc) <= 60:
            continue
        segs = re.split(
            r"(?:No Accidents|Excellent|Good Deal|Great Deal|Fair Deal|"
            r"EV Battery|See payment|Dealer Fees|Electric|Hybrid|"
            r"\d+[Kk]\s*mi\b|[\d,]{4,})",
            loc, flags=re.I,
        )
        dealer = segs[-1].strip()[:60] if segs else ""
        if dealer and dealer != loc:
            entry["location"] = dealer
            n += 1
    totals["0j AT location re-extract"] = n
    if n:
        dirty = True

    # ── 0k. Restore AT listings filtered by Chrome storm (HF-30) ─────────────
    n = 0
    for cid, entry in seen.items():
        if not (cid.startswith("at-") or
                entry.get("source", "").lower() == "autotrader"):
            continue
        if entry.get("dismissed"):
            continue
        if (entry.get("vdp_fail_count", 0) >= 3 and
                entry.get("status") == "filtered"):
            entry["vdp_fail_count"] = 0
            entry["status"] = ACTIVE
            n += 1
    totals["0k AT Chrome storm restore"] = n
    if n:
        dirty = True

    # ── 0l. Clear absurd miles values (>200k) ────────────────────────────────
    n = 0
    for entry in seen.values():
        m = entry.get("miles")
        if m and isinstance(m, (int, float)) and m > 200_000:
            entry["miles"] = None
            n += 1
    totals["0l absurd miles"] = n
    if n:
        dirty = True

    # ── 0m. Sync azure_gray from color field ─────────────────────────────────
    n = 0
    for entry in seen.values():
        if entry.get("azure_gray"):
            continue
        if "azure gray" in (entry.get("color") or "").lower():
            entry["azure_gray"] = True
            n += 1
    totals["0m azure_gray sync"] = n
    if n:
        dirty = True

    # ── 0n. AT title: append 'Lariat' to ER-confirmed listings without trim ──
    n = 0
    for cid, entry in seen.items():
        if not (cid.startswith("at-") or
                entry.get("source", "").lower() == "autotrader"):
            continue
        if entry.get("extended_range_confirmed") is not True:
            continue
        t = entry.get("title", "")
        if "lariat" in t.lower():
            continue
        ym = re.search(r"\b(2023|2024)\b", t)
        if not ym:
            continue
        entry["title"] = t.rstrip() + " Lariat"
        n += 1
    totals["0n AT title Lariat append"] = n
    if n:
        dirty = True

    # ── 0o. Filter pre-HF32 AT listings with unknown trim ────────────────────
    n = 0
    for cid, entry in seen.items():
        if not (cid.startswith("at-") or
                entry.get("source", "").lower() == "autotrader"):
            continue
        if entry.get("status") != ACTIVE:
            continue
        if entry.get("dismissed"):
            continue
        t = (entry.get("title") or "").lower()
        has_trim = any(w in t for w in ("lariat", "platinum", "flash", "xlt", "pro"))
        if has_trim:
            continue
        entry["status"] = "filtered"
        entry["vdp_fail_count"] = 0
        entry["sweep_miss_count"] = 0
        n += 1
    totals["0o AT trimless filter"] = n
    if n:
        dirty = True

    # ── 0h. Auto-confirm 511A for 2023 Lariat ER and all 2024 Lariat ─────────
    # Runs AFTER 0n so updated titles are available.
    n511 = ner24 = 0
    for entry in seen.values():
        t    = (entry.get("title") or "").lower()
        ym   = re.search(r"\b(2023|2024)\b", entry.get("title") or "")
        yr   = int(ym.group(1)) if ym else 0
        is_lariat = "lariat" in t and "platinum" not in t
        if not is_lariat:
            continue
        if entry.get("dismissed") or entry.get("status") == "sold":
            continue
        if yr == 2024:
            if entry.get("extended_range_confirmed") is None:
                entry["extended_range_confirmed"] = True
                entry["er_note"] = "2024 Lariat (ER-only trim)"
                ner24 += 1
            if entry.get("equipment_511a_confirmed") is None:
                entry["equipment_511a_confirmed"] = True
                entry["equip_note"] = "2024 Lariat (511A always)"
                n511 += 1
        elif yr == 2023 and entry.get("extended_range_confirmed") is True:
            if entry.get("equipment_511a_confirmed") is None:
                entry["equipment_511a_confirmed"] = True
                entry["equip_note"] = "2023 Lariat ER (511A always)"
                n511 += 1
    totals["0h 511A auto-confirm"] = n511
    totals["0h 2024 Lariat ER confirm"] = ner24
    if n511 or ner24:
        dirty = True

    # ── 0q. Remove browser error page junk listings ───────────────────────────
    JUNK = (
        "your connection is not private",
        "connect to wi-fi",
        "wi-fi required",
        "access denied",
        "403 forbidden",
        "just a moment",
        "attention required",
    )
    n = 0
    for uid in list(seen.keys()):
        t = (seen[uid].get("title") or "").strip().lower()
        if t and any(s in t for s in JUNK):
            del seen[uid]
            n += 1
    totals["0q junk title removal"] = n
    if n:
        dirty = True

    # ── 0r Part A. Correct SR-VIN listings still labeled ER ──────────────────
    n = 0
    for uid, entry in list(seen.items()):
        if entry.get("status") in (SOLD, "filtered"):
            continue
        vin = (entry.get("vin") or "").strip().upper()
        if len(vin) < 4 or vin[3] != _VIN_SR_CHAR:
            continue
        if entry.get("extended_range_confirmed") is not True:
            continue
        note = entry.get("er_note") or ""
        if note.startswith("window sticker"):
            log.warning("0r: SR VIN but window sticker says ER for %s -- skipping", uid[:12])
            continue
        entry["extended_range_confirmed"] = False
        entry["er_note"] = (
            "corrected by migrate_state: VIN[3]=%s (SR) overrides %r" % (vin[3], note)
        )
        if entry.get("status") == ACTIVE:
            entry["status"] = "filtered"
        n += 1
    totals["0r SR-VIN correction"] = n

    # ── 0r Part B. Remove www.cars.com / cars.com titled junk ────────────────
    BAD = {"www.cars.com", "cars.com"}
    nb = 0
    for uid in list(seen.keys()):
        t = (seen[uid].get("title") or "").strip().lower()
        if t in BAD and not seen[uid].get("price") and not seen[uid].get("source_id"):
            del seen[uid]
            nb += 1
    totals["0r bad-title junk"] = nb
    if n or nb:
        dirty = True

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n=== migrate_state.py results ===")
    any_nonzero = False
    for step, count in totals.items():
        marker = " ***" if count > 0 else ""
        print(f"  {step:<40} {count:>5}{marker}")
        if count > 0:
            any_nonzero = True

    if dirty:
        save_state(state)
        print(f"\nState saved. Total listings: {len(seen)}")
    else:
        print("\nNothing changed -- state is already clean.")

    return totals


if __name__ == "__main__":
    run_migrations()
