"""scanner/merge.py -- merge new listings into state dict."""
import re
from typing import Optional

from scanner.config import (
    log, TODAY, ACTIVE, SOLD, PRICE_DROP,
    _ALLOWED_TRIMS, _DEALER_SUFFIX_RE, _SUPPRESSED_UUIDS,
)
from scanner.detect import (
    _apply_vin_er, _fetch_window_sticker_er, _is_trim_excluded, _price_valid,
)

def merge_into_state(
    state: dict,
    new_listings: list,
    cars_com_sweep_count: int = 0,
    skip_sold_marking: bool = False,
) -> dict:
    """
    Merge collected listings into state. Returns summary of changes.
    Deduplicates by VIN where available.
    SR listings are excluded. ER-unconfirmed listings are included with flag.
    """
    seen = state["listings"]
    # Build reverse VIN → canonical_id index from existing state
    vin_index = {v["vin"]: k for k, v in seen.items() if v.get("vin")}

    summary: dict = {
        "new":          [],
        "price_drops":  [],
        "unchanged":    [],
        "sr_excluded":  [],
        "unconfirmed_er": [],
    }

    current_ids: set = set()

    for listing in new_listings:
        uid   = listing["id"]
        vin   = listing.get("vin", "")
        price = listing.get("price")
        er    = listing.get("extended_range_confirmed")

        # Skip permanently suppressed junk listings
        if uid in _SUPPRESSED_UUIDS:
            continue

        # Belt-and-suspenders trim filter — catches any Pro/XLT that slipped past VDP stage.
        # Wrap title in spaces so word-boundary matching works at start/end of string.
        _merge_title_lower = (listing.get("title") or "").lower()
        if _merge_title_lower:
            _m_trim_rejected = any(t in f" {_merge_title_lower} " for t in ("pro", "xlt"))
            _m_trim_allowed  = any(t in _merge_title_lower for t in _ALLOWED_TRIMS)
            if _m_trim_rejected and not _m_trim_allowed:
                log.info("merge: skipping wrong-trim listing '%s'", listing.get("title"))
                continue

        # Exclude confirmed Standard Range
        if er is False:
            log.debug("SR excluded: %s", listing.get("title", uid)[:50])
            summary["sr_excluded"].append(uid)
            continue

        if er is None:
            summary["unconfirmed_er"].append(uid)

        # VIN-based deduplication across sources
        canonical_id = uid
        if vin and vin in vin_index and vin_index[vin] != uid:
            canonical_id = vin_index[vin]
            log.info("  Dedup %s → %s (same VIN %s)", uid[:16], canonical_id[:16], vin[:10])

        current_ids.add(canonical_id)

        if canonical_id not in seen:
            entry = _new_state_entry(listing, canonical_id)
            seen[canonical_id] = entry
            if vin:
                vin_index[vin] = canonical_id
            # Don't announce pre-filtered new shells (e.g. AT VDPs that were blocked)
            if entry.get("status") == "filtered":
                log.debug("  NEW (filtered)  %s  |  pre-merge VDP probe failed", canonical_id[:12])
            else:
                summary["new"].append(canonical_id)
                log.info("  NEW  %s  |  %s  |  %s  |  $%s",
                         canonical_id[:12],
                         entry.get("title", "")[:40],
                         entry.get("location", ""),
                         f"{entry['price']:,}" if entry.get("price") else "?")
        else:
            existing = seen[canonical_id]

            # A dismissed listing that was marked sold and has now reappeared should
            # NOT be reactivated — the dismissal takes permanent precedence.
            if existing.get("dismissed") and existing.get("status") == SOLD:
                log.debug("Skipping reactivation of dismissed listing %s", canonical_id)
                continue

            old_price = existing.get("price")

            # Confirm or discard any large drop held from the previous run
            if "price_drop_pending" in existing:
                pending = existing["price_drop_pending"]
                if price and abs(price - pending["price"]) < 500:
                    # New VDP agrees — commit the held drop
                    log.info(
                        "Confirming held price drop: $%s → $%s for %s",
                        f"{old_price:,}", f"{pending['price']:,}", canonical_id[:12],
                    )
                    existing["price"] = pending["price"]
                    existing["price_history"].append({"date": pending["date"], "price": pending["price"]})
                    if pending["price"] < old_price - 100 and _price_valid(old_price):
                        existing["status"] = PRICE_DROP
                        summary["price_drops"].append(canonical_id)
                    del existing["price_drop_pending"]
                else:
                    # Price didn't confirm — was a bad read, discard
                    log.info(
                        "Discarding unconfirmed price drop (%.0f%%) for %s — price not confirmed",
                        pending["drop_pct"], canonical_id[:12],
                    )
                    del existing["price_drop_pending"]

            if price and old_price and price != old_price and _price_valid(price):
                # Hold large drops for confirmation: >20% OR >=$10k absolute
                drop_pct = (old_price - price) / old_price if price < old_price else 0
                if drop_pct > 0.20 or (old_price - price) >= 10_000:
                    existing["price_drop_pending"] = {
                        "price": price,
                        "date": TODAY,
                        "from_price": old_price,
                        "drop_pct": round(drop_pct * 100, 1),
                    }
                    log.warning(
                        "Large price drop held for confirmation: $%s → $%s (%.0f%%) for %s — "
                        "will confirm next run",
                        f"{old_price:,}", f"{price:,}", drop_pct * 100, canonical_id[:12],
                    )
                    # Do NOT update price or fire PRICE_DROP yet
                else:
                    existing["price"] = price
                    existing["price_history"].append({"date": TODAY, "price": price})
                    # Only alert on real drops — skip if the previous price was corrupt
                    if price < old_price - 100 and _price_valid(old_price):
                        existing["status"] = PRICE_DROP
                        summary["price_drops"].append(canonical_id)
                        log.info("  DROP %s  $%s → $%s  (%s)",
                                 canonical_id[:12], old_price, price,
                                 existing.get("title", "")[:35])
                    else:
                        # Price went up or barely moved — always clear stale PRICE_DROP.
                        # Without this, a listing that dropped then recovered stays orange forever.
                        existing["status"] = ACTIVE
                        summary["unchanged"].append(canonical_id)
            elif price and not old_price:
                # Backfill: price was None/missing, now we have a valid price
                existing["price"] = price
                if not existing.get("price_history"):
                    existing["price_history"] = []
                existing["price_history"].append({"date": TODAY, "price": price})
                if existing["status"] != PRICE_DROP:
                    existing["status"] = ACTIVE
                summary["unchanged"].append(canonical_id)
            else:
                if existing["status"] == "filtered":
                    # Re-activate AT listings filtered due to missing trim (HF-34)
                    # when the incoming listing now has a trim-confirmed title.
                    _new_title = listing.get("title", "")
                    _old_title = existing.get("title", "")
                    _trim_words = ("lariat", "platinum", "flash", "xlt", "pro")
                    _new_has_trim = any(w in _new_title.lower() for w in _trim_words)
                    _old_missing_trim = not any(w in _old_title.lower() for w in _trim_words)
                    if _new_has_trim and _old_missing_trim:
                        existing["status"] = ACTIVE
                        existing["title"] = _new_title  # force: upgrade path skips non-empty titles
                        log.info("AT re-activate %s: trim confirmed in new title (%s)",
                                 canonical_id[:12], _new_title[:40])
                    # else: leave filtered — legitimately filtered or trim still unknown
                elif existing["status"] not in (PRICE_DROP,):
                    existing["status"] = ACTIVE
                summary["unchanged"].append(canonical_id)

            existing["last_seen"] = TODAY
            existing["sweep_miss_count"] = 0   # seen this run — reset 2-miss counter

            # Always refresh dos_active from Visor (it's the most current DOM signal)
            if listing.get("dos_active") is not None:
                existing["dos_active"] = listing["dos_active"]

            # Upgrade fields as better data becomes available
            for field in ("vin", "color", "location", "seller_type",
                          "extended_range_confirmed", "equipment_511a_confirmed",
                          "er_note", "equip_note"):
                new_val = listing.get(field)
                if new_val and not existing.get(field):
                    existing[field] = new_val
            # Upgrade history by priority: Buyback > Accident > Clean > Unknown
            # Never downgrade (e.g. Buyback → Clean is not allowed)
            _hist_priority = {"🚫 Salvage": 4, "🚫 Buyback": 3, "⚠️ Accident": 2, "✅ Clean": 1, "❓ Unknown": 0}
            new_hist = listing.get("history_flag")
            if new_hist:
                old_hist = existing.get("history_flag", "❓ Unknown")
                if _hist_priority.get(new_hist, 0) > _hist_priority.get(old_hist, 0):
                    existing["history_flag"] = new_hist
                    existing["history_note"] = listing.get("history_note", "")
                    if old_hist != new_hist:
                        log.info(
                            "History resolved for %s: %s → %s (source: %s)",
                            canonical_id[:20], old_hist, new_hist,
                            listing.get("history_note") or listing.get("source", "sweep"),
                        )
            if listing.get("vin") and not existing.get("vin"):
                vin_index[listing["vin"]] = canonical_id
            existing["azure_gray"] = listing.get("azure_gray", existing.get("azure_gray", False))
            if listing.get("_vdp_visited"):
                existing["last_vdp_visit"] = TODAY

            # VIN position-4 ER detection: runs every merge so newly-written VINs
            # (from backfill) get their ER status resolved immediately.
            _apply_vin_er(existing)

            # If vin was just written and ER is still unknown, try window sticker.
            if (existing.get("vin") and
                    existing.get("extended_range_confirmed") is None):
                _er_ws, _note_ws = _fetch_window_sticker_er(existing["vin"])
                if _er_ws is not None:
                    existing["extended_range_confirmed"] = _er_ws
                    existing["er_note"] = _note_ws

            # Re-apply trim filter using best available price. Catches Platinums whose VDP
            # timed out before a fresh price could be confirmed — we use the stored price.
            if existing.get("status") == ACTIVE:
                _best_price = price if _price_valid(price) else existing.get("price")
                if _is_trim_excluded(existing.get("title", ""), price=_best_price):
                    existing["status"] = "filtered"
                    log.info("  FILT %s  %s (stored-price trim check)",
                             canonical_id[:12], existing.get("title", "")[:40])

            # Year filter: VDP may confirm a 2025+ title that the card stage missed.
            if existing.get("status") == ACTIVE:
                _year_m = re.search(r"\b(20\d\d)\b", existing.get("title", ""))
                if _year_m and int(_year_m.group(1)) > 2024:
                    existing["status"] = "filtered"
                    log.info("  FILT %s  %s (model year %s)",
                             canonical_id[:12], existing.get("title", "")[:50],
                             _year_m.group(1))

    # ── Secondary dedup: same dealer + similar mileage + similar price ───────
    # Catches cross-source duplicates where one listing is missing a VIN so VIN
    # dedup didn't fire.  Only merges across different sources to avoid
    # false-positives within a single source (e.g. two auctions at same lot).
    def _norm_dealer(loc: str) -> str:
        """Extract and normalise dealer name from 'Name · City, ST' location string.

        Strips city/state suffix, lowercases, removes punctuation, strips common
        business-type suffixes ('Sales Inc', 'LLC', 'Motors', etc.), and collapses
        internal whitespace so 'J Star Ford' and 'JStar Ford' map to the same key.
        """
        name = loc.split("·")[0].strip() if "·" in loc else loc
        name = re.sub(r"[^a-z0-9\s]", "", name.lower())
        name = _DEALER_SUFFIX_RE.sub("", name).strip()
        # Collapse all whitespace — normalises 'J Star' vs 'JStar', extra spaces, etc.
        return re.sub(r"\s+", "", name)

    _fuzzy_index: dict = {}
    for _fuid, _fentry in seen.items():
        if _fentry.get("status") in (SOLD, "filtered"):
            continue
        if _fentry.get("dismissed"):
            continue
        _fn = _norm_dealer(_fentry.get("location", ""))
        if len(_fn) < 5:
            continue
        _fp = _fentry.get("price")
        _fm = _fentry.get("miles")
        if not _fp or not _fm:
            continue
        _key = (_fn, _fp // 500, _fm // 1000)
        _fuzzy_index.setdefault(_key, []).append(_fuid)

    for _key, _fuids in _fuzzy_index.items():
        if len(_fuids) < 2:
            continue
        # Only merge across different sources
        _sources = {seen[u].get("source") for u in _fuids}
        if len(_sources) < 2:
            continue
        # Sort by completeness: count non-empty interesting fields.
        # Visor links are visor.vin search fallbacks, not dealer VDPs, so visor
        # loses to any real source at equal field score.
        _COMPLETE_FIELDS = ("vin", "color", "price", "miles", "location",
                            "extended_range_confirmed", "er_note", "history_flag")
        def _completeness(u):
            e = seen[u]
            field_score = sum(1 for f in _COMPLETE_FIELDS if e.get(f) not in (None, "", False))
            source_bonus = 0 if e.get("source") == "visor" else 1
            return (field_score, source_bonus)
        _fuids_sorted = sorted(_fuids, key=_completeness, reverse=True)
        _canonical = _fuids_sorted[0]
        _can_entry  = seen[_canonical]
        for _dup_uid in _fuids_sorted[1:]:
            _dup = seen[_dup_uid]
            log.warning(
                "FUZZY-DEDUP: %s (%s, $%s, %s mi) ≈ %s (%s) — merging into canonical",
                _dup_uid[:20], _dup.get("source"), _dup.get("price"), _dup.get("miles"),
                _canonical[:20], _can_entry.get("source"),
            )
            # Upgrade canonical with any fields the duplicate has and canonical lacks
            for _ff in ("vin", "color", "location", "seller_type",
                        "extended_range_confirmed", "er_note",
                        "equipment_511a_confirmed", "equip_note",
                        "history_flag", "history_note", "dos_active"):
                if _dup.get(_ff) and not _can_entry.get(_ff):
                    _can_entry[_ff] = _dup[_ff]
            # If duplicate had VIN, register it in vin_index
            if _dup.get("vin") and _dup["vin"] not in vin_index:
                vin_index[_dup["vin"]] = _canonical
            # Re-run ER detection on canonical in case we just got a VIN
            _apply_vin_er(_can_entry)
            if (_can_entry.get("vin") and
                    _can_entry.get("extended_range_confirmed") is None):
                _er_ws, _note_ws = _fetch_window_sticker_er(_can_entry["vin"])
                if _er_ws is not None:
                    _can_entry["extended_range_confirmed"] = _er_ws
                    _can_entry["er_note"] = _note_ws
            # Mark duplicate as filtered/merged
            _dup["status"] = "filtered"
            _dup["merged_into"] = _canonical

    # ── Cross-bucket price fix: same-truck pairs straddling a $500 boundary ───────
    # e.g. $49,999 (bucket 99) + $50,498 (bucket 100) — same truck, different sources.
    # Scan adjacent price buckets for the same dealer+miles key; merge if within $600.
    _cross_checked: set = set()
    for (_fn, _pb, _mb), _fuids_lo in list(_fuzzy_index.items()):
        _hi_key = (_fn, _pb + 1, _mb)
        if _hi_key not in _fuzzy_index:
            continue
        for _u_lo in _fuids_lo:
            for _u_hi in _fuzzy_index[_hi_key]:
                _pair = tuple(sorted([_u_lo, _u_hi]))
                if _pair in _cross_checked:
                    continue
                _cross_checked.add(_pair)
                _e_lo = seen.get(_u_lo, {})
                _e_hi = seen.get(_u_hi, {})
                if _e_lo.get("status") in (SOLD, "filtered"):
                    continue
                if _e_hi.get("status") in (SOLD, "filtered"):
                    continue
                if _e_lo.get("source") == _e_hi.get("source"):
                    continue
                _p_lo = _e_lo.get("price") or 0
                _p_hi = _e_hi.get("price") or 0
                if abs(_p_lo - _p_hi) > 600:
                    continue  # adjacent bucket but too far apart — skip
                _COMPLETE_FIELDS = ("vin", "color", "price", "miles", "location",
                                    "extended_range_confirmed", "er_note", "history_flag")
                def _cb_score(e):
                    field_score = sum(1 for f in _COMPLETE_FIELDS if e.get(f) not in (None, "", False))
                    source_bonus = 0 if e.get("source") == "visor" else 1
                    return (field_score, source_bonus)
                _score_lo = _cb_score(_e_lo)
                _score_hi = _cb_score(_e_hi)
                if _score_lo >= _score_hi:
                    _can_uid, _can, _dup_uid, _dup = _u_lo, _e_lo, _u_hi, _e_hi
                else:
                    _can_uid, _can, _dup_uid, _dup = _u_hi, _e_hi, _u_lo, _e_lo
                log.warning(
                    "FUZZY-DEDUP (cross-bucket): %s (%s, $%s) ≈ %s (%s, $%s) — merging",
                    _dup_uid[:20], _dup.get("source"), _dup.get("price"),
                    _can_uid[:20], _can.get("source"), _can.get("price"),
                )
                for _ff in ("vin", "color", "location", "seller_type",
                            "extended_range_confirmed", "er_note",
                            "equipment_511a_confirmed", "equip_note",
                            "history_flag", "history_note", "dos_active"):
                    if _dup.get(_ff) and not _can.get(_ff):
                        _can[_ff] = _dup[_ff]
                if _dup.get("vin") and _dup["vin"] not in vin_index:
                    vin_index[_dup["vin"]] = _can_uid
                _apply_vin_er(_can)
                if _can.get("vin") and _can.get("extended_range_confirmed") is None:
                    _er_ws, _note_ws = _fetch_window_sticker_er(_can["vin"])
                    if _er_ws is not None:
                        _can["extended_range_confirmed"] = _er_ws
                        _can["er_note"] = _note_ws
                _dup["status"] = "filtered"
                _dup["merged_into"] = _can_uid

    # ── Secondary dedup pass B: price-only match for listings missing miles ───
    # AT entries often lack miles; this catches AT+cars.com same-truck pairs
    # where the AT card didn't extract mileage.
    _fuzzy_price_index: dict = {}
    for _fuid, _fentry in seen.items():
        if _fentry.get("status") in (SOLD, "filtered"):
            continue
        if _fentry.get("dismissed"):
            continue
        if _fentry.get("miles"):
            continue  # covered by the main fuzzy index above
        _fn = _norm_dealer(_fentry.get("location", ""))
        if len(_fn) < 5:
            continue
        _fp = _fentry.get("price")
        if not _fp:
            continue
        _key = (_fn, _fp // 100)  # tighter price bucket, no miles
        _fuzzy_price_index.setdefault(_key, []).append(_fuid)

    for _key, _fuids_missing in _fuzzy_price_index.items():
        _dealer_name, _price_bucket = _key
        _confirmed_matches = []
        for _fuid2, _fentry2 in seen.items():
            if _fentry2.get("status") in (SOLD, "filtered"):
                continue
            if _fentry2.get("dismissed"):
                continue
            if _fentry2.get("miles") is None:
                continue  # need at least one side to have miles
            if _norm_dealer(_fentry2.get("location", "")) != _dealer_name:
                continue
            if abs((_fentry2.get("price") or 0) - (_price_bucket * 100)) > 150:
                continue
            _confirmed_matches.append(_fuid2)

        if not _confirmed_matches:
            continue

        _COMPLETE_FIELDS = ("vin", "color", "price", "miles", "location",
                            "extended_range_confirmed", "er_note", "history_flag")
        for _dup_uid in _fuids_missing:
            _dup = seen[_dup_uid]
            for _can_uid in _confirmed_matches:
                _can = seen[_can_uid]
                if _dup.get("source") == _can.get("source"):
                    continue  # same source, skip
                log.warning(
                    "FUZZY-DEDUP (price-only): %s (%s, $%s, miles=None) ≈ %s (%s, $%s, %s mi) "
                    "— merging into canonical",
                    _dup_uid[:20], _dup.get("source"), _dup.get("price"),
                    _can_uid[:20], _can.get("source"), _can.get("price"), _can.get("miles"),
                )
                _dup_score = sum(1 for f in _COMPLETE_FIELDS if _dup.get(f) not in (None, "", False))
                _can_score = sum(1 for f in _COMPLETE_FIELDS if _can.get(f) not in (None, "", False))

                if _can_score >= _dup_score:
                    _canonical_uid, _canonical, _to_filter_uid, _to_filter = _can_uid, _can, _dup_uid, _dup
                else:
                    _canonical_uid, _canonical, _to_filter_uid, _to_filter = _dup_uid, _dup, _can_uid, _can

                for _ff in ("vin", "color", "location", "miles", "seller_type",
                            "extended_range_confirmed", "er_note",
                            "equipment_511a_confirmed", "equip_note",
                            "history_flag", "history_note", "dos_active"):
                    if _to_filter.get(_ff) and not _canonical.get(_ff):
                        _canonical[_ff] = _to_filter[_ff]
                if _to_filter.get("vin") and _to_filter["vin"] not in vin_index:
                    vin_index[_to_filter["vin"]] = _canonical_uid
                _apply_vin_er(_canonical)
                if (_canonical.get("vin") and
                        _canonical.get("extended_range_confirmed") is None):
                    _er_ws, _note_ws = _fetch_window_sticker_er(_canonical["vin"])
                    if _er_ws is not None:
                        _canonical["extended_range_confirmed"] = _er_ws
                        _canonical["er_note"] = _note_ws
                _to_filter["status"] = "filtered"
                _to_filter["merged_into"] = _canonical_uid
                break  # only merge into first confirmed match

    # ── VIN cross-reference: AT unconfirmed → CarGurus ──────────────────────
    # For AT listings that have a price+location but no VIN (VDP probe blocked),
    # look for a CarGurus entry at the same dealer within $1k of the same price.
    # If exactly one candidate exists (no ambiguity), borrow the VIN and re-run
    # ER detection.  Only fires when the match is unambiguous to avoid false
    # positives at dealers with multiple Lariat listings.
    _cg_vin_by_dealer: dict = {}   # norm_dealer -> [(price, vin, uid)]
    for _uid, _entry in seen.items():
        if _entry.get("source") != "cargurus":
            continue
        if _entry.get("status") in (SOLD, "filtered"):
            continue
        _vin = _entry.get("vin")
        if not _vin:
            continue
        _dn = _norm_dealer(_entry.get("location", ""))
        _pr = _entry.get("price")
        if _dn and _pr:
            _cg_vin_by_dealer.setdefault(_dn, []).append((_pr, _vin, _uid))

    _xref_count = 0
    for _uid, _entry in seen.items():
        if _entry.get("source") != "autotrader":
            continue
        if _entry.get("status") in (SOLD, "filtered"):
            continue
        if _entry.get("vin"):
            continue
        if _entry.get("extended_range_confirmed") is not None:
            continue
        _at_price = _entry.get("price")
        if not _at_price:
            continue
        _at_dealer = _norm_dealer(_entry.get("location", ""))
        if not _at_dealer or len(_at_dealer) < 5:
            continue
        _candidates = [
            (_pr, _vin, _cuid)
            for (_pr, _vin, _cuid) in _cg_vin_by_dealer.get(_at_dealer, [])
            if abs(_pr - _at_price) <= 1000
        ]
        if len(_candidates) != 1:
            continue  # no match or ambiguous (multiple trucks at same dealer in range)
        _match_price, _match_vin, _match_cuid = _candidates[0]
        log.info(
            "VIN xref: AT %s → CG %s via dealer '%s' (price diff $%d), VIN=%s",
            _uid[:16], _match_cuid[:16], _at_dealer[:24],
            abs(_match_price - _at_price), _match_vin[:10],
        )
        _entry["vin"] = _match_vin
        if _match_vin not in vin_index:
            vin_index[_match_vin] = _uid
        _apply_vin_er(_entry)
        # If VIN says SR, filter the listing now
        if _entry.get("extended_range_confirmed") is False:
            _entry["status"] = "filtered"
            log.info(
                "VIN xref: %s confirmed SR via CG dealer match -- filtering",
                _uid[:16],
            )
        elif _entry.get("extended_range_confirmed") is None:
            # VIN position ambiguous -- try window sticker
            _er_ws, _note_ws = _fetch_window_sticker_er(_match_vin)
            if _er_ws is not None:
                _entry["extended_range_confirmed"] = _er_ws
                _entry["er_note"] = _note_ws
        _xref_count += 1

    if _xref_count:
        log.info("VIN xref: resolved %d AT listing(s) from CG dealer match", _xref_count)

    # ── Stale price_drop cleanup ─────────────────────────────────────────────
    # After dedup merges, a canonical entry can end up with current price >=
    # its historical peak (e.g. a higher-priced source updated the price field
    # after a lower-priced AT card triggered the drop flag). Clear those.
    for _uid, _entry in seen.items():
        if _entry.get("status") != PRICE_DROP:
            continue
        _cur = _entry.get("price")
        _hist = _entry.get("price_history") or []
        if not _cur or not _hist:
            continue
        _peak = max((h.get("price") or 0) for h in _hist)
        if _cur >= _peak - 100:
            # Price did not actually drop meaningfully — clear the flag.
            log.debug(
                "Clearing stale price_drop for %s: cur=$%s, peak=$%s",
                _uid[:12], _cur, _peak,
            )
            _entry["status"] = ACTIVE

    # Guard: if a source returned nothing (or suspiciously few) this run, it's likely blocked/failed.
    # Never mark its listings as sold based on an incomplete sweep.
    # Use the observed sweep UUID count if provided; fall back to new_listings count
    if cars_com_sweep_count > 0:
        cars_sweep_count = cars_com_sweep_count
    else:
        cars_sweep_count = sum(1 for l in new_listings if l.get("source") == "cars.com")

    cars_com_active_in_state = sum(
        1 for e in seen.values()
        if e.get("source") == "cars.com"
        and e.get("status") not in ("sold", "filtered")
        and not e.get("dismissed")
    )

    min_required = max(20, int(cars_com_active_in_state * 0.75))
    cars_sweep_ok = cars_sweep_count >= min_required

    if not cars_sweep_ok:
        log.warning(
            "cars.com sweep observed only %d UUIDs (need %d, have %d active in state) — "
            "skipping sold-marking to protect state integrity.",
            cars_sweep_count, min_required, cars_com_active_in_state
        )

    ebay_sweep_ok = any(l.get("source") == "ebay" for l in new_listings)
    if not ebay_sweep_ok:
        log.warning("eBay sweep returned 0 results — skipping sold-marking for all eBay listings to protect state integrity.")

    at_sweep_ok = any(l.get("source") == "autotrader" for l in new_listings)
    if not at_sweep_ok:
        # Only warn if we actually have AutoTrader listings in state to protect
        at_in_state = sum(1 for e in seen.values()
                          if e.get("source") == "autotrader"
                          and e.get("status") not in (SOLD, "filtered"))
        if at_in_state:
            log.warning(
                "AutoTrader sweep returned 0 results — skipping sold-marking "
                "for %d AutoTrader listing(s) to protect state integrity.", at_in_state
            )

    carvana_sweep_ok = any(l.get("source") == "carvana" for l in new_listings)
    if not carvana_sweep_ok:
        carvana_in_state = sum(1 for e in seen.values()
                               if e.get("source") == "carvana"
                               and e.get("status") not in (SOLD, "filtered"))
        if carvana_in_state:
            log.warning(
                "Carvana sweep returned 0 results — skipping sold-marking "
                "for %d Carvana listing(s).", carvana_in_state
            )

    cargurus_sweep_ok = any(l.get("source") == "cargurus" for l in new_listings)
    if not cargurus_sweep_ok:
        cg_in_state = sum(1 for e in seen.values()
                          if e.get("source") == "cargurus"
                          and e.get("status") not in (SOLD, "filtered"))
        if cg_in_state:
            log.warning(
                "CarGurus sweep returned 0 results — skipping sold-marking "
                "for %d CarGurus listing(s) to protect state integrity.", cg_in_state
            )

    edmunds_sweep_ok = any(l.get("source") == "edmunds" for l in new_listings)
    if not edmunds_sweep_ok:
        edm_in_state = sum(1 for e in seen.values()
                           if e.get("source") == "edmunds"
                           and e.get("status") not in (SOLD, "filtered"))
        if edm_in_state:
            log.warning(
                "Edmunds sweep returned 0 results — skipping sold-marking "
                "for %d Edmunds listing(s) to protect state integrity.", edm_in_state
            )

    # Mark listings not seen this run as sold (or filtered if excluded by trim filter).
    # Skipped entirely in --source mode to avoid false sold markings when only one
    # source is tested.
    if skip_sold_marking:
        log.info("Sold-marking skipped (--source mode)")
    else:
        for uid, entry in seen.items():
            if uid in _SUPPRESSED_UUIDS:
                entry["status"] = "filtered"
                continue
            if not cars_sweep_ok and entry.get("source") == "cars.com":
                continue
            if not ebay_sweep_ok and entry.get("source") == "ebay":
                continue
            if not at_sweep_ok and entry.get("source") == "autotrader":
                continue
            if not carvana_sweep_ok and entry.get("source") == "carvana":
                continue
            if not cargurus_sweep_ok and entry.get("source") == "cargurus":
                continue
            if not edmunds_sweep_ok and entry.get("source") == "edmunds":
                continue
            if uid not in current_ids and entry.get("status") not in ("filtered", SOLD):
                title = entry.get("title", "")
                if _is_trim_excluded(title, price=entry.get("price")):
                    entry["status"] = "filtered"
                    entry["sweep_miss_count"] = 0
                    log.info("  FILT %s  %s", uid[:12], title[:40])
                else:
                    miss_count = entry.get("sweep_miss_count", 0) + 1
                    entry["sweep_miss_count"] = miss_count
                    if miss_count >= 2:
                        entry["status"] = SOLD
                        entry["last_seen"] = TODAY
                        entry["sweep_miss_count"] = 0
                        log.info("  SOLD %s  %s (missed %d consecutive sweeps)",
                                 uid[:12], title[:40], miss_count)
                    else:
                        log.debug("  MISS %s  %s (miss %d/2 — not marking sold yet)",
                                  uid[:12], title[:40], miss_count)
            elif uid in current_ids and entry.get("sweep_miss_count", 0) > 0:
                # Listing reappeared — reset miss counter
                entry["sweep_miss_count"] = 0

    return summary


def _new_state_entry(listing: dict, canonical_id: str) -> dict:
    price = listing.get("price")
    return {
        "id":                      canonical_id,
        "source":                  listing.get("source", ""),
        "vin":                     listing.get("vin", ""),
        "title":                   listing.get("title", ""),
        "color":                   listing.get("color", ""),
        "location":                listing.get("location", ""),
        "seller_type":             listing.get("seller_type", "Dealer"),
        "miles":                   listing.get("miles"),
        "price":                   price,
        "price_history":           [{"date": TODAY, "price": price}] if price else [],
        "history_flag":            listing.get("history_flag", "❓ Unknown"),
        "history_note":            listing.get("history_note", ""),
        "extended_range_confirmed": listing.get("extended_range_confirmed"),
        "er_note":                 listing.get("er_note", ""),
        "equipment_511a_confirmed": listing.get("equipment_511a_confirmed"),
        "equip_note":              listing.get("equip_note", ""),
        "azure_gray":              listing.get("azure_gray", False),
        "dos_active":              listing.get("dos_active"),
        "dismissed":               False,
        "timeout_count":           0,
        "vdp_fail_count":          listing.get("vdp_fail_count", 0),
        "last_vdp_visit":          None,
        "first_seen":              TODAY,
        "last_seen":               TODAY,
        # Preserve status="filtered" from pre-merge VDP probe (Fix 1B); else ACTIVE.
        "status":                  listing.get("status", ACTIVE) if listing.get("status") == "filtered" else ACTIVE,
        "link":                    listing.get("link", ""),
    }
