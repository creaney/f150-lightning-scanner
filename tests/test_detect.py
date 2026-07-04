"""
Characterization tests for detection and scoring functions.
These pin current behavior so later phases can prove they changed nothing.

xfail items are confirmed bugs. Phase 1 must flip them to passing.
"""
import pytest
import scanner


# ── _vin_battery ─────────────────────────────────────────────────────────────

class TestVinBattery:
    def test_er_char_returns_ER(self):
        # Real ER VIN prefix: 1FT6W1EV (VIN[3] = '6')
        assert scanner._vin_battery("1FT6W1EV3PWG45591") == "ER"

    def test_er_char_minimal(self):
        assert scanner._vin_battery("1FT6") == "ER"

    def test_sr_char_returns_SR(self):
        # Real SR VIN prefix: 1FTVW1EV (VIN[3] = 'V')
        assert scanner._vin_battery("1FTVW1EV4PWG10330") == "SR"

    def test_sr_char_minimal(self):
        assert scanner._vin_battery("1FTV") == "SR"

    def test_short_vin_returns_none(self):
        assert scanner._vin_battery("1FT") is None

    def test_empty_string_returns_none(self):
        assert scanner._vin_battery("") is None

    def test_none_returns_none(self):
        assert scanner._vin_battery(None) is None

    def test_garbage_vin_returns_none(self):
        # Position 4 char is not '6' or 'V'
        assert scanner._vin_battery("XXXZXXXX") is None

    def test_case_insensitive_sr(self):
        # 'v' lowercase should still match SR
        assert scanner._vin_battery("1FTvW1EV4PWG10330") == "SR"


# ── detect_er ────────────────────────────────────────────────────────────────

class TestDetectEr:
    # --- VIN wins ---

    def test_er_vin_wins_regardless_of_text(self):
        confirmed, note = scanner.detect_er("1FT6W1EV3PWG45591", "standard range battery")
        assert confirmed is True
        assert "VIN" in note

    def test_sr_vin_wins_regardless_of_text(self):
        confirmed, note = scanner.detect_er("1FTVW1EV4PWG10330", "extended range moonroof bluecruise")
        assert confirmed is False
        assert "SR" in note

    def test_er_vin_with_no_text(self):
        confirmed, _ = scanner.detect_er("1FT6W1EV4PWG11482", "")
        assert confirmed is True

    def test_sr_vin_with_no_text(self):
        confirmed, _ = scanner.detect_er("1FTVW1EV8PWG26580", "")
        assert confirmed is False

    # --- No VIN, text-only ---

    def test_no_vin_explicit_extended_range_label(self):
        confirmed, note = scanner.detect_er("", "Extended Range battery 131 kWh moonroof")
        assert confirmed is True

    def test_no_vin_explicit_standard_range_label(self):
        confirmed, note = scanner.detect_er("", "Standard Range 98 kWh well maintained")
        assert confirmed is False

    def test_no_vin_no_signals_unconfirmed(self):
        confirmed, _ = scanner.detect_er("", "Great truck, low miles, one owner")
        assert confirmed is None

    def test_no_vin_negative_beats_no_positive(self):
        # Only negative signals -> SR
        confirmed, _ = scanner.detect_er("", "98 kWh battery, great deal")
        assert confirmed is False

    # --- Phase 1 fixes: these were xfail in Phase 0, now must pass ---

    def test_negative_wins_over_positive_text(self):
        # Explicit "Standard Range" beats any positive signal including "extended range".
        # Negative always wins when both are present in text with no VIN.
        confirmed, _ = scanner.detect_er("", "Standard Range, extended range available as upgrade")
        assert confirmed is False

    def test_bluecruise_alone_does_not_confirm_er(self):
        # "bluecruise" is a hint, not a strong signal -- cannot confirm ER without VIN.
        confirmed, _ = scanner.detect_er("", "bluecruise")
        assert confirmed is None

    def test_moonroof_alone_does_not_confirm_er(self):
        confirmed, _ = scanner.detect_er("", "panoramic moonroof, great truck")
        assert confirmed is None

    # --- Strong text signals ---

    def test_131kwh_confirms_er_without_vin(self):
        confirmed, note = scanner.detect_er("", "131 kWh battery, like new")
        assert confirmed is True

    def test_98kwh_confirms_sr_without_vin(self):
        confirmed, _ = scanner.detect_er("", "98 kWh battery, standard range")
        assert confirmed is False

    def test_511a_in_text_confirms_er(self):
        confirmed, _ = scanner.detect_er("", "equipment group 511A, fully loaded")
        assert confirmed is True

    def test_negative_beats_strong_signal(self):
        # If listing somehow has both 131 kWh and standard range, negative wins.
        confirmed, _ = scanner.detect_er("", "131 kWh standard range battery")
        assert confirmed is False

    def test_hint_returns_unconfirmed_not_none_note(self):
        # Hints should return None with a note indicating "suggests"
        confirmed, note = scanner.detect_er("", "moonroof, pro power onboard")
        assert confirmed is None
        assert "suggest" in note.lower() or "unconfirmed" in note.lower()


# ── detect_511a ──────────────────────────────────────────────────────────────

class TestDetect511a:
    def test_explicit_511a_returns_true(self):
        confirmed, note = scanner.detect_511a("Equipment Group 511A moonroof")
        assert confirmed is True
        assert "511A" in note

    def test_explicit_511a_with_dash(self):
        confirmed, _ = scanner.detect_511a("511-A package")
        assert confirmed is True

    def test_two_keyword_signals_returns_true(self):
        # moonroof + bluecruise = 2 signals
        confirmed, note = scanner.detect_511a("panoramic moonroof and BlueCruise")
        assert confirmed is True
        assert "signal" in note

    def test_one_keyword_signal_returns_none(self):
        confirmed, note = scanner.detect_511a("moonroof only, great truck")
        assert confirmed is None
        assert "1 signal" in note

    def test_no_signals_returns_none(self):
        confirmed, note = scanner.detect_511a("Great truck, low miles")
        assert confirmed is None

    def test_empty_text_returns_none(self):
        confirmed, _ = scanner.detect_511a("")
        assert confirmed is None

    def test_bang_and_olufsen_plus_moonroof(self):
        confirmed, _ = scanner.detect_511a("B&O sound system, moonroof installed")
        assert confirmed is True

    def test_trailer_backup_plus_max_tow(self):
        confirmed, _ = scanner.detect_511a("trailer backup assist and max trailer tow package")
        assert confirmed is True


# ── deal_score ────────────────────────────────────────────────────────────────

class TestDealScore:
    """
    Pin deal_score behavior against an EXPLICIT baseline.

    Phase 4 changed the signature to deal_score(listing, baseline, er_floor).
    These tests pass baseline=44_000 explicitly so they are not silently bound
    to whatever _DEAL_BASELINE happens to be set to at runtime.  The star values
    are identical to the pre-Phase-4 values because the math is unchanged; only
    the source of the baseline moved (constant -> caller-supplied).
    """

    BASE = {
        "price": 44_000,
        "miles": 20_000,
        "history_flag": "Clean",
        "seller_type": "Dealer",
        "title": "Used 2023 Ford F-150 Lightning LARIAT",
        "extended_range_confirmed": True,
    }
    BASELINE = 44_000   # explicit: matches old _DEAL_BASELINE so values are stable

    def _score(self, **overrides):
        listing = {**self.BASE, **overrides}
        return scanner.deal_score(listing, baseline=self.BASELINE)

    def test_at_baseline_price_is_3_stars(self):
        # $44k at 20k miles = exactly the baseline -> delta=0 -> 3 stars
        assert self._score() == 3

    def test_strong_deal_5_stars(self):
        # $40k at 20k miles -> delta = +4000 -> 5 stars
        assert self._score(price=40_000) == 5

    def test_good_deal_4_stars(self):
        # $42k at 20k miles -> delta = +2000 -> 4 stars
        assert self._score(price=42_000) == 4

    def test_overpriced_1_star(self):
        # $50k at 20k miles -> delta = -6000 -> 1 star
        assert self._score(price=50_000) == 1

    def test_accident_penalizes(self):
        # Accident adds $1500 to adj, reducing delta
        normal = self._score(price=44_000)
        accident = self._score(price=44_000, history_flag="Accident reported")
        assert accident <= normal

    def test_buyback_penalizes_heavily(self):
        normal = self._score(price=44_000)
        buyback = self._score(price=44_000, history_flag="Buyback title")
        assert buyback < normal

    def test_cpo_improves_score(self):
        normal = self._score(price=44_000)
        cpo = self._score(price=44_000, seller_type="CPO")
        assert cpo >= normal

    def test_platinum_improves_score(self):
        normal = self._score(price=44_000)
        platinum = self._score(price=44_000, title="Used 2023 Ford F-150 Lightning Platinum")
        assert platinum > normal

    def test_no_price_returns_none(self):
        assert self._score(price=None) is None

    def test_high_mileage_penalizes(self):
        low = self._score(miles=10_000)
        high = self._score(miles=60_000)
        assert high < low

    def test_mileage_rate(self):
        # Each 1000 miles above 20k adds $120 (0.12/mile * 1000)
        base = self._score(price=44_000, miles=20_000)
        higher_miles = self._score(price=44_000, miles=46_000)
        # 26k extra miles = $3120 penalty -> effective price $47,120 -> delta = -3120 -> 1 star
        assert higher_miles == 1
        assert base == 3


# ── compute_market_baseline ───────────────────────────────────────────────────

def _make_er_listing(price, miles=20_000, title="2023 Ford F-150 Lightning Lariat"):
    """Helper: minimal ER-confirmed Lariat listing dict."""
    return {
        "price": price,
        "miles": miles,
        "extended_range_confirmed": True,
        "history_flag": "Clean",
        "seller_type": "Dealer",
        "title": title,
        "status": "active",
    }


class TestComputeMarketBaseline:

    def test_provisional_when_too_few_listings(self):
        # Fewer than _MARKET_BASELINE_MIN_SAMPLE (8) confirmed-ER listings -> provisional
        listings = [_make_er_listing(50_000) for _ in range(5)]
        baseline, is_provisional, er_floor, _ = scanner.compute_market_baseline(listings)
        assert is_provisional is True
        assert er_floor is None
        # Fallback value is the constant
        assert baseline == scanner._DEAL_BASELINE

    def test_provisional_path_does_not_crash_with_empty_input(self):
        baseline, is_provisional, er_floor, _ = scanner.compute_market_baseline([])
        assert is_provisional is True
        assert baseline == scanner._DEAL_BASELINE

    def test_median_of_odd_sample(self):
        # 9 listings at known adj prices -> median is 5th value
        prices = [40_000, 42_000, 44_000, 46_000, 48_000, 50_000, 52_000, 54_000, 56_000]
        listings = [_make_er_listing(p) for p in prices]
        baseline, is_provisional, er_floor, _ = scanner.compute_market_baseline(listings)
        assert is_provisional is False
        assert baseline == 48_000   # 5th of 9 = median
        assert er_floor is not None

    def test_median_of_even_sample(self):
        # 10 listings -> median = average of 5th and 6th
        prices = [40_000, 42_000, 44_000, 46_000, 48_000, 50_000, 52_000, 54_000, 56_000, 58_000]
        listings = [_make_er_listing(p) for p in prices]
        baseline, is_provisional, er_floor, _ = scanner.compute_market_baseline(listings)
        assert is_provisional is False
        assert baseline == 49_000   # (48_000 + 50_000) // 2

    def test_excludes_sr_confirmed_listings(self):
        # SR listings must not influence the baseline
        er_listings = [_make_er_listing(50_000) for _ in range(8)]
        sr = {**_make_er_listing(30_000), "extended_range_confirmed": False}
        baseline, _, _, _ = scanner.compute_market_baseline(er_listings + [sr])
        assert baseline == 50_000  # SR listing excluded; all ER at 50k

    def test_excludes_unpriced_listings(self):
        # Listings with no valid price are excluded from the comparable set
        er_listings = [_make_er_listing(50_000) for _ in range(8)]
        no_price = {**_make_er_listing(50_000), "price": None}
        baseline, _, _, _ = scanner.compute_market_baseline(er_listings + [no_price])
        assert baseline == 50_000

    def test_platinum_excluded_from_comparable_set(self):
        # Platinum-only listings should not drag the baseline up
        er_lariats = [_make_er_listing(50_000) for _ in range(8)]
        platinum = _make_er_listing(70_000, title="2023 Ford F-150 Lightning Platinum")
        baseline, _, _, _ = scanner.compute_market_baseline(er_lariats + [platinum])
        assert baseline == 50_000  # Platinum excluded


# ── deal_score with live baseline ─────────────────────────────────────────────

class TestDealScoreRelative:
    """
    Prove deal_score is relative to the supplied baseline, not hardcoded.
    The same truck must earn more stars when the market moves up.
    """

    TRUCK = {
        "price": 49_000,
        "miles": 20_000,
        "history_flag": "Clean",
        "seller_type": "Dealer",
        "title": "2023 Ford F-150 Lightning Lariat",
        "extended_range_confirmed": True,
    }

    def test_score_improves_as_market_rises(self):
        # Market at $48k: truck at $49k is $1k above market -> 2 stars
        # Market at $55k: same truck at $49k is $6k below market -> 5 stars
        low_market_score  = scanner.deal_score(self.TRUCK, baseline=48_000)
        high_market_score = scanner.deal_score(self.TRUCK, baseline=55_000)
        assert high_market_score > low_market_score

    def test_low_market_exact_values(self):
        # $49k truck vs $48k baseline: delta = -1000 -> exactly the 2-star boundary (not > -1000)
        assert scanner.deal_score(self.TRUCK, baseline=48_000) == 2

    def test_high_market_exact_values(self):
        # $49k truck vs $55k baseline: delta = 6000 -> 5 stars
        assert scanner.deal_score(self.TRUCK, baseline=55_000) == 5

    def test_same_truck_two_market_snapshots(self):
        # End-to-end: build two market sets, compute baselines, score same truck
        low_market  = [_make_er_listing(48_000) for _ in range(9)]
        high_market = [_make_er_listing(55_000) for _ in range(9)]

        b_low,  _, floor_low,  _ = scanner.compute_market_baseline(low_market)
        b_high, _, floor_high, _ = scanner.compute_market_baseline(high_market)

        score_low  = scanner.deal_score(self.TRUCK, baseline=b_low,  er_floor=floor_low)
        score_high = scanner.deal_score(self.TRUCK, baseline=b_high, er_floor=floor_high)

        assert score_high > score_low, (
            f"Expected higher market to produce better score for same truck; "
            f"got {score_low} (baseline={b_low}) vs {score_high} (baseline={b_high})"
        )


# ── er_confidence_tier ────────────────────────────────────────────────────────

class TestErConfidenceTier:
    """Prove ER-confidence tiers produce correct sort ordering."""

    def _listing(self, er, price=50_000):
        return {
            "extended_range_confirmed": er,
            "price": price,
            "miles": 20_000,
            "history_flag": "Clean",
            "seller_type": "Dealer",
            "title": "2023 Ford F-150 Lightning Lariat",
        }

    def test_confirmed_er_is_tier_0(self):
        assert scanner.er_confidence_tier(self._listing(True)) == 0

    def test_confirmed_sr_is_tier_2(self):
        # SR-confirmed should already be filtered; tier 2 is the safe fallback
        assert scanner.er_confidence_tier(self._listing(False)) == 2

    def test_unconfirmed_without_floor_is_tier_1(self):
        assert scanner.er_confidence_tier(self._listing(None)) == 1

    def test_unconfirmed_above_floor_is_tier_1(self):
        # Price above the ER floor -> not likely SR
        assert scanner.er_confidence_tier(self._listing(None, price=52_000), er_floor=45_000) == 1

    def test_unconfirmed_below_floor_is_tier_2(self):
        # Price well below the ER floor -> likely SR priced out
        assert scanner.er_confidence_tier(self._listing(None, price=38_000), er_floor=45_000) == 2

    def test_confirmed_er_always_tier_0_regardless_of_floor(self):
        # A cheap confirmed-ER truck (auction, salvage, anything) is still tier 0
        assert scanner.er_confidence_tier(self._listing(True, price=30_000), er_floor=45_000) == 0


# ── ER cap on likely-SR unconfirmed listings ──────────────────────────────────

class TestDealScoreErFloorCap:
    """
    An unconfirmed listing priced below the ER floor must be capped at 2 stars
    even if its raw delta would otherwise earn 4 or 5 stars.
    """

    TRUCK = {
        "price": 38_000,
        "miles": 20_000,
        "history_flag": "Clean",
        "seller_type": "Dealer",
        "title": "2023 Ford F-150 Lightning Lariat",
        "extended_range_confirmed": None,  # unconfirmed
    }
    BASELINE  = 52_000   # live market
    ER_FLOOR  = 45_000   # 10th pct of ER comparable set

    def test_likely_sr_unconfirmed_capped_at_2(self):
        # adj=$38k, baseline=$52k -> delta=+$14k -> would be 5 stars without cap
        # but price < er_floor -> capped at 2
        score = scanner.deal_score(self.TRUCK, baseline=self.BASELINE, er_floor=self.ER_FLOOR)
        assert score == 2

    def test_confirmed_er_not_capped(self):
        # Same price, but confirmed ER -> no cap, gets full 5 stars
        er_truck = {**self.TRUCK, "extended_range_confirmed": True}
        score = scanner.deal_score(er_truck, baseline=self.BASELINE, er_floor=self.ER_FLOOR)
        assert score == 5

    def test_unconfirmed_above_floor_not_capped(self):
        # Price above floor -> no cap applied, normal scoring
        truck_above = {**self.TRUCK, "price": 50_000}
        score = scanner.deal_score(truck_above, baseline=self.BASELINE, er_floor=self.ER_FLOOR)
        # adj=$50k, baseline=$52k -> delta=+$2k -> 4 stars (no cap needed)
        assert score == 4

    def test_no_floor_no_cap(self):
        # Without an er_floor, unconfirmed listings are never capped
        score = scanner.deal_score(self.TRUCK, baseline=self.BASELINE, er_floor=None)
        assert score == 5  # full score, no floor check
