#!/usr/bin/env python3
"""
Phase 1 backfill: correct ER state entries that were set by now-invalid logic.

Two categories of corrections:
  1. VIN-based: entries where VIN[3]='V' (SR) but er_confirmed=True.
     _apply_vin_er() already handles this at merge time, but these entries
     predate that correction or arrived before VIN was available.
  2. Text-only: entries where er_note='text-confirmed ER' and no VIN.
     Under Phase 1 rules, text hints alone cannot confirm ER.
     These are reset to (None, 'reset by Phase 1: text cannot confirm ER').

Entries confirmed by authoritative or strong sources are left alone:
  - VIN-confirmed ER (VIN[3]='6')
  - Window sticker
  - Edmunds electricityRange API field
  - 2024 Lariat year/trim rule
  - User-confirmed
  - 131 kWh / ER battery / 511A code in text (strong -- preserved)

Backup must be created before running this script.
"""
import sys
from pathlib import Path

# Allow running as a script from any directory
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.backup_state import backup
from scanner import _vin_battery, load_state, save_state

def backfill():
    print("Backing up state before backfill...")
    backup()

    state  = load_state()
    listings = state["listings"]

    corrected_vin_sr  = []   # had VIN[3]='V' but er_confirmed=True
    corrected_text    = []   # had er_note='text-confirmed ER', no VIN, now unconfirmed
    skipped_protected = []   # left alone (window sticker, Edmunds, user-confirmed, etc.)

    PROTECTED_NOTES = (
        "window sticker",
        "edmunds electricityrange",
        "user confirmed",
        "2024 lariat",
        "2023 lariat",
        "vdp battery",       # explicit battery kWh read from VDP page
    )

    for uid, entry in listings.items():
        er    = entry.get("extended_range_confirmed")
        note  = (entry.get("er_note") or "").strip()
        vin   = entry.get("vin", "") or ""
        note_l = note.lower()

        if er is not True:
            continue   # only fixing incorrect True labels

        # Protected sources: leave alone regardless
        if any(note_l.startswith(p) for p in PROTECTED_NOTES):
            skipped_protected.append((uid[:16], note[:50]))
            continue

        # Category 1: VIN says SR but er_confirmed=True
        if vin:
            bat = _vin_battery(vin)
            if bat == "SR":
                old_note = note
                entry["extended_range_confirmed"] = False
                entry["er_note"] = (
                    f"corrected by Phase 1 backfill: VIN[3]={vin[3]} (SR)"
                    f" overrides prior {old_note!r}"
                )
                corrected_vin_sr.append((uid[:16], vin, old_note[:40]))
                continue

        # Category 2: text-only ER confirm with no VIN
        if not vin and note == "text-confirmed ER":
            entry["extended_range_confirmed"] = None
            entry["er_note"] = "reset by Phase 1: text cannot confirm ER"
            corrected_text.append((uid[:16], note[:40]))
            continue

    print(f"\n=== Phase 1 backfill results ===")
    print(f"VIN-SR corrections (True -> False):  {len(corrected_vin_sr)}")
    for uid, vin, old in corrected_vin_sr:
        print(f"  {uid}  vin={vin}  was: {old!r}")

    print(f"\nText-only resets (True -> None):     {len(corrected_text)}")
    for uid, old in corrected_text:
        print(f"  {uid}  was: {old!r}")

    print(f"\nProtected (unchanged):               {len(skipped_protected)}")

    if not corrected_vin_sr and not corrected_text:
        print("Nothing to change.")
        return

    save_state(state)
    print(f"\nState saved.")

if __name__ == "__main__":
    backfill()
