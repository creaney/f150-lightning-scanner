"""
Fixture-based detection tests using real listings from seen-listings.json.

Truth labels are in tests/fixtures/listings.json.
Entries where true_er='UNKNOWN' require manual confirmation before Phase 1.

SR entries with er_confirmed=true in the DB are the Phase 1 targets -- they
are currently mislabeled and their tests are marked xfail.
"""
import json
import pytest
from pathlib import Path
import scanner

FIXTURES = json.loads((Path(__file__).parent / "fixtures" / "listings.json").read_text())


def _detect_er_for_fixture(f: dict):
    """Run detect_er using just VIN (no raw listing text stored in fixtures)."""
    return scanner.detect_er(f.get("vin", ""), "")


# Separate fixtures by type for parametrize
ER_VIN_FIXTURES = [f for f in FIXTURES if f["true_er"] == "ER"]
SR_VIN_FIXTURES = [f for f in FIXTURES if f["true_er"] == "SR"]
UNKNOWN_ER_FIXTURES = [f for f in FIXTURES if f["true_er"] == "UNKNOWN"]


@pytest.mark.parametrize("fixture", ER_VIN_FIXTURES, ids=[f["id"][:8] for f in ER_VIN_FIXTURES])
def test_er_vin_detected_as_er(fixture):
    confirmed, note = _detect_er_for_fixture(fixture)
    assert confirmed is True, (
        f"Expected ER for VIN {fixture['vin']} ({fixture['title']}), got confirmed={confirmed} ({note})"
    )


@pytest.mark.parametrize("fixture", SR_VIN_FIXTURES, ids=[f["id"][:8] for f in SR_VIN_FIXTURES])
def test_sr_vin_detected_as_sr(fixture):
    # detect_er with VIN[3]='V' already returns False correctly.
    # The live detection path is fine; the stored state in seen-listings.json is wrong.
    confirmed, note = _detect_er_for_fixture(fixture)
    assert confirmed is False, (
        f"Expected SR for VIN {fixture['vin']} ({fixture['title']}), got confirmed={confirmed} ({note})"
    )


@pytest.mark.parametrize("fixture", SR_VIN_FIXTURES, ids=[f["id"][:8] for f in SR_VIN_FIXTURES])
def test_sr_vin_state_is_not_mislabeled(fixture):
    """The DB entry for this SR VIN must NOT have extended_range_confirmed=True."""
    import json
    from pathlib import Path
    state = json.loads((Path(__file__).parent.parent / "seen-listings.json").read_text())
    entry = state["listings"].get(fixture["id"])
    if entry is None:
        pytest.skip("listing not in state file")
    assert entry.get("extended_range_confirmed") is not True, (
        f"SR VIN {fixture['vin']} ({fixture['title']}) is stored as ER in seen-listings.json"
    )


@pytest.mark.parametrize("fixture", UNKNOWN_ER_FIXTURES, ids=[f["id"][:8] for f in UNKNOWN_ER_FIXTURES])
def test_unknown_er_no_vin_is_unconfirmed(fixture):
    # No VIN + no text = unconfirmed. This is the correct behavior.
    confirmed, _ = _detect_er_for_fixture(fixture)
    assert confirmed is None


def test_azure_gray_detected(fixture=None):
    azure = next(f for f in FIXTURES if (f.get("color") or "").lower() == "azure gray")
    assert scanner.detect_azure_gray(azure["color"]) is True


def test_deal_score_returns_int_for_priced_listings():
    for f in FIXTURES:
        if f.get("price"):
            score = scanner.deal_score(f)
            assert score is None or isinstance(score, int), (
                f"deal_score returned {score!r} for listing {f['id'][:8]}"
            )
