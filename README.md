# Swiss First Names Analysis - Automated Report Generator

## Description

Downloads Swiss baby-name data (newborn rankings + total living population),
computes rankings and year-over-year changes, and generates ready-to-paste
article text in French, German and English, plus a handful of CSV/HTML
tables (top climbers/fallers, national & regional podiums, most common
names).

The pipeline is a single Python script: `00_data/babynames_script.py`.
The original R/Quarto version (`00_data/babynames_script.qmd`) is kept for
reference but is no longer the one to run.

## Data Sources

- **PX Web API**: Newborn names by region and year — stable endpoint, never needs updating.
- **Swiss Federal Statistical Office (BFS) DAM API**: Total living population by name —
  auto-discovered every run, no manual URLs. See "How the population data is fetched" below.

## Setup

Requires Python 3.8+ and two packages:

```bash
pip install -r 00_data/requirements.txt
```

That's it — no Jupyter, no R, no system libraries.

## Annual Configuration Required

**None**, in the common case — that's the point. Just run the script every year.

`DATA_YEAR` (the reference year for the whole analysis) is **not** based on
today's date — it's auto-detected as the most recent year present in the PX
Web newborn data, since BFS publishes final figures with a lag.

### How the population data is fetched

The "total living population by name" snapshot used to be a pair of manual
URLs that had to be hunted down on the BFS website every year (browser
inspector, Network tab, copy the asset link — see git history for the old
instructions). This is now automatic:

- BFS gives each dataset a stable **contentId** that never changes, while the
  **damId** (used in the actual download link) changes with every new annual
  edition. `GET /dam/assets/{contentId}:latest:fr` always resolves to
  whichever edition is currently newest — so "this year's" snapshot needs no
  configuration at all.
- There's no BFS endpoint to fetch *older* editions, so "previous year" works
  differently: after every successful run, the script saves what it just used
  as "current" into `00_data/bfs_population_state.json` (tracked in git).
  Next year, that becomes the "previous" snapshot automatically. It's a
  rolling one-year memory that requires the script to be run at least once a
  year to stay in sync.
- The very first time the script ever runs (before that state file exists),
  it falls back to `POPULATION_STATE_SEED` in the script — a one-time
  bootstrap value that's already set to a recent edition. You should never
  need to touch it.
- If the script wasn't run for more than a year, or BFS restructures its
  catalog, the script prints a clear warning instead of silently comparing
  mismatched years.

## Usage

```bash
cd 00_data
python babynames_script.py
```

Output is written to `00_data/output/<data_year>/`:

- `article_fr_YYYY.html`, `article_de_YYYY.html`, `article_en_YYYY.html` —
  the generated article text, ready to copy-paste into the CMS. Each includes
  section headings and, right after the climbers/fallers paragraphs, a
  compact HTML table of the top 5 gainers/losers per gender (10 names total)
  — more detail than the prose gives on its own.
- `report_YYYY.html` — a single page combining all three article texts with
  every table below, for a quick visual review before publishing. **Open this one first.**
- `climbers_YYYY.csv`, `fallers_YYYY.csv` — biggest rank gainers/losers, national ranking, 10 per gender.
- `rank_changes_full_YYYY.csv` — every name in the analysis pool (name, gender,
  rank_prev, rank_current, rank_change, counts, status), not just the top movers.
- `new_entries_YYYY.csv`, `exits_YYYY.csv` — names entering/leaving the national top 100.
- `rankings_national_YYYY.csv`, `rankings_regional_YYYY.csv` — top 3 podiums.
- `most_common_YYYY.csv`, `biggest_population_increases_YYYY.csv`, `biggest_population_decreases_YYYY.csv` — total living population analysis.

Raw downloads are cached in `00_data/input/raw/` (git-ignored). Set
`FORCE_REDOWNLOAD = False` at the top of the script to reuse them between runs
while iterating locally.

## Script Structure

- **Config**: paths, thresholds, DAM API content ids.
- **Import**: PX Web (newborn rankings) + BFS DAM population snapshots (auto-discovered), with encoding and year-mismatch sanity checks.
- **Rankings**: national/regional top 3, tie-free custom rank.
- **Rank changes**: climbers, fallers, new entries, exits (year-over-year).
- **Population analysis**: most common names overall, biggest population swings.
- **Name length**: average first-name length for the reference year's births (two methodologies - see below).
- **Text generation**: FR/DE/EN article text, with embedded top-movers tables.
- **Export**: HTML articles, combined HTML report, CSV tables.

## Average name length: two different numbers, both correct

The article quotes two figures, because "average name length" is ambiguous:

- **Weighted by births** (primary figure): the length a randomly picked
  newborn actually has. Short, popular names like Emma/Noah pull this down.
- **Unweighted**: the simple mean across the roster of *distinct* names given
  that year — Aliénor and Emma each count once, regardless of how many babies
  got that name.

These can differ by half a character or more (e.g. 5.16 vs. 5.59 in 2024).
The original R script's code comment claimed to compute the unweighted
version but the code actually computed the weighted one — if last year's
published figure looks different from this year's, that's why.

## Notes on the R → Python conversion

A few things from the original R/Quarto script were deliberately dropped or fixed:

- Removed the interactive Plotly heatmaps and the "years as #1 leader" /
  name-diversity analyses — none of them fed into the final article text or
  the climbers/fallers tables, so they were dead weight.
- Fixed a real bug: the R script hardcoded "Maria"/"Daniel" to compute the
  previous-year count of the most common names. If a different name ever
  becomes the most common, that logic would silently report the wrong
  number. The Python version always looks up the actual most common name's
  own previous-year count.
- `DATA_YEAR` is now detected from the data itself instead of `today - 1`,
  since BFS may not have published the most recent year yet when the script runs.
- Dropped the clipboard-copy helper (unreliable across OSes) — the HTML
  report is easier to copy from directly.
- Replaced the four manually-updated population URLs with automatic
  discovery via the BFS DAM API (see "How the population data is fetched" above) —
  this was the last remaining annual manual step, and it's now gone.
- The name-length figure silently changed methodology between the R comment
  and the R code (see "Average name length" above) — the Python version
  reports both explicitly instead of picking one silently.

## Troubleshooting

- A PX Web download failure usually means BFS changed that endpoint — the
  error message names the offending URL.
- A population download/resolution failure means BFS restructured their DAM
  catalog (rare) — check the content ids in `POPULATION_CONTENT_IDS` still
  resolve by visiting `https://dam-api.bfs.admin.ch/hub/api/dam/assets/<content_id>:latest:fr` in a browser.
- A "previous edition is stale" warning means `bfs_population_state.json` is
  more than a year old (the script wasn't run last year) — either accept the
  wider comparison window, or delete the file to fall back to the seed value.
- Check the "encoding sanity check" output after import for names with
  unexpected characters — BFS serves the PX Web export as Windows-1252,
  which can't represent every diacritic (e.g. Czech `š`/`č`); this is a
  source-data limitation, not a script bug.
