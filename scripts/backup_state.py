#!/usr/bin/env python3
"""Back up seen-listings.json before any phase that can touch state."""
import shutil
from datetime import datetime
from pathlib import Path

BASE_DIR   = Path(__file__).parent.parent
STATE_FILE = BASE_DIR / "seen-listings.json"
BACKUP_DIR = BASE_DIR / "backups"

def backup():
    if not STATE_FILE.exists():
        raise FileNotFoundError(f"State file not found: {STATE_FILE}")
    BACKUP_DIR.mkdir(exist_ok=True)
    ts   = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    dest = BACKUP_DIR / f"seen-listings.{ts}.json"
    shutil.copy2(STATE_FILE, dest)
    print(f"Backed up: {dest}")
    return dest

if __name__ == "__main__":
    backup()
