# Swiss First Names Analysis - Automated Report Generator

## Description

Downloads Swiss baby-name data (newborn rankings + total living population),
computes rankings and year-over-year changes, and generates ready-to-paste
article text in French and German, plus a handful of CSV/HTML tables (top
climbers/fallers, national & regional podiums, most common names).

The pipeline is a single Python script: `00_data/babynames_script.py`.
The original R/Quarto version (`00_data/babynames_script.qmd`) is kept for
reference but is no longer the one to run.

**The text is the deliverable; the tables are backup.** The target CMS takes
plain text, not HTML, so the generated article text has no HTML tags at all -
first names are marked with `**bold**` (Markdown-style) instead, which gets
converted to real `<strong>` tags in the HTML output so that copying from a
browser (or from a Claude chat response with the same text pasted in) carries
bold through the rich-text clipboard into the CMS. See "Rich-text copy-paste"
below.

## Data Sources

- **stats.swiss SDMX API** (newborn names by canton and year): queried
  directly via the standard SDMX REST data endpoint (SDMX-JSON format).
  This replaced the old PX Web tables in 2026 - see "The 2026 migration to
  stats.swiss" below.
- **Swiss Federal Statistical Office (BFS) DAM API** (total living population
  by name): auto-discovered every run, no manual URLs. See "How the
  population data is fetched" below.

## Setup

Requires Python 3.8+ and two packages:

```bash
pip install -r 00_data/requirements.txt
```

That's it — no Jupyter, no R, no system libraries.

## Annual Configuration Required

**None**, in the common case — that's the point. Just run the script every year.

`DATA_YEAR` (the reference year for the whole analysis) is **not** based on
today's date — it's auto-detected as the most recent year present in the
newborn-name data, since BFS publishes final figures with a lag.

### The 2026 migration to stats.swiss

BFS retired the old PX Web tables in 2026 in favor of the new SDMX-based
"Swiss stats explorer" (`stats.swiss`). `import_px_data()` now queries the
standard SDMX REST data endpoint directly:
`https://disseminate.stats.swiss/rest/data/CH1.BEVNAT,<dataflow>,1.0.0/..A.COUNT`
(one GET per gender, `..A.COUNT` = wildcard GEO, wildcard NAME, annual
frequency, counts only - "Rang" isn't requested since the script always
recomputes its own tie-free rank). Response format is SDMX-JSON, UTF-8
natively, so accented names (e.g. "Eliška") are not at risk of the
single-byte-charset mangling the old CSV export had.

One structural change to be aware of: the new dataflow's `GEO` dimension only
carries Switzerland-total (code `8100`) and the 26 cantons - the
pre-aggregated linguistic-region codes still listed in the codelist (German-
/French-/Italian-/Romansh-speaking Switzerland) carry **no actual data**
(confirmed: querying them 404s). So `sdmx_json_to_long_df()` rebuilds
"Suisse alémanique/romande/italienne/romanche" itself, by summing cantons via
`CANTON_TO_LINGUISTIC_REGION`. Bilingual/trilingual cantons already arrive as
separate per-language GEO codes (e.g. Bern-German `21` vs Bern-French `22`,
Graubünden `181`/`183`/`184` for German/Italian/Romansh), so no canton is
ever double-counted. This also means a 4th region, Romansh-speaking
Graubünden ("Suisse romanche"), is now tracked - it appears in the regional
top-3 table but is small enough that it's deliberately not mentioned in the
FR/DE article prose, which still only discusses the three historical regions.

The prénom↔code mapping (`CL_TOP_MALE/FEMALE_FIRSTNAMES`) is a codelist
versioned by year (e.g. `1.2025.0`) - the script never hardcodes a code, it
always reads the label embedded in that run's own SDMX-JSON response.

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

- `article_fr_YYYY.html`, `article_de_YYYY.html` — one page per language,
  split into sections: a heading, then a highlighted text block (the actual
  copy, first names in bold, no other markup), then that section's tables.
  Open in a browser and copy each text block directly into the CMS.
- `report_YYYY.html` — both languages one after another on a single page,
  plus a few full reference tables at the end, for a quick overview before
  publishing. **Open this one first.**
- `data_quality_log_YYYY.txt` — encoding check, PX-vs-population freshness
  check, and a manual-review reminder (see "Data quality checks" below).
  Also printed to the console on every run.
- `climbers_YYYY.csv`, `fallers_YYYY.csv` — biggest rank gainers/losers
  within the top `CLIMBER_RANK_CEILING` (200), national ranking, 10 per gender.
- `rank_changes_full_YYYY.csv` — every name in the analysis pool (name,
  gender, rank_prev, rank_current, rank_change, counts, status), not just the
  top movers or the top-200 pool.
- `evolution_male_YYYY.csv`, `evolution_female_YYYY.csv` — the 10-year rank
  history behind the evolution table, one column per year.
- `new_entries_YYYY.csv`, `exits_YYYY.csv` — names entering/leaving the national top 100.
- `rankings_national_YYYY.csv`, `rankings_regional_YYYY.csv` — top 3 podiums.
- `most_common_YYYY.csv`, `biggest_population_increases_YYYY.csv`, `biggest_population_decreases_YYYY.csv` — total living population analysis.

Raw downloads are cached in `00_data/input/raw/` (git-ignored). Set
`FORCE_REDOWNLOAD = False` at the top of the script to reuse them between runs
while iterating locally.

## Rich-text copy-paste

The CMS accepts pasted rich text (bold preserved) but not raw HTML tags typed
as text. So the article HTML never shows a literal `<strong>` - the text
blocks embed real, unescaped `<strong>` elements (see `markdown_bold_to_html()`),
and selecting + copying that block from a rendered browser page carries the
bold through the browser's rich-text (`text/html`) clipboard representation.

The same `**name**` markers are what a Claude chat response uses to render
bold directly in the conversation - so pasting the article text into a chat
message and asking for it back, or having Claude generate it directly in a
reply, works the same way: copying the rendered chat bubble carries bold into
the CMS too, without needing to open a file first.

## Script Structure

- **Config**: paths, thresholds, stats.swiss SDMX dataflow IDs, canton→region
  mapping, DAM API content ids.
- **Import**: stats.swiss (newborn rankings, via SDMX REST/SDMX-JSON) + BFS DAM
  population snapshots (auto-discovered).
- **Data quality checks**: see below — run once, after both datasets are loaded.
- **Rankings**: national/regional top 3, tie-free custom rank, 10-year evolution of today's top 10.
- **Rank changes**: climbers, fallers, new entries, exits (year-over-year).
- **Population analysis**: most common names overall, biggest population swings.
- **Name length**: average first-name length for the reference year's births.
- **Text generation**: FR/DE article sections (heading + plain-text-with-bold
  block + tables), including the "years as #1" leader-continuity narrative.
- **Export**: HTML article pages, combined HTML report, CSV tables, data quality log.

## Data quality checks

Every run performs checks and writes them to `data_quality_log_YYYY.txt`
(and prints them to the console):

1. **Encoding.** Flags names containing characters that indicate a likely
   decoding problem — the Unicode replacement character, a stray diacritic
   with no base letter (e.g. a leftover `Eli¨ka` if the data source ever
   regresses), or digits. Deliberately does **not** flag apostrophes, hyphens
   or spaces, since those are legitimate in real Swiss-registered names
   (`N'Guessan`, `Jean-Pierre`). This should normally come back clean since
   the stats.swiss SDMX-JSON data is UTF-8 natively (see "The 2026 migration
   to stats.swiss" above) - if it isn't, that's worth investigating, not ignoring.
2. **Newborn rankings vs. population census freshness.** The newborn-name
   rankings and the population census are two independent BFS datasets. This
   check confirms the population census actually has rows for
   `yearofbirth == DATA_YEAR` (the year the newborn rankings say is the
   latest) — if not, the census hasn't caught up yet, and any figure derived
   from it (most common name, name length) is flagged as unreliable rather
   than silently used.
3. **Manual review reminder.** Two paragraphs in `build_article_sections()`
   are static text, never re-verified against data: the name-diversity/
   individualisation trend (references 1980, but the data only goes back to
   2000) and the "as shown by our maps" sentence (the script doesn't generate
   maps). The log just reprints a reminder to skim them each year.

## Climbers/fallers: a rank ceiling, not a minimum count

`CLIMBER_RANK_CEILING = 200` restricts "biggest movers" to names ranked
within the national top 200 in *both* years. This was deliberately chosen
over filtering by a minimum birth count: within the top 200, counts are
already comfortably high (rank 200 ≈ 36 births), so tiny-sample noise (a name
going from 1 to 3 births swinging wildly in rank) isn't a risk, and the
resulting climbers/fallers are still described with modest wording ("de
belles progressions" / "legten spürbar zu") rather than superlatives, since
within a 200-name pool they aren't necessarily the single most extreme swing
in the full ~2600-name dataset - just a notable one within a meaningful pool.
`rank_changes_full_YYYY.csv` has no such ceiling, if a wider view is ever wanted.

## Average name length

The article reports one figure: the **unweighted** mean length across the
roster of distinct names given that year (each name counts once, regardless
of how many babies got it) - this is what the real published reference
articles use. `name_length_summary()` also computes a **weighted** version
(length as experienced by a randomly picked newborn, pulled down by short
popular names like Emma/Noah) for reference, but it's not currently used in
the generated text. The two can differ by half a character or more.

## Troubleshooting

- A stats.swiss SDMX query failure usually means BFS changed the dataflow ID
  or dimension codes — the error message names the dataflow. Check
  `https://disseminate.stats.swiss/rest/dataflow/CH1.BEVNAT/<dataflow_id>/1.0.0?references=all`
  (GET, `Accept: application/vnd.sdmx.structure+json;charset=utf-8;version=1.0`)
  in a browser/curl to see the current dimensions and codelists.
- If a canton code stops appearing in the data (script silently drops it via
  the `else: continue` in `sdmx_json_to_long_df()`), a linguistic region's
  total would quietly shrink — check `CANTON_TO_LINGUISTIC_REGION` against
  the current `CL_KT_LING_DIFF` codelist if regional totals look off.
- A population download/resolution failure means BFS restructured their DAM
  catalog (rare) — check the content ids in `POPULATION_CONTENT_IDS` still
  resolve by visiting `https://dam-api.bfs.admin.ch/hub/api/dam/assets/<content_id>:latest:fr` in a browser.
- A "previous edition is stale" warning means `bfs_population_state.json` is
  more than a year old (the script wasn't run last year) — either accept the
  wider comparison window, or delete the file to fall back to the seed value.
- Check `data_quality_log_YYYY.txt` for names with unexpected characters —
  should be empty now (see "The 2026 migration to stats.swiss"); if
  something shows up, it's worth a look rather than assuming it's expected.

## Notes on the R → Python conversion

A few things from the original R/Quarto script were deliberately dropped or fixed:

- Removed the interactive Plotly heatmaps - replaced by a plain-text-friendly
  10-year rank evolution table, which the R version never actually used in
  its article text.
- Fixed a real bug: the R script hardcoded "Maria"/"Daniel" to compute the
  previous-year count of the most common names. If a different name ever
  becomes the most common, that logic would silently report the wrong
  number. The Python version always looks up the actual most common name's
  own previous-year count.
- `DATA_YEAR` is now detected from the data itself instead of `today - 1`,
  since BFS may not have published the most recent year yet when the script runs.
- Replaced the four manually-updated population URLs with automatic
  discovery via the BFS DAM API — the last remaining annual manual step, now gone.
- Replaced the CSV "saved query" PX Web download (lossy for some accented
  names) with the PXWebAPI directly. (PX Web itself was later retired by BFS
  in 2026 - see "The 2026 migration to stats.swiss" above.)
