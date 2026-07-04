# F-150 Lightning Scanner

A daily scanner that watches Cars.com, CarGurus, AutoTrader, CarMax, Edmunds, and Carvana for used **2023-2024 Ford F-150 Lightning Lariat** listings, filters out Standard Range and non-Lariat trims, tracks price drops, and generates a clean HTML report each run.

Built for people who are actively searching and don't want to manually refresh five sites every day.

---

## What it does

- Searches 6+ sources simultaneously for used 2023-2024 Lightning Lariats nationwide
- Detects Extended Range vs Standard Range using the VIN (position 4: `6` = ER, `V` = SR) with text-based fallback signals (131 kWh, 511A package, 98 kWh negatives)
- Filters out Pro, XLT, and Flash trims
- Deduplicates across sources by VIN so the same truck doesn't appear twice
- Tracks every price drop with history
- Scores each listing 1-5 stars against the live market median
- Generates a dated HTML report you open in any browser

---

## Requirements

- **Mac** (tested on macOS Ventura and later; should work on Monterey)
- **Python 3.11 or later**
- A working internet connection
- About 10-15 minutes for first-time setup

To check your Python version, open Terminal and run:
```bash
python3 --version
```
If you see `Python 3.11.x` or higher, you're set. If not, download Python from [python.org](https://www.python.org/downloads/).

---

## Setup

### 1. Download the scanner

On the GitHub page, click the green **Code** button, then **Download ZIP**. Unzip it somewhere permanent — your Documents folder works well:

```
~/Documents/lightning-scan/
```

### 2. Open Terminal and navigate to the folder

```bash
cd ~/Documents/lightning-scan
```

### 3. Install dependencies

```bash
pip3 install -r requirements.txt
playwright install chromium
```

This installs the Python libraries and downloads a headless browser (Chromium) that the scanner uses to load JavaScript-heavy pages. The Chromium download is about 150 MB and only happens once.

If `pip3` isn't found, try `pip` instead.

### 4. Configure your search

Open `scanner/config.py` in any text editor (TextEdit works; so does VS Code if you have it).

The main things you might want to adjust are near the top of the file:

```python
# ZIP code used as the geographic center for result ordering.
# The search is always nationwide regardless of what you put here.
SEARCH_ZIP = "10001"
```

Change `"10001"` to your zip code if you want results ordered closer to you.

```python
# Target color (used to flag matching listings in the report)
_AZURE_GRAY = re.compile(r"azure\s*gray", re.I)
```

Change `azure\s*gray` to whatever color you're targeting, or leave it as-is.

```python
_DEAL_BASELINE = 44_000   # fallback price baseline when live sample is too small
```

Adjust this to match what you think fair market value is. The scanner will compute a live baseline from active listings automatically, but this is the fallback.

Save the file when done.

---

## Running the scanner

Each time you want a fresh scan:

```bash
cd ~/Documents/lightning-scan
python3 scanner.py
```

The first run takes 3-5 minutes. It's visiting multiple sources and enriching listings with history and VIN data. Subsequent runs are similar.

When it finishes, the terminal will show a summary like:

```
══════════════════════════════════════════════
  ⚡ Lightning Scan Complete
══════════════════════════════════════════════
  Active listings   : 183
  New this run      :  22
  Price drops       :  14
  Azure Gray        :   3
  SR excluded       : 144
  ER unconfirmed    :  35
  Report            : reports/2026-06-19.html
══════════════════════════════════════════════
```

A browser window should open automatically with the report. If it doesn't, open the `reports/` folder and double-click the dated HTML file.

---

## Reading the report

The report has three sections:

**Confirmed ER** — Listings where the VIN or strong listing text confirms Extended Range battery. These are trustworthy. Sorted by deal score (5 stars = best deal relative to live market median).

**Unconfirmed** — Listings where battery type couldn't be confirmed from available data. Usually AutoTrader listings without a VIN. Treat these with skepticism until you can verify directly.

**Azure Gray** (or your target color) — A separate callout of color-matched listings pulled from both sections above.

### Column guide

| Column | Meaning |
|--------|---------|
| Stars | Deal score 1-5 vs live market median (mileage-adjusted) |
| Price | Listed price |
| Delta | How far above or below market median (positive = under market) |
| Miles | Odometer |
| DOM | Days on market († = true listing age from dealer data; otherwise estimated from first seen) |
| Drops | Price drop history (e.g., `3x -$500 last`) |
| History | Title history: clean, accident, salvage, buyback |
| ER Source | How ER was confirmed: VIN, 511A text, 131 kWh text, etc. |

### Things to know

- **CarGurus sometimes shows wrong specs.** If a confirmed-ER listing shows "98 kW" on the CarGurus page, trust the VIN over the CG spec display. Verify directly with the dealer or pull the window sticker at [motortrend.com/windowsticker](https://www.motortrend.com/windowsticker/).
- **AutoTrader goes down occasionally.** When it does, the scanner skips sold-marking for AutoTrader listings to protect your data. Those listings will be refreshed on the next run.
- **"Unconfirmed" shrinks over time.** AutoTrader listings without VINs get aged out of the unconfirmed section after 2 days if they can't be resolved.
- **Never delete `seen-listings.json`.** This is the scanner's memory. Deleting it resets all history, price drops, and sold tracking.

---

## Suppressing a listing

If a specific listing is junk and you want to hide it permanently, find its UUID in `seen-listings.json` (search for the dealer name) and add it to `_SUPPRESSED_UUIDS` in `scanner/config.py`:

```python
_SUPPRESSED_UUIDS: frozenset = frozenset({
    "cargurus-123456789",   # Corwin Ford -- not a real listing
})
```

---

## Sources

| Source | Notes |
|--------|-------|
| Cars.com | Three keyword sweeps; visits individual VDPs for trim and history data |
| CarGurus | Full nationwide inventory; 10-12 pages per run |
| AutoTrader | Nationwide; intermittently blocks scrapers |
| CarMax | Small inventory but clean data |
| Edmunds | Intermittent; low yield |
| Carvana | Small inventory; occasional junk listings |
| Visor | Dealer inventory feed; high-confidence VIN data |

eBay Motors is supported (`--source ebay`) but excluded from default runs due to low yield.

---

## Troubleshooting

**`ModuleNotFoundError`** — Run `pip3 install -r requirements.txt` again.

**`playwright._impl._errors.Error: Executable doesn't exist`** — Run `playwright install chromium`.

**AutoTrader returns 0 results** — AutoTrader blocks scrapers intermittently. The scanner detects this and skips sold-marking to protect your data. Try again in a few hours.

**Report not opening automatically** — Open the `reports/` folder in Finder and double-click the most recent `.html` file.

**Lots of "Unconfirmed" listings** — Normal after a big influx of AutoTrader listings. They'll age out of the unconfirmed section after 2 days if the VIN can't be resolved.

---

## Privacy note

`seen-listings.json` and the `reports/` folder are excluded from version control via `.gitignore`. They contain your personal search history and should not be committed or shared.
