"""scanner/score.py -- deal scoring and market baseline."""
import re
from datetime import date
from typing import Optional

from scanner.config import (
    _PRICE_MIN, _PRICE_MAX, _DEAL_BASELINE, _MILES_RATE, _MARKET_BASELINE_MIN_SAMPLE,
)
from scanner.detect import _price_valid


def _adj_price(listing: dict) -> int:
    """
    Compute the mileage- and quality-adjusted price for a listing.

    The same adjustments are applied in deal_score() and compute_market_baseline()
    so price and baseline are always on equal footing.

    Reference mileage: 20,000 miles.  Each mile above/below that adds/subtracts
    _MILES_RATE (currently $0.12).  History penalties and CPO/Platinum credits are
    also folded in so the baseline reflects what the market is actually paying for
    clean, lower-mileage trucks.
    """
    price = listing.get("price") or 0
    miles = listing.get("miles") or 20_000
    adj = price + (miles - 20_000) * _MILES_RATE
    hist = listing.get("history_flag", "")
    if "Accident" in hist:
        adj += 1_500
    elif "Buyback" in hist or "Salvage" in hist:
        adj += 4_000
    if listing.get("seller_type") == "CPO":
        adj -= 1_500
    # Platinum has more features than Lariat at same price -- treat as $5k better value
    if re.search(r"\bPlatinum\b", listing.get("title", ""), re.I):
        adj -= 5_000
    return int(adj)


def _score_from_delta(delta: int) -> int:
    """Map baseline-minus-adjusted-price delta to a 1-5 star score."""
    if delta >= 3_000:  return 5
    if delta >= 1_000:  return 4
    if delta > -1_000:  return 3
    if delta > -3_000:  return 2
    return 1


def compute_market_baseline(
    active_listings: list,
) -> tuple:
    """
    Compute a live market reference from the active ER-confirmed Lariat comparable set.

    The comparable set is: active, extended_range_confirmed=True, Lariat trim
    (Platinum-only excluded -- different value tier), with a valid price.

    Returns (baseline, is_provisional, er_floor, label):
      baseline       -- median adjusted price; _DEAL_BASELINE constant if sample too small
      is_provisional -- True when sample < _MARKET_BASELINE_MIN_SAMPLE
      er_floor       -- 10th-percentile adjusted price used to flag likely-SR unconfirmed
                        listings; None when provisional
      label          -- "2023" or "all years" describing the comparable set used

    Rationale for median over mean: a handful of overpriced outlier listings (dealer
    markups, cherry-picked inventory) would drag the mean up and make every truck look
    like a deal relative to those outliers.  The median is more stable.
    """
    def _comp_year(listing: dict) -> int:
        """Extract model year from stored field or title fallback."""
        yr = listing.get("year")
        if yr:
            return int(yr)
        m = re.search(r'\b(202[0-9])\b', listing.get("title", ""))
        return int(m.group(1)) if m else 0

    def _is_comp(listing: dict, year_filter: int | None = None) -> bool:
        if listing.get("extended_range_confirmed") is not True:
            return False
        if not _price_valid(listing.get("price")):
            return False
        title = listing.get("title", "")
        if re.search(r"\bPlatinum\b", title, re.I) and not re.search(r"\bLariat\b", title, re.I):
            return False
        if year_filter and _comp_year(listing) != year_filter:
            return False
        return True

    # Prefer 2023-only comparable set; fall back to all years if sample too small.
    adjs_2023 = [_adj_price(l) for l in active_listings if _is_comp(l, year_filter=2023)]
    if len(adjs_2023) >= _MARKET_BASELINE_MIN_SAMPLE:
        adjs = adjs_2023
        _baseline_label = "2023"
    else:
        adjs = [_adj_price(l) for l in active_listings if _is_comp(l)]
        _baseline_label = "all years"
        if len(adjs) < _MARKET_BASELINE_MIN_SAMPLE:
            return _DEAL_BASELINE, True, None, _baseline_label

    adjs_sorted = sorted(adjs)
    n = len(adjs_sorted)
    mid = n // 2
    baseline = (
        (adjs_sorted[mid - 1] + adjs_sorted[mid]) // 2
        if n % 2 == 0
        else adjs_sorted[mid]
    )
    # 10th percentile: floor below which unconfirmed listings are likely SR
    p10_idx = max(0, int(n * 0.10) - 1)
    er_floor = adjs_sorted[p10_idx]
    return baseline, False, er_floor, _baseline_label


def deal_score(
    listing: dict,
    baseline: int = _DEAL_BASELINE,
    er_floor: Optional[int] = None,
) -> Optional[int]:
    """
    Score 1-5 based on price vs. live market median, mileage, history, and CPO status.
    Returns None if price is unknown or outside the valid range.

    baseline: median adjusted price of the ER-confirmed Lariat comparable set for this
              run, computed by compute_market_baseline().  Defaults to _DEAL_BASELINE
              ($44k) when called without a live baseline (provisional / fallback mode).

    er_floor: 10th-percentile adjusted price of the same comparable set.  An unconfirmed
              listing whose adjusted price falls below this floor is likely SR; it is
              capped at 2 stars so a cheap probable-SR truck cannot earn top ratings.
              Pass None (or omit) to skip the cap.

    The five bands are symmetric around the baseline:
      +$3k or more above baseline -> 5 stars   (exceptional deal)
      +$1k to +$3k                -> 4 stars   (good deal)
      -$1k to +$1k                -> 3 stars   (fair market)
      -$3k to -$1k                -> 2 stars   (above market)
      more than $3k above market  -> 1 star    (overpriced)
    """
    if not _price_valid(listing.get("price")):
        return None
    adj = _adj_price(listing)
    score = _score_from_delta(baseline - adj)
    # Likely-SR cap: unconfirmed listing priced below the ER floor is capped at 2 stars.
    # This prevents a cheap probable-SR truck from outranking confirmed-ER trucks.
    if (
        er_floor is not None
        and listing.get("extended_range_confirmed") is None
        and adj < er_floor
    ):
        score = min(score, 2)
    return score


def er_confidence_tier(listing: dict, er_floor: Optional[int] = None) -> int:
    """
    Return a sort tier for ER confidence (lower = better; surfaces first in the report).

    0 -- confirmed ER (extended_range_confirmed=True)
    1 -- unconfirmed, not obviously SR
    2 -- unconfirmed AND adjusted price below ER floor (likely SR priced out)

    Confirmed SR (extended_range_confirmed=False) should be filtered before ranking;
    this function returns 2 for it as a safe fallback.
    """
    er = listing.get("extended_range_confirmed")
    if er is True:
        return 0
    if er is False:
        return 2   # should be filtered already; safe fallback
    # Unconfirmed -- check price evidence
    if er_floor is not None and _price_valid(listing.get("price")):
        if _adj_price(listing) < er_floor:
            return 2
    return 1


def deal_stars(score: int) -> str:
    return "★" * score + "☆" * (5 - score)


def days_on_market(entry: dict) -> tuple:
    """
    Return (days: int, true_dom: bool) for a listing.

    true_dom=True  -- value comes from dosActive (Visor-provided true listing age)
    true_dom=False -- value derived from first_seen (our observation date, a floor)

    Returns (0, False) when neither field is available.
    """
    dos = entry.get("dos_active")
    if dos is not None:
        return int(dos), True
    first = entry.get("first_seen", "")
    if first:
        try:
            delta = (date.today() - date.fromisoformat(first[:10])).days
            return max(0, delta), False
        except (ValueError, TypeError):
            pass
    return 0, False


def market_delta(entry: dict, baseline: int) -> Optional[int]:
    """
    Return baseline minus adjusted price (positive = under market = good deal).
    None when price is unknown or invalid.
    """
    if not _price_valid(entry.get("price")):
        return None
    return baseline - _adj_price(entry)


def price_drop_summary(entry: dict) -> str:
    """
    Compact price-drop indicator for a row: 'Nx -$Y,YYY last' or '' if no drops.

    Counts events where price fell by >= $100 and reports the most recent drop amount.
    """
    hist = entry.get("price_history") or []
    drops = []
    for i in range(1, len(hist)):
        prev = hist[i - 1].get("price")
        curr = hist[i].get("price")
        if prev and curr and (prev - curr) >= 100:
            drops.append(prev - curr)
    if not drops:
        return ""
    n = len(drops)
    last = drops[-1]
    return f"{n}x -${last:,}"
