"""
scanner/config.py -- all compile-time constants for the Lightning Scanner.

Extracted from scanner/__init__.py (Phase 3 split).
Nothing in this module imports from other scanner submodules.
"""

import io
import logging
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import requests

# ── logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


class _SuppressedStdout(io.TextIOWrapper):
    """Filters nodriver cleanup noise from stdout."""
    _SUPPRESS = (
        "successfully removed temp profile",
        "successfully removed temp",
    )

    def __init__(self, wrapped):
        self._wrapped = wrapped

    def write(self, text):
        if any(s in text for s in self._SUPPRESS):
            return len(text)
        return self._wrapped.write(text)

    def flush(self):
        return self._wrapped.flush()

    def __getattr__(self, name):
        return getattr(self._wrapped, name)


# Suppress nodriver's verbose startup/teardown output
logging.getLogger("nodriver").setLevel(logging.ERROR)
logging.getLogger("nodriver.core").setLevel(logging.ERROR)
logging.getLogger("nodriver.core.browser").setLevel(logging.ERROR)
logging.getLogger("nodriver.core.connection").setLevel(logging.ERROR)
logging.getLogger("nodriver.core.tab").setLevel(logging.ERROR)
logging.getLogger("nodriver.core.element").setLevel(logging.ERROR)
logging.getLogger("nodriver.core.config").setLevel(logging.ERROR)
logging.getLogger("websockets").setLevel(logging.ERROR)
logging.getLogger("websockets.client").setLevel(logging.ERROR)
logging.getLogger("asyncio").setLevel(logging.ERROR)
sys.stdout = _SuppressedStdout(sys.stdout)

# ── module-level run flags ────────────────────────────────────────────────
_EDMUNDS_DIAG_DONE: bool = False   # suppress repeated per-page diagnostic

# ── paths ──────────────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent.parent
STATE_FILE  = BASE_DIR / "seen-listings.json"
REPORTS_DIR = BASE_DIR / "reports"
TODAY       = date.today().isoformat()
YESTERDAY   = str(date.today() - timedelta(days=1))
RUN_TS      = datetime.now().strftime("%Y-%m-%d  %H:%M")

# ── shared HTTP session ────────────────────────────────────────────────────
S = requests.Session()
S.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "DNT":             "1",
})

# ── cars.com ───────────────────────────────────────────────────────────────
# CRITICAL: never add models[]=ford-f150_lightning with keyword -- cars.com bug returns 0 results.
CARS_BASE_URL = "https://www.cars.com/shopping/results/"
CARS_KEYWORDS = [
    "511A Lightning",
    "moonroof extended range lightning",
    "bluecruise extended range lightning",
]
CARS_BASE_PARAMS = {
    "makes[]":          "ford",
    "stock_type":       "used",
    "maximum_distance": "all",
    "year_max":         "2024",
    "year_min":         "2023",
    "sort":             "best_match_desc",
}

# ── eBay Motors ────────────────────────────────────────────────────────────
_EBAY_BASE = (
    "https://www.ebay.com/sch/Cars-Trucks/6001/i.html"
    "?LH_ItemCondition=3000&_sop=15&LH_PrefLoc=1&_nkw="
)
EBAY_KEYWORDS = [
    "f-150+lightning+lariat+extended+range",
    "f150+lightning+511A",
    "lightning+lariat+131+kwh",
]

# ── Carvana ────────────────────────────────────────────────────────────────
CARVANA_URL = "https://www.carvana.com/cars/ford-f150-lightning"

# ── Search geography ───────────────────────────────────────────────────────
# ZIP code used as the geographic center for distance-based searches.
# All sources use searchRadius=0 / distance=2000 (nationwide), so this only
# affects result ordering on some sources.  Any US ZIP works.
SEARCH_ZIP = "10001"

# ── CarGurus ───────────────────────────────────────────────────────────────
# distance=2000 = nationwide; year params narrow to 2023-2024; SEARCH_ZIP sets center.
# Pagination via ?page=N.  Pages 2-N fetched via in-browser ?_data= Remix endpoint.
CARGURUS_URL = (
    "https://www.cargurus.com/Cars/l-Used-Ford-F-150-Lightning-d3147"
    f"?startYear=2023&endYear=2024&distance=2000&zip={SEARCH_ZIP}"
)

# ── Edmunds ────────────────────────────────────────────────────────────────
# radius=2000 = nationwide; year params narrow to 2023-2024; SEARCH_ZIP sets center.
# trims=Lariat attempts server-side trim filtering (unverified; falls back to
# client-side trim filter in _parse_edmunds_inventories if ignored by Edmunds).
# Pagination via ?pagenumber=N.  Data in window.EDM.preloadedState (SSR).
EDMUNDS_URL = (
    "https://www.edmunds.com/used-ford-f-150-lightning/"
    f"?year_min=2023&year_max=2024&radius=2000&location={SEARCH_ZIP}&trims=Lariat"
)

# ── CarMax ─────────────────────────────────────────────────────────────────
CARMAX_BROWSE_URL = "https://www.carmax.com/cars?search=F-150+Lightning"

# ── AutoTrader ─────────────────────────────────────────────────────────────
# URL confirmed 2025-05: new slug is f150-lightning (no hyphens), city segment required.
# searchRadius=0 = nationwide; numRecords=100 for max results per page.
AT_SEARCH_URL = (
    "https://www.autotrader.com/cars-for-sale/ford/f150-lightning/new-york-ny"
    f"?zip={SEARCH_ZIP}&startYear=2023&endYear=2024&numRecords=100&searchRadius=0"
)

# ── VIN battery detection ──────────────────────────────────────────────────
# F-150 Lightning VIN structure (positions 1-indexed):
#   1-3  : WMI  = "1FT"  (USA / Ford / F-series)
#   4    : Body = '6'  -> Extended Range 131 kWh  ~320 mi EPA
#                'V'  -> Standard Range   98 kWh  ~230 mi EPA
#   5-7  : "W1E"  (engine / restraint / check digit -- constant for Lightning)
#   8    : varies; NOT the battery indicator (old assumption was wrong)
#   9    : check digit
#   10   : model year  (P=2023, R=2024, ...)
#   11-17: plant + sequence
#
# Examples:
#   1FT6W1EV...  -> Extended Range   (VIN[3]='6')
#   1FTVW1EV...  -> Standard Range   (VIN[3]='V')
_VIN_ER_CHAR = "6"   # VIN index 3 (position 4) = '6' -> ER
_VIN_SR_CHAR = "V"   # VIN index 3 (position 4) = 'V' -> SR
# Accept any alphanumeric at position 8 -- it is NOT the range indicator.
_VIN_RE = re.compile(r"\b(1FT[A-Z0-9]W1E[A-Z0-9][0-9A-HJ-NPR-Z]{9})\b", re.I)

_DEALER_SUFFIX_RE = re.compile(
    r"\b(sales\s+inc|sales\s+llc|sales|inc|llc|motors|auto\s+group|"
    r"group|auto|ltd|corp|dealership|of\s+\w+)\s*$"
)

# ── report server ──────────────────────────────────────────────────────────
_SERVER_PORT             = 8765
_SERVER_INACTIVITY_SECS  = 1800   # 30 minutes

# Sources that run on every scan by default.
# eBay is excluded (zero yield); use --source ebay to test it manually.
DEFAULT_SOURCES = [
    "cars", "cargurus", "edmunds", "carmax", "autotrader", "carvana", "visor"
]

# ── detection patterns ─────────────────────────────────────────────────────
_PATTERNS_511A = [re.compile(p, re.I) for p in [
    r"511\s*[-]?\s*A",
    r"moon\s*-?\s*roof",
    r"blue\s*-?\s*cruise",
    r"b\s*&\s*o\b",
    r"bang\s*(?:&|and)\s*olufsen",
    r"360.{0,8}camera",
    r"trailer\s+backup\s+assist",
    r"max\s+trailer\s+tow",
]]
# Strong text signals: objective battery/package facts that can confirm ER
# without a VIN.  Each of these has one meaning and cannot appear on an SR truck
# unless the listing is simply wrong (dealer error).
_ER_STRONG = [re.compile(p, re.I) for p in [
    r"131[\s-]?kwh?",                  # ER battery capacity (definitive; kW and kWh both appear)
    r"\ber\s+battery\b",              # explicit "ER battery" label
    r"\b511\s*[-]?\s*A\b",            # 511A equipment group (ER Lariat exclusive)
]]

# Hint signals: feature mentions that correlate with ER but are not proof.
# BlueCruise is a subscription add-on, moonroof is not battery-tied, and the
# 2.4 kW Pro Power Onboard ships on Standard Range.  Hints can nudge a listing
# to "likely ER" but never confirm alone.
_ER_HINT = [re.compile(p, re.I) for p in [
    r"extended[\s-]?range",           # too broad -- appears in trim comparisons
    r"bluecruise",                    # subscription ADAS, not battery-exclusive
    r"\bmoonroof\b|\bpanoramic\b",    # roof option, not battery-tied
    r"pro\s*power\s*onboard",         # 2.4 kW version ships on Standard Range
]]

_ER_NEGATIVE = [re.compile(p, re.I) for p in [
    # Explicit negatives always win over any positive signal.
    r"standard[\s-]?range",           # explicit SR label
    r"\bsr\s+battery\b",              # explicit SR battery label
    r"98[\s-]?kwh?",                   # SR battery capacity (definitive; kW and kWh both appear)
    r"\b510\s*[-]?\s*A\b",            # SR Lariat equipment package code
    # Removed: r"230[\s-]?(?:mi|mile)\s+epa" -- range figures are unreliable
]]
_AZURE_GRAY = re.compile(r"azure\s*gray", re.I)
_KNOWN_COLORS = [
    "Azure Gray", "Antimatter Blue", "Oxford White", "Carbonized Gray",
    "Atlas Blue", "Rapid Red", "Dark Matter Gray", "Agate Black",
    "Silver Metallic",
]

# ── status values ──────────────────────────────────────────────────────────
ACTIVE     = "active"
SOLD       = "sold"
PRICE_DROP = "price_drop"
_BAD_TITLES = {
    "www.cars.com", "cars.com",
    "just a moment...", "just a moment",
    "attention required!", "attention required",
    "your connection is not private",
    "connect to wi-fi",
    "access denied",
    "403 forbidden",
    "404 not found",
}

# Trim allow/reject constants -- belt-and-suspenders catch for Pro/XLT slipthrough.
# VDP and merge checks wrap the title in spaces before matching so word-boundary
# detection works even when the trim name falls at the very end of the string.
_ALLOWED_TRIMS  = ("lariat", "platinum")
_REJECTED_TRIMS = (" pro ", " pro\n", " xlt ", " xlt\n", "lightning pro", "lightning xlt")

# ── deal score ─────────────────────────────────────────────────────────────
_PRICE_MIN     = 25_000
_PRICE_MAX     = 90_000
_DEAL_BASELINE = 44_000   # fallback constant used when live sample is too small
_MILES_RATE    = 0.12     # $/mile value adjustment vs. 20k reference
# Minimum number of ER-confirmed comparable listings required to trust the
# live median baseline.  Below this, deal_score falls back to _DEAL_BASELINE
# and marks the score provisional.
_MARKET_BASELINE_MIN_SAMPLE = 8

# Permanently suppressed listings: junk data that can never be recovered.
# These are excluded from reports, VDP visits, and backfill queues across all runs.
# Add a listing's UUID here (from seen-listings.json) to permanently hide it.
_SUPPRESSED_UUIDS: frozenset = frozenset()

NEEDS_BACKFILL_FIELDS = ("location", "color")

# ── Playwright browser ─────────────────────────────────────────────────────
_PW_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/135.0.0.0 Safari/537.36"
)

_PW_CTX_HEADERS = {
    "Accept-Language":           "en-US,en;q=0.9",
    "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "DNT":                       "1",
    "Upgrade-Insecure-Requests": "1",
}
