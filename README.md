# RPA Scraper — IQVIA Automation Platform

Automates IQVIA report creation, downloads the resulting Excel exports, and post-processes them into clean CSV files ready for downstream analysis.

---

## How it works (overview)

1. Reads a product list (`data/iqvia/report_sources.tsv`)
2. Opens a Chromium browser and logs into the IQVIA portal
3. For every product row, builds a pivot report (C / O / M sheets) and exports it as Excel
4. Post-processes each Excel file — adds VTSTACK, RAW, and TABLE 1 sheets — and writes a combined CSV
5. A Streamlit dashboard lets you trigger and monitor runs without touching the command line

---

## Prerequisites

| Requirement | Minimum version |
|-------------|----------------|
| Python | 3.12 |
| pip | 24+ (bundled with Python 3.12) |
| Windows / macOS / Linux | any |
| IQVIA portal access | valid username + password + OTP |

---

## 1 — Clone the repo

```bash
git clone <repo-url>
cd rpa-scraper
```

---

## 2 — Create a virtual environment

```bash
python -m venv .venv
```

Activate it:

```bash
# Windows (PowerShell)
.venv\Scripts\Activate.ps1

# Windows (CMD)
.venv\Scripts\activate.bat

# macOS / Linux
source .venv/bin/activate
```

---

## 3 — Install Python dependencies

```bash
pip install -r requirements.txt
```

---

## 4 — Install Playwright browsers

Playwright needs to download Chromium once after install:

```bash
playwright install chromium
```

---

## 5 — Configure environment variables

Copy the example below, save it as `.env` in the project root, and fill in your credentials:

```dotenv
# IQVIA portal
APP_URL=https://hub.bi.iqvia.com/iam/
IQVIA_USER=your.email@company.com
IQVIA_PASS=YourPassword123
IQVIA_OTP=                        # optional: pre-set TOTP code (leave blank for manual entry)

# Optional: process only one product-list row (1-based). Leave blank to run all rows.
# Example: IQVIA_ROW=3  → only the 3rd product in report_sources.tsv / uploaded file
IQVIA_ROW=

# Data paths (optional — defaults shown)
IQVIA_DOWNLOAD_DIR=data/downloads/iqvia
DBF_INPUT_DIR=data/raw/DBF_FILES
PROCESSED_OUTPUT_DIR=data/processed
```

> **Note:** `.env` is listed in `.gitignore` and will never be committed.

### Run only one product by row number

Set `IQVIA_ROW` to the **1-based** row number in your product list (header row does not count):

```dotenv
IQVIA_ROW=5
```

That run downloads **only row 5**. Clear it or leave it blank to process every product again:

```dotenv
IQVIA_ROW=
```

---

## 6 — Prepare the product list

The bot reads a tab-separated file that tells it which products and markets to scrape.

**Default location:** `data/iqvia/report_sources.tsv`

| Column | Description |
|--------|-------------|
| `Data Source` | IQVIA data source name |
| `Cube No.` | Database/Catalog ID (e.g. `DDD_PK_M_MERCK_0123456789`) |
| `MARKET` | Market filter value |
| `PRODUCT` | Product name used for sheet/file naming |

Example:

```
Data Source	Cube No.	MARKET	PRODUCT
My Data Source	DDD_PK_M_MERCK_0123456789	PAKISTAN	ASPIRIN
My Data Source	DDD_PK_M_MERCK_0987654321	PAKISTAN	ROSUVASTATIN
```

You can also upload this file directly from the Streamlit UI (`.tsv`, `.csv`, or `.xlsx` all accepted — it gets converted to TSV automatically).

On first start, if `data/iqvia/report_sources.tsv` exists, it is **saved as the active list** at `data/iqvia/active_report_sources.tsv` and used for every run until you upload a new file.

---

## 7 — Run

On first start the project **creates all required folders automatically** — you do not need to create `data/`, `auth/`, or any subfolders by hand:

```
data/
├── downloads/iqvia/     ← IQVIA Excel/CSV exports
├── exports/
├── iqvia/               ← product list TSV (upload via UI or add manually)
├── logs/                ← per-job log files
├── processed/           ← DBF → CSV output
└── raw/DBF_FILES/       ← drop DBF files here for conversion
auth/                    ← saved browser login sessions
```

### Option A — Streamlit dashboard (recommended)

```bash
streamlit run ui/streamlit_app.py
```

Open `http://localhost:8501` in your browser. Use the **Download** page to start a run and monitor it live.

### Option B — Command line

Run the full scraper (all products in the list):

```bash
python scripts/run_scraper.py
```

Run a single product by name:

```bash
python scripts/run_single_product.py --product "ASPIRIN"
```

Post-process an already-downloaded Excel file (no browser):

```bash
python scripts/postprocess_report.py path/to/export.xlsx
```

Convert DBF files to CSV:

```bash
python apps/workers/conv_dbf_to_csv.py data/raw/DBF_FILES --recursive
```

---

## 8 — First run (login + OTP)

On the first run the browser will open in headed mode (visible window). Log in manually when prompted — the bot will save the session to `auth/iqvia_auth.json` so subsequent runs skip the login step.

If your OTP changes every run, leave `IQVIA_OTP` blank in `.env`; the bot will pause and wait for you to enter it in the terminal.

---

## 9 — Output files

| Path | Contents |
|------|----------|
| `data/downloads/iqvia/` | Raw Excel exports from IQVIA (`<PRODUCT>_<timestamp>.xlsx`) |
| `data/downloads/iqvia/` | Post-processed Excel with VTSTACK / RAW / TABLE 1 sheets |
| `data/downloads/iqvia/` | Combined CSV (all sheets, first column = sheet name) |
| `data/processed/<timestamp>/` | DBF → CSV conversion output |
| `data/logs/` | Per-job log files |
| `rpa_jobs.db` | SQLite job history database |

---

## 10 — Project structure

```
rpa-scraper/
├── apps/
│   ├── core/                   # Shared utilities: paths, job DB, logging
│   │   └── utils/              # Config reader, auth session manager
│   ├── engines/                # Orchestration: pipeline + job registry
│   ├── scrapers/
│   │   └── iqvia/              # IQVIA bot, page locators, post-processing
│   └── workers/                # Background job workers (download, convert)
├── ui/
│   ├── streamlit_app.py        # Dashboard home
│   └── pages/
│       ├── download.py         # Trigger IQVIA download
│       ├── convert.py          # Trigger DBF → CSV conversion
│       └── logs.py             # Job history and log viewer
├── scripts/                    # CLI entry points
├── data/                       # All runtime data (gitignored)
├── auth/                       # Saved browser sessions (gitignored)
├── .env                        # Credentials (gitignored — copy from section 5)
└── requirements.txt
```

---

## 11 — Common issues

| Problem | Fix |
|---------|-----|
| `playwright install` not run | Run `playwright install chromium` |
| `.env file not found` | Create `.env` in the project root (see section 5) |
| `No product list file found` | Add `data/iqvia/report_sources.tsv` or upload via the UI |
| Login fails silently | Check `IQVIA_USER` / `IQVIA_PASS` in `.env`; ensure the portal is reachable |
| Excel file too small error | The export timed out or was empty — retry or increase `DOWNLOAD_TIMEOUT_MS` in `iqvia_bot.py` |
| DBF import error | Run `pip install dbfread` |

---

## 12 — Running tests

```bash
pytest tests/
```
