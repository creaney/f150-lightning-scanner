"""
Tests for the _build_listing year-rule guard fix (Phase 1b).

The 2024 Lariat ER rule is authoritative: it must override text-based ER
detection (which can return False when listing text contains comparison copy
like "standard range available"). The VIN is the only thing that outranks it.

Run: python3 -m pytest tests/test_build_listing.py -v
"""
import pytest
import scanner


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _listing(
    title="2024 Ford F-150 Lightning Lariat",
    vin="",
    er_confirmed=None,
    er_note="Unconfirmed",
    equip_confirmed=None,
    equip_note="",
):
    """Call _build_listing with a minimal set of required args."""
    return scanner._build_listing(
        uid="test-1", source="test", vin=vin,
        title=title,
        color="", location="", seller_type="Dealer",
        miles=20_000, price=55_000,
        hist_flag="Unknown", hist_note="",
        er_confirmed=er_confirmed, er_note=er_note,
        equip_confirmed=equip_confirmed, equip_note=equip_note,
        is_azure=False, link="",
    )


# ---------------------------------------------------------------------------
# 2024 Lariat -- year-rule is authoritative
# ---------------------------------------------------------------------------

class Test2024LariatRule:

    def test_text_false_overridden_by_year_rule(self):
        """
        REGRESSION: detect_er returned False (e.g. listing text said "standard range")
        for a genuine 2024 Lariat.  The year rule must override it to True.
        This test must FAIL on the pre-fix code (er_confirmed stays False).
        """
        result = _listing(er_confirmed=False, er_note="SR")
        assert result["extended_range_confirmed"] is True, (
            "2024 Lariat with text-detected SR should be overridden to ER by year rule"
        )
        assert result["er_note"] == "2024 Lariat (ER-only trim)"

    def test_text_none_confirmed_by_year_rule(self):
        """No text signal -> year rule promotes to True."""
        result = _listing(er_confirmed=None, er_note="Unconfirmed")
        assert result["extended_range_confirmed"] is True
        assert result["er_note"] == "2024 Lariat (ER-only trim)"

    def test_text_true_preserved_not_overwritten(self):
        """Text already confirmed ER -> year rule does not clobber the existing note."""
        result = _listing(er_confirmed=True, er_note="text-confirmed ER")
        assert result["extended_range_confirmed"] is True
        # Note from text detection should be preserved since er_confirmed was already True
        assert result["er_note"] == "text-confirmed ER"

    def test_er_vin_confirmed(self):
        """ER VIN (position 4 = '6') plus 2024 Lariat title -> confirmed ER, no conflict."""
        result = _listing(vin="1FT6W1EVXRWG00001", er_confirmed=None)
        assert result["extended_range_confirmed"] is True
        assert "CONFLICT" not in result["er_note"]

    def test_sr_vin_raises_conflict_not_forced_er(self):
        """
        SR VIN (position 4 = 'V') on a 2024 Lariat title is a genuine data conflict.
        The function must surface it rather than silently forcing ER or leaving SR.
        """
        result = _listing(vin="1FTVW1EVXRWG00001", er_confirmed=None)
        assert result["extended_range_confirmed"] is False
        assert "CONFLICT" in result["er_note"]
        assert "SR" in result["er_note"]

    def test_sr_vin_text_detected_sr_conflict(self):
        """SR VIN + text-detected SR -> still surfaces CONFLICT (VIN is the authority)."""
        result = _listing(vin="1FTVW1EVXRWG00001", er_confirmed=False, er_note="SR")
        assert result["extended_range_confirmed"] is False
        assert "CONFLICT" in result["er_note"]

    def test_511a_set_when_not_already_confirmed(self):
        """Year rule also confirms 511A for 2024 Lariat when it was None."""
        result = _listing(er_confirmed=None, equip_confirmed=None)
        assert result["equipment_511a_confirmed"] is True
        assert result["equip_note"] == "2024 Lariat (511A always)"

    def test_511a_not_overwritten_when_already_true(self):
        """Existing 511A confirmation (from text or VIN) is not overwritten."""
        result = _listing(
            er_confirmed=None, equip_confirmed=True, equip_note="511A in text"
        )
        assert result["equipment_511a_confirmed"] is True
        assert result["equip_note"] == "511A in text"

    def test_511a_not_set_on_sr_conflict(self):
        """SR VIN conflict: ER is False, 511A should not be forced True."""
        result = _listing(vin="1FTVW1EVXRWG00001", er_confirmed=None, equip_confirmed=None)
        # With an SR VIN conflict, ER is False and 511A should remain unset
        assert result["extended_range_confirmed"] is False
        assert result["equipment_511a_confirmed"] is None


# ---------------------------------------------------------------------------
# 2023 Lariat -- 511A follows ER confirmation
# ---------------------------------------------------------------------------

class Test2023LariatRule:

    def test_2023_er_confirmed_sets_511a(self):
        """2023 Lariat ER -> 511A is set."""
        result = _listing(
            title="2023 Ford F-150 Lightning Lariat",
            er_confirmed=True, er_note="VIN-confirmed ER",
            equip_confirmed=None,
        )
        assert result["equipment_511a_confirmed"] is True
        assert result["equip_note"] == "2023 Lariat ER (511A always)"

    def test_2023_er_not_confirmed_does_not_set_511a(self):
        """2023 Lariat unconfirmed ER -> 511A not set by year rule."""
        result = _listing(
            title="2023 Ford F-150 Lightning Lariat",
            er_confirmed=None, equip_confirmed=None,
        )
        assert result["equipment_511a_confirmed"] is None

    def test_2023_er_false_does_not_set_511a(self):
        """2023 Lariat SR -> 511A not set."""
        result = _listing(
            title="2023 Ford F-150 Lightning Lariat",
            er_confirmed=False, equip_confirmed=None,
        )
        assert result["equipment_511a_confirmed"] is None

    def test_2023_511a_not_overwritten_when_already_true(self):
        """Pre-existing 511A confirmation is not clobbered."""
        result = _listing(
            title="2023 Ford F-150 Lightning Lariat",
            er_confirmed=True,
            equip_confirmed=True, equip_note="511A detected in text",
        )
        assert result["equip_note"] == "511A detected in text"


# ---------------------------------------------------------------------------
# Non-Lariat and non-2024/2023 titles -- year rule must not fire
# ---------------------------------------------------------------------------

class TestYearRuleDoesNotFire:

    def test_platinum_title_not_affected(self):
        """Platinum is not a Lariat; year rule must not override its ER status."""
        result = _listing(
            title="2024 Ford F-150 Lightning Platinum",
            er_confirmed=False, er_note="SR",
        )
        # Platinum: _is_lariat is False, so er_confirmed stays False
        assert result["extended_range_confirmed"] is False
        assert result["er_note"] == "SR"

    def test_2022_lariat_not_affected(self):
        """Pre-2023 Lariat is outside the year rule scope."""
        result = _listing(
            title="2022 Ford F-150 Lightning Lariat",
            er_confirmed=None, er_note="Unconfirmed",
        )
        # No year rule applies; stays None
        assert result["extended_range_confirmed"] is None

    def test_no_year_in_title_not_affected(self):
        """Title with no year extracted -> year rule does not fire."""
        result = _listing(
            title="Ford F-150 Lightning Lariat",
            er_confirmed=None,
        )
        assert result["extended_range_confirmed"] is None
