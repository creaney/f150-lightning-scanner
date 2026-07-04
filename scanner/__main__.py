"""scanner/__main__.py -- CLI entry point for python3 -m scanner."""
import argparse
import asyncio
import concurrent.futures
import json
import os
import random
import re
import shutil
import sys
import tempfile
import threading
import time
import webbrowser
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup

from scanner.config import (
    log, _SuppressedStdout,
    BASE_DIR, STATE_FILE, REPORTS_DIR, TODAY, YESTERDAY, RUN_TS,
    S, ACTIVE, SOLD, PRICE_DROP,
    CARS_BASE_URL, CARS_KEYWORDS, CARS_BASE_PARAMS,
    _EBAY_BASE, EBAY_KEYWORDS,
    CARVANA_URL, CARGURUS_URL, EDMUNDS_URL, CARMAX_BROWSE_URL, AT_SEARCH_URL,
    _VIN_ER_CHAR, _VIN_SR_CHAR, _VIN_RE,
    _SERVER_PORT, _SERVER_INACTIVITY_SECS,
    _SUPPRESSED_UUIDS, _ALLOWED_TRIMS,
    _PRICE_MIN, _PRICE_MAX, _DEAL_BASELINE, _MILES_RATE, _MARKET_BASELINE_MIN_SAMPLE,
    _PW_UA, _PW_CTX_HEADERS,
    DEFAULT_SOURCES,
)
from scanner.state import load_state, save_state
from scanner.detect import (
    _vin_battery, _fetch_window_sticker_er, _apply_vin_er,
    detect_er, detect_511a, extract_price, extract_miles,
    _price_valid, _is_trim_excluded, needs_backfill,
    detect_seller_type, _url_hash,
)
from scanner.models import _build_listing, _partial_to_listing, _extract_ld_json
from scanner.score import compute_market_baseline, deal_score
from scanner.browser import _pw_fetch, _nd_fetch, _quiet_exception_handler, _kill_orphaned_browsers
from scanner.merge import merge_into_state
from scanner.report import generate_report
from scanner.server import _start_report_server
from scanner.sources.cars import sweep_cars_com, _parse_cars_vdp_soup, visit_all_cars_vdps_parallel
from scanner.sources.ebay import sweep_ebay, visit_ebay_item
from scanner.sources.carvana import sweep_carvana
from scanner.sources.cargurus import sweep_cargurus, _parse_cargurus_vdp
from scanner.sources.carmax import sweep_carmax
from scanner.sources.edmunds import sweep_edmunds, sweep_vroom
from scanner.sources.visor import sweep_visor
from scanner.sources.autotrader import sweep_autotrader, _parse_autotrader_vdp, _is_at_blocked

def dismiss_listing(uuid: str) -> None:
    """Set dismissed=True for a listing so it no longer appears in the Azure Gray section."""
    state = load_state()
    if uuid not in state["listings"]:
        print(f"Error: UUID {uuid!r} not found in state file.")
        sys.exit(1)
    entry = state["listings"][uuid]
    entry["dismissed"] = True
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False))
    log.info("Dismissed: %s  (%s)", uuid[:12], entry.get("title", ""))
    print(f"✓ Dismissed: {entry.get('title', uuid)}")


# ═══════════════════════════════════════════════════════════════════════════
# Interactive report server
# ═══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Lightning Scanner")
    parser.add_argument(
        "--dismiss", metavar="UUID",
        help="Dismiss a listing from the Azure Gray priority section",
    )
    parser.add_argument(
        "--report", action="store_true",
        help=(
            "Serve the most recent HTML report on localhost:8765 without running a scan. "
            "Opens the browser automatically and keeps the server alive until Ctrl+C."
        ),
    )
    parser.add_argument(
        "--source", nargs="+",
        choices=["cars", "ebay", "carvana", "cargurus", "carmax", "autotrader", "edmunds", "visor"],
        metavar="SOURCE",
        help=(
            "Run only the specified source(s) and skip sold-marking. "
            "For fast single-source testing. "
            "Choices: cars ebay carvana cargurus carmax autotrader edmunds"
        ),
        default=None,
    )
    args = parser.parse_args()

    if args.dismiss:
        dismiss_listing(args.dismiss)
        return

    if args.report:
        REPORTS_DIR.mkdir(exist_ok=True)
        # Find the most recently written report file
        reports = sorted(REPORTS_DIR.glob("*.html"), key=lambda p: p.stat().st_mtime)
        if not reports:
            print("No report files found in", REPORTS_DIR)
            sys.exit(1)
        latest = reports[-1]
        print(f"  Serving {latest.name} at http://localhost:{_SERVER_PORT}/")
        _start_report_server(latest)
        webbrowser.open(f"http://localhost:{_SERVER_PORT}/")
        print("  Press Ctrl+C to stop the server.")
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            print("\n  Server stopped.")
        return

    # --source: explicit list overrides DEFAULT_SOURCES.
    # No --source flag -> run DEFAULT_SOURCES (eBay excluded by default).
    if args.source:
        _sources = set(args.source)
        log.info("Single-source mode: %s -- sold-marking disabled", ", ".join(sorted(_sources)))
    else:
        _sources = set(DEFAULT_SOURCES)

    def _should_run(src: str) -> bool:
        return src in _sources

    log.info("Clearing any orphaned browser processes before scan...")
    _kill_orphaned_browsers()

    log.info("=== Lightning Scanner  %s ===", RUN_TS)
    REPORTS_DIR.mkdir(exist_ok=True)

    state = load_state()
    seen  = state["listings"]

    # ── AT Chrome profile (disabled) ──────────────────────────────────────────
    # --user-data-dir causes nodriver to crash; profile injection is disabled.
    # AT VDP access is blocked; search-result data is the current source.
    _AT_PROFILE_DIR: Optional[str] = None
    _at_temp_profile_dir = None


    all_listings: list = []

    # Initialise variables that are only set inside conditional sweep blocks
    # so that later code (timeout updates, merge call) always has them defined.
    cars_uuids: dict         = {}
    _cars_com_sweep_count: int = 0
    timed_out_uuids: set     = set()
    retry_succeeded: set     = set()

    # ── 1. cars.com (3 keyword sweeps) ────────────────────────────────
    if _should_run("cars"):
        cars_uuids = sweep_cars_com()
        # Remove permanently suppressed listings so they don't get VDP visits
        for _sup in _SUPPRESSED_UUIDS:
            cars_uuids.pop(_sup, None)
        # Capture sweep UUID count BEFORE backfills inflate cars_uuids —
        # this is the true observed coverage used by the sweep guard in merge_into_state.
        _cars_com_sweep_count = len(cars_uuids)

        # Queue cars.com listings missing key fields for re-visit even if already seen
        backfill_uuids = {
            uid for uid, entry in seen.items()
            if entry.get("source") == "cars.com"
            and entry.get("status") == ACTIVE
            and needs_backfill(entry)
            and uid not in _SUPPRESSED_UUIDS
            and not entry.get("dismissed")
        }
        for uid in backfill_uuids:
            if uid not in cars_uuids:
                existing = seen[uid]
                # Don't re-queue chronic timeouts with no data — they won't resolve
                if (
                    existing.get("timeout_count", 0) >= 5
                    and not existing.get("price")
                    and not existing.get("location")
                    and not existing.get("color")
                ):
                    log.info("Skipping chronic-timeout backfill: %s (count=%d)",
                             uid[:12], existing["timeout_count"])
                    continue
                cars_uuids[uid] = {
                    "id":       uid,
                    "source":   "cars.com",
                    "title":    existing.get("title", ""),
                    "price":    None,
                    "miles":    existing.get("miles"),
                    "location": existing.get("location", ""),
                    "vin":      existing.get("vin", ""),
                    "link":     existing.get("link", f"https://www.cars.com/vehicledetail/{uid}/"),
                    "_partial": True,
                }
                log.info("Queuing backfill re-visit: %s  (price=%s, loc=%r, color=%r)",
                         uid[:12],
                         f"${existing['price']:,}" if existing.get("price") else "?",
                         existing.get("location", ""),
                         existing.get("color", ""))

        log.info("Visiting %d cars.com VDPs in parallel (concurrency=4)...", len(cars_uuids))
        cars_details, timed_out_uuids, tier1_uuids = asyncio.run(
            visit_all_cars_vdps_parallel(cars_uuids, seen)
        )
        for detail in cars_details:
            if detail:
                all_listings.append(detail)

        # ── 1b. Retry timed-out VDPs (Tier 1 only, sequential, hard 150s wall-clock guard) ──
        tier1_retries = {uid for uid in timed_out_uuids if uid in tier1_uuids}
        if tier1_retries:
            log.info("Retrying %d Tier-1 timed-out VDPs with 120s Playwright / 150s wall-clock timeout...",
                     len(tier1_retries))
            consecutive_wall_timeouts = 0
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as retry_executor:
                for uuid in sorted(tier1_retries):
                    partial = cars_uuids.get(uuid, {"id": uuid, "source": "cars.com"})
                    existing = seen.get(uuid)
                    if existing and existing.get("status") in (SOLD, "filtered"):
                        log.debug("RETRY SKIP sold/filtered: %s", uuid[:12])
                        continue
                    if (
                        existing
                        and existing.get("timeout_count", 0) >= 5
                        and not existing.get("price")
                        and not existing.get("location")
                        and not existing.get("color")
                    ):
                        log.info("SKIP chronic timeout %s (count=%d, no data)",
                                 uuid[:12], existing["timeout_count"])
                        continue
                    log.info("RETRY %s  (%s)", uuid[:12], partial.get("title", "")[:40])
                    url = f"https://www.cars.com/vehicledetail/{uuid}/"
                    fut = retry_executor.submit(
                        _pw_fetch, url, 2500, None, "domcontentloaded", True, 120_000
                    )
                    try:
                        content = fut.result(timeout=150)
                    except concurrent.futures.TimeoutError:
                        consecutive_wall_timeouts += 1
                        log.warning("RETRY TIMEOUT %s (hard 150s wall clock)", uuid[:12])
                        if consecutive_wall_timeouts >= 3:
                            remaining = len([u for u in sorted(tier1_retries) if u > uuid])
                            log.warning(
                                "WARNING: 3 consecutive wall-clock timeouts — likely site-wide block. "
                                "Aborting retry loop. %d retries skipped.", remaining
                            )
                            break
                        continue
                    except Exception as exc:
                        consecutive_wall_timeouts = 0
                        log.warning("RETRY ERROR %s: %s", uuid[:12], exc)
                        continue
                    if content:
                        soup = BeautifulSoup(content, "lxml")
                        result = _parse_cars_vdp_soup(soup, uuid, partial)
                        if result:
                            all_listings.append(result)
                            retry_succeeded.add(uuid)
                            consecutive_wall_timeouts = 0
                            log.info("RETRY OK %s", uuid[:12])
                            continue
                    consecutive_wall_timeouts = 0
                    log.warning("RETRY FAILED %s", uuid[:12])
    else:
        log.info("Skipping cars.com sweep (--source mode)")

    # Release Chrome file descriptors accumulated during cars.com VDP backfill
    # before starting the next browser-heavy phase (eBay / AutoTrader).
    _kill_orphaned_browsers()

    # ── 2. eBay Motors ────────────────────────────────────────────────
    if _should_run("ebay"):
        ebay_partials = sweep_ebay()
    else:
        log.info("Skipping eBay sweep (--source mode)")
        ebay_partials = []

    if ebay_partials:
        log.info("Visiting eBay item pages (%d listings)...", len(ebay_partials))
    for partial in ebay_partials:
        uid      = partial["id"]
        existing = seen.get(uid)

        if (
            existing
            and existing.get("status") == ACTIVE
            and existing.get("price")
            and partial.get("price")
            and abs((partial["price"] or 0) - (existing["price"] or 0)) < 50
        ):
            all_listings.append({**existing})
            continue

        item_id = partial.get("ebay_id") or uid.replace("ebay-", "")
        detail  = visit_ebay_item(item_id, partial)
        if detail:
            all_listings.append(detail)
        time.sleep(1.5)

    # ── 3. Carvana, CarGurus, CarMax, AutoTrader, Edmunds, Vroom ─────
    _sweep_map = {
        "carvana":    sweep_carvana,
        "cargurus":   sweep_cargurus,
        "carmax":     sweep_carmax,
        "autotrader": sweep_autotrader,
        "edmunds":    sweep_edmunds,
        "visor":      sweep_visor,
    }
    for _src_name, _sweep_fn in _sweep_map.items():
        if not _should_run(_src_name):
            log.info("Skipping %s sweep (--source mode)", _src_name)
            continue
        try:
            _listings = _sweep_fn()
            all_listings.extend(_listings)
        except Exception as exc:
            log.warning("%s failed: %s", _sweep_fn.__name__, exc)
    # Vroom is always a no-op (ceased operations) — run unconditionally
    all_listings.extend(sweep_vroom())

    log.info("Total raw listings before dedup: %d", len(all_listings))

    # ── 3b. CarGurus VDP backfill ─────────────────────────────────────
    # Queue entries missing color OR with unconfirmed ER for VDP enrichment.
    # Tier 1 (missing color): visit every run regardless of last_vdp_visit.
    # Tier 2 (color present, ER unconfirmed): skip if already visited today.
    cg_backfill_uids = [
        uid for uid, entry in state["listings"].items()
        if uid.startswith("cargurus-")
        and uid not in _SUPPRESSED_UUIDS
        and (not entry.get("color") or entry.get("extended_range_confirmed") is None)
        and entry.get("link")
        and not entry.get("dismissed")
        and entry.get("status") not in ("sold", "filtered")
    ]
    if cg_backfill_uids:
        log.info("CarGurus VDP backfill: %d entries to enrich", len(cg_backfill_uids))
    for uid in cg_backfill_uids:
        entry = state["listings"][uid]
        vdp_url = entry.get("link", "")
        if not vdp_url:
            continue

        # Tier 1/2 visit frequency logic
        _cg_tier1 = not entry.get("color")   # missing color = must visit
        if not _cg_tier1:
            # Tier 2: ER unconfirmed but color present — skip if visited today
            if entry.get("last_vdp_visit") == TODAY:
                log.debug("CG backfill skip (visited today, Tier 2): %s", uid)
                continue

        try:
            enriched = _parse_cargurus_vdp(vdp_url)
        except Exception as exc:
            log.warning("CarGurus VDP backfill error (%s): %s", uid, exc)
            enriched = {}

        entry["last_vdp_visit"] = TODAY
        # Always increment attempt counter regardless of whether data came back
        entry["backfill_attempts"] = entry.get("backfill_attempts", 0) + 1

        _hist_priority_cg = {"🚫 Salvage": 4, "🚫 Buyback": 3, "⚠️ Accident": 2, "✅ Clean": 1, "❓ Unknown": 0}
        for k, v in enriched.items():
            if v is not None and not entry.get(k):
                entry[k] = v
        # History flag: upgrade if VDP found a more specific signal
        new_hist = enriched.get("history_flag")
        new_note = enriched.get("history_note", "")
        if new_hist:
            old_hist = entry.get("history_flag", "❓ Unknown")
            if _hist_priority_cg.get(new_hist, 0) > _hist_priority_cg.get(old_hist, 0):
                entry["history_flag"] = new_hist
                entry["history_note"] = new_note
                log.info("History resolved for %s: %s → %s (source: %s)",
                         uid[:20], old_hist, new_hist, new_note or "CarGurus VDP text")

        # Change 7: if VDP confirmed a non-Lariat trim, filter the listing.
        # Use _is_trim_excluded() so the 2023 Platinum-under-$50k exception is honoured.
        # Combine entry title (carries year) + confirmed trim so the year-aware Platinum
        # path inside _is_trim_excluded() has enough context to decide correctly.
        confirmed_trim = enriched.get("trim", "")
        if confirmed_trim and _is_trim_excluded(
            entry.get("title", "") + " " + confirmed_trim,
            price=entry.get("price"),
        ):
            entry["status"] = "filtered"
            log.info("Filtering non-Lariat trim %r for listing %s", confirmed_trim, uid)

        # Change 4: suppress ghost listings that never resolve after 5 attempts
        if (entry.get("backfill_attempts", 0) >= 5
                and not entry.get("price")
                and not entry.get("location")
                and not entry.get("title")):
            entry["status"] = "filtered"
            log.warning("Auto-suppressing ghost listing %s after %d failed backfills",
                        uid, entry["backfill_attempts"])

        time.sleep(1.5)

    # ── 3c. AT VDP backfill REMOVED (HF-31) ──────────────────────────────
    # Chrome-based AT VDP backfill was removed because Chrome exhaustion during
    # cars.com phase caused ALL 204 AT listings to accumulate vdp_fail_count ≥ 3
    # and get auto-filtered in a single run (HF-30 regression).  Location data is
    # now extracted from search-result card text by _at_parse_cards (no Chrome
    # needed), and migration 0k restores any listings that were incorrectly filtered.

    # ── 3d. Pre-merge VDP probe for NEW at-* shells (Fix 1B) ──────────
    # New at-* shells discovered this sweep are NOT in state yet. Probe each
    # one immediately — if the page is blocked, mark it filtered before it
    # enters state so dead shells never accumulate.
    _known_at_ids = {uid for uid in state["listings"] if uid.startswith("at-")}
    _new_at_shells = [r for r in all_listings
                      if r.get("id", "").startswith("at-")
                      and r["id"] not in _known_at_ids]
    if _new_at_shells:
        # Drain Chrome FDs before AT VDP probing — startup fails with EMFILE if
        # cars.com / eBay browser instances weren't fully reaped yet.
        _kill_orphaned_browsers()
        log.info("AutoTrader: probing %d new shell(s) before merge", len(_new_at_shells))
    _shells_blocked = 0
    _shells_live = 0
    for _shell in _new_at_shells:
        _shell_url = _shell.get("link", "")
        if not _shell_url:
            _shell["vdp_fail_count"] = 1
            _shell["status"] = "filtered"
            _shells_blocked += 1
            continue
        _shell_content: Optional[str] = None
        try:
            _shell_content = _pw_fetch(_shell_url, wait_ms=5000, headless=False,
                                       user_data_dir=_AT_PROFILE_DIR)
        except Exception:
            pass
        if _is_at_blocked(_shell_content):
            _shell["vdp_fail_count"] = 1
            _shells_blocked += 1
            # Only pre-filter if the shell has NO useful data from the search page.
            # Shells with price OR location already visible should enter the report
            # so they remain visible in the report.  The backfill loop will retry on subsequent runs
            # and auto-filter after 3 consecutive VDP failures.
            _shell_has_data = bool(_shell.get("price") or _shell.get("location"))
            if not _shell_has_data:
                _shell["status"] = "filtered"
                log.debug("AutoTrader: new shell %s blocked + no data — pre-filtered",
                          _shell["id"])
            else:
                # Mark as visited today so the backfill loop doesn't retry this run
                _shell["last_vdp_visit"] = TODAY
                log.debug("AutoTrader: new shell %s VDP blocked but has search data — entering report",
                          _shell["id"])
        else:
            _shells_live += 1
            # Live page — enrich the shell while we have it
            try:
                _shell_enriched = _parse_autotrader_vdp(_shell_url, content=_shell_content)
                for _k, _v in _shell_enriched.items():
                    if _v in (None, ""):
                        continue
                    # Never overwrite miles with a plausible-but-wrong value from
                    # the AT geo API (dma/searchRadius contamination, Bug B).
                    if _k == "miles" and (_shell.get("miles") or 0) > 1000:
                        continue
                    _shell[_k] = _v
            except Exception:
                pass
        time.sleep(0.5)
    if _new_at_shells:
        log.info("AutoTrader shells: %d probed — %d blocked/pre-filtered, %d live",
                 len(_new_at_shells), _shells_blocked, _shells_live)

    # ── 4. Merge, dedup, update state ─────────────────────────────────
    summary = merge_into_state(
        state, all_listings,
        cars_com_sweep_count=_cars_com_sweep_count,
        skip_sold_marking=bool(args.source),
    )

    # Update timeout_count for cars.com listings based on this run's outcomes.
    # Any UUID visited successfully (first pass or retry) resets to 0;
    # any UUID that timed out on both passes increments by 1.
    vdp_succeeded = (set(cars_uuids.keys()) - timed_out_uuids) | retry_succeeded
    for uid in cars_uuids:
        entry = state["listings"].get(uid)
        if entry is None:
            continue
        if uid in vdp_succeeded:
            entry["timeout_count"] = 0
        elif uid in timed_out_uuids and uid not in retry_succeeded:
            entry["timeout_count"] = entry.get("timeout_count", 0) + 1

    save_state(state)

    # ── 4b. Window sticker pass ───────────────────────────────────────
    # For every active listing with a full VIN but no ER determination,
    # fetch the Ford Monroney PDF and parse the equipment group code.
    # Runs after merge so newly-added VINs are already in state.
    # Skipped if pdfplumber is not installed.
    try:
        import pdfplumber as _pdfplumber_check  # noqa: F401
        _ws_available = True
    except ImportError:
        _ws_available = False

    _ws_resolved = 0
    _ws_checked  = 0
    if _ws_available:
        for _uid, _entry in seen.items():
            if _entry.get("status") in (SOLD, "filtered"):
                continue
            if _entry.get("extended_range_confirmed") is not None:
                continue  # already resolved
            _vin = (_entry.get("vin") or "").strip().upper()
            if len(_vin) != 17:
                continue  # no valid VIN
            _ws_checked += 1
            _er_ws, _note_ws = _fetch_window_sticker_er(_vin)
            if _er_ws is not None:
                _entry["extended_range_confirmed"] = _er_ws
                _entry["er_note"] = _note_ws
                if not _er_ws and _entry.get("status") == ACTIVE:
                    _entry["status"] = "filtered"
                    log.info("Window sticker SR: filtering %s (%s)",
                             _uid[:12], _entry.get("title", "")[:40])
                else:
                    log.info("Window sticker ER resolved: %s -> %s (%s)",
                             _uid[:12], "ER" if _er_ws else "SR", _note_ws)
                _ws_resolved += 1
            time.sleep(1.0)  # rate-limit Ford's server
        if _ws_checked:
            log.info("Window sticker pass: checked %d listings, resolved %d",
                     _ws_checked, _ws_resolved)
            if _ws_resolved:
                save_state(state)
    else:
        log.debug("Window sticker pass: skipped (pdfplumber not installed)")

    # ── 5. HTML report ────────────────────────────────────────────────
    report_path = generate_report(state, summary)

    # ── 5b. Interactive report server ─────────────────────────────────
    _srv_thread = None
    try:
        _srv_thread = _start_report_server(report_path)
        webbrowser.open(f"http://localhost:{_SERVER_PORT}/")
        print(f"  🌐 Report server live at http://localhost:{_SERVER_PORT}/"
              f" — closes after 30 min inactivity (Ctrl+C to stop early)")
    except OSError as _srv_err:
        log.warning("Could not start report server: %s", _srv_err)

    # ── 6. Terminal summary ───────────────────────────────────────────
    # Match the HTML report: exclude filtered, suppressed, and dismissed entries.
    n_active = sum(
        1 for v in state["listings"].values()
        if v.get("status") not in (SOLD, "filtered")
        and v["id"] not in _SUPPRESSED_UUIDS
        and not v.get("dismissed")
    )
    n_new    = len(summary.get("new", []))
    n_drops  = len(summary.get("price_drops", []))
    n_sr     = len(summary.get("sr_excluded", []))
    n_unc    = len(summary.get("unconfirmed_er", []))
    n_azure  = sum(1 for v in state["listings"].values()
                   if v.get("azure_gray")
                   and v.get("status") not in (SOLD, "filtered")
                   and not v.get("dismissed"))

    print()
    print("═" * 46)
    print("  ⚡ Lightning Scan Complete")
    print("═" * 46)
    print(f"  Active listings   : {n_active}")
    print(f"  New this run      : {n_new}")
    print(f"  Price drops       : {n_drops}")
    print(f"  Azure Gray        : {n_azure}")
    print(f"  SR excluded       : {n_sr}")
    print(f"  ER unconfirmed    : {n_unc}")
    print(f"  Report            : {report_path.relative_to(BASE_DIR)}")
    print("═" * 46)

    if summary.get("new"):
        print("\n  NEW LISTINGS THIS RUN:")
        for uid in summary["new"]:
            v  = state["listings"].get(uid, {})
            er = ("✓ER" if v.get("extended_range_confirmed") is True
                  else ("?ER" if v.get("extended_range_confirmed") is None else "SR"))
            az = " 🔵" if v.get("azure_gray") else ""
            price_str = f"${v['price']:,}" if v.get("price") else "?"
            print(f"    [{er}]{az}  {v.get('title','')[:42]}  ·  {v.get('location','')}  ·  {price_str}")

    if summary.get("price_drops"):
        print("\n  PRICE DROPS:")
        for uid in summary["price_drops"]:
            v    = state["listings"].get(uid, {})
            hist = v.get("price_history", [])
            cur  = v.get("price")
            if not cur or not hist:
                continue
            # Find the highest recorded price before the current price.
            # Using hist[-2] alone is unreliable after dedup merges reorder history.
            prev_prices = [h["price"] for h in hist if h.get("price") and h["price"] > cur]
            if not prev_prices:
                continue  # no prior higher price in history; skip (stale or bad data)
            old_p = max(prev_prices)
            print(f"    ${old_p:,} → ${cur:,}  {v.get('title','')[:42]}")

    print()

    # Keep the process alive so the daemon server thread can serve requests.
    # The server exits after _SERVER_INACTIVITY_SECS of idle time.
    if _srv_thread is not None:
        try:
            while True:
                time.sleep(60)
        except KeyboardInterrupt:
            print("\n  Report server stopped.")

    # AT Chrome profile cleanup — disabled (profile injection disabled in 0d)


if __name__ == "__main__":
    main()
