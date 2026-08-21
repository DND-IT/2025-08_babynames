"""
Swiss First Names Analysis - Automated Annual Report
Author: Olaf Koenig

Downloads newborn-name rankings (stats.swiss SDMX API, automatic) and
total-population name counts (BFS DAM API, auto-discovered) for Switzerland,
computes rankings, year-over-year changes and a few key stats, then generates
ready-to-paste article text in FR/DE/EN plus a handful of CSV/HTML tables.

Requirements: Python 3.8+, pandas, requests (see requirements.txt).
Run with:     python babynames_script.py
"""

import io
import json
import re
import unicodedata
from html import escape
from pathlib import Path

import pandas as pd
import requests

# =============================================================================
# CONFIGURATION - UPDATE ANNUALLY
# =============================================================================

# DATA_YEAR / COMPARISON_YEAR are NOT derived from today's date: BFS publishes
# final birth-name statistics with a lag (e.g. in mid-2026, only 2024 data may
# be available yet, not 2025). Instead they are detected from the newest year
# actually present in the downloaded PX Web data - see detect_years() below.
DATA_YEAR = None
COMPARISON_YEAR = None

# How many ranks to look at when computing new entries/exits. Climbers/fallers
# have no such cap (see build_rank_changes) - a name can validly jump from
# e.g. rank 583 to rank 167.
TOP_N_DISPLAY = 100    # threshold used for "new entry" / "exit" from the top
TOP_N_MOVERS_IN_ARTICLE = 5     # climbers/fallers shown per gender in the article's own tables
TOP_N_NEW_ENTRIES_IN_ARTICLE = 5  # new top-100 entries shown per gender in the article

# A name must be ranked within this pool in BOTH years to be eligible as a
# climber/faller - keeps tiny-sample noise (e.g. 1 birth -> 3 births, deep in
# the tail) from dominating "biggest movers", see climbers_and_fallers().
CLIMBER_RANK_CEILING = 200

# National top-N-names-over-N-years evolution table (first article section).
EVOLUTION_N_NAMES = 10
EVOLUTION_N_YEARS = 10

# Re-download files even if they already exist locally for this run.
# Set to False while iterating locally to avoid hammering the BFS servers.
FORCE_REDOWNLOAD = True

# Linguistic regions as labelled by the BFS data (kept in French - it's the
# raw data value, not display text). "romanche" is new: the stats.swiss
# dataflow no longer publishes pre-aggregated linguistic-region totals (see
# CANTON_TO_LINGUISTIC_REGION below), so regions are rebuilt from cantons and
# Romansh-speaking Graubünden can be kept as its own (tiny) region instead of
# being folded into another one.
NATIONAL_REGION = "Suisse"
LINGUISTIC_REGIONS = {
    "alemanique": "Suisse alémanique",
    "romande": "Suisse romande",
    "italienne": "Suisse italienne",
    "romanche": "Suisse romanche",
}

# Paths (relative to this script, so it runs the same regardless of the
# working directory it's launched from). OUTPUT_DIR is finalised once
# DATA_YEAR is known - see detect_years().
BASE_DIR = Path(__file__).resolve().parent
INPUT_RAW_DIR = BASE_DIR / "input" / "raw"
INPUT_PROCESSED_DIR = BASE_DIR / "input" / "processed"
OUTPUT_DIR = None

for directory in (INPUT_RAW_DIR, INPUT_PROCESSED_DIR):
    directory.mkdir(parents=True, exist_ok=True)

# stats.swiss SDMX REST API (automatic - full historical newborn-name series,
# stable dataflow IDs, never need updating). BFS migrated the old PX Web
# tables here in 2026; this is the standard SDMX-JSON data format (native
# Unicode, so no encoding issues like the PX Web CSV export used to have).
SDMX_API_BASE = "https://disseminate.stats.swiss/rest"
SDMX_AGENCY = "CH1.BEVNAT"
SDMX_DATAFLOW_IDS = {"female": "DF_BEVNAT_PRENOMS_2", "male": "DF_BEVNAT_PRENOMS_1"}
SDMX_DATAFLOW_VERSION = "1.0.0"
SDMX_DATA_HEADERS = {"Accept": "application/vnd.sdmx.data+json;charset=utf-8;version=1.0"}
# Query key is GEO.NAME.FREQ.UNIT; empty segments mean "all values for this
# dimension" (SDMX REST wildcard). We ask for annual frequency and raw counts
# only - "Rang" isn't requested since custom_rank is computed ourselves.
SDMX_DATA_KEY = "..A.COUNT"

# Unlike the old PX Web table, the GEO dimension here only carries
# Switzerland-total (code 8100) and the 26 cantons - the codelist still lists
# pre-aggregated linguistic-region codes (1/2/3/4), but they carry no actual
# data (confirmed: querying them 404s). So the 3(+1) linguistic regions used
# throughout this script are rebuilt here by summing cantons into their
# language group. Bilingual/trilingual cantons already arrive as separate
# per-language GEO codes (e.g. Bern-German 21 vs Bern-French 22, Fribourg 101
# vs 102, Valais 231 vs 232, Jura 261 vs 262, Graubünden 181/183/184 for
# German/Italian/Romansh), so no canton is ever double-counted across regions.
CANTON_TO_LINGUISTIC_REGION = {
    "11": "alemanique", "21": "alemanique", "31": "alemanique", "41": "alemanique",
    "51": "alemanique", "61": "alemanique", "71": "alemanique", "81": "alemanique",
    "91": "alemanique", "101": "alemanique", "111": "alemanique", "121": "alemanique",
    "131": "alemanique", "141": "alemanique", "151": "alemanique", "161": "alemanique",
    "171": "alemanique", "181": "alemanique", "191": "alemanique", "201": "alemanique",
    "231": "alemanique", "261": "alemanique",
    "22": "romande", "102": "romande", "222": "romande", "232": "romande",
    "242": "romande", "252": "romande", "262": "romande",
    "213": "italienne", "183": "italienne",
    "184": "romanche",
}
SDMX_NATIONAL_GEO_CODE = "8100"

# Total-population-by-name snapshots (used for "most common name overall").
# No manual URL to maintain: BFS assigns each dataset a stable contentId that
# never changes, while the damId (used in download links) changes with every
# new annual edition. GET /dam/assets/{contentId}:latest:{lang} always
# resolves to this year's edition automatically - see resolve_latest_population_asset().
DAM_API = "https://dam-api.bfs.admin.ch/hub/api"
POPULATION_CONTENT_IDS = {"female": 26925125, "male": 26925124}

# There is no BFS endpoint to fetch *previous* editions, so "current" is
# saved to POPULATION_STATE_FILE (tracked in git) after every run and reused
# as next year's "previous" - fully automatic from the second run onward.
# POPULATION_STATE_SEED only bootstraps the very first run, before that file exists.
POPULATION_STATE_FILE = BASE_DIR / "bfs_population_state.json"
POPULATION_STATE_SEED = {
    "female": {"dam_id": 32208758, "period": "2023"},
    "male": {"dam_id": 32208755, "period": "2023"},
}


# =============================================================================
# IMPORT HELPERS
# =============================================================================

def _strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def clean_names(df: pd.DataFrame) -> pd.DataFrame:
    """Snake-case, accent-free column names (mirrors R's janitor::clean_names)."""
    def clean(col):
        col = _strip_accents(str(col)).strip()
        col = re.sub(r"[^0-9a-zA-Z]+", "_", col)
        col = re.sub(r"_+", "_", col).strip("_")
        return col.lower()

    df = df.copy()
    df.columns = [clean(c) for c in df.columns]
    return df


def download(url: str, dest_path: Path) -> None:
    if dest_path.exists() and not FORCE_REDOWNLOAD:
        print(f"  (skip download, already have {dest_path.name})")
        return
    try:
        response = requests.get(url, timeout=60)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(f"Failed to download {url}\nOriginal error: {exc}") from exc
    dest_path.write_bytes(response.content)


def read_csv_robust_encoding(path: Path) -> tuple:
    """Used for the population snapshots (DAM API), which are UTF-8. Tries
    UTF-8 first and only falls back to Windows-1252 if that fails outright -
    self-verifying, since real Windows-1252 bytes containing accented
    characters almost never happen to also form valid UTF-8. (PX Web data no
    longer goes through this path - see import_px_data(), which uses the
    PXWebAPI JSON-stat2 format instead of the CSV export precisely because
    that CSV export is forced to a lossy single-byte charset server-side.)"""
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
        encoding_used = "UTF-8"
    except UnicodeDecodeError:
        text = raw.decode("cp1252")
        encoding_used = "Windows-1252 (UTF-8 decoding failed)"
    return pd.read_csv(io.StringIO(text)), encoding_used


def resolve_latest_population_asset(content_id: int, lang: str = "fr") -> dict:
    """Auto-discovers this year's population-by-name snapshot via the BFS DAM
    API. contentId identifies the dataset lineage and never changes; damId
    identifies one specific annual edition and changes every year. Asking for
    "{content_id}:latest:{lang}" always resolves to whatever is currently the
    newest edition - no manual URL-hunting required."""
    url = f"{DAM_API}/dam/assets/{content_id}:latest:{lang}"
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        payload = response.json()
        dam_id = payload["ids"]["damId"]
        period = str(payload["description"]["bibliography"]["period"])
        master_url = next(link["href"] for link in payload["links"] if link["rel"] == "master")
    except (requests.RequestException, KeyError, StopIteration) as exc:
        raise RuntimeError(
            f"Could not auto-resolve the latest BFS population dataset (content id {content_id}) "
            f"via {url}. BFS may have restructured their catalog. Original error: {exc}"
        ) from exc
    return {"dam_id": dam_id, "period": period, "url": master_url}


def load_population_state() -> dict:
    """{gender: {"current": {...} | None, "previous": {...}}}. "previous" is
    what gets used as this run's previous-year population snapshot. "current"
    is only there so a same-year re-run can tell "BFS published a new edition"
    apart from "I'm just running the script again" - see load_population_data()."""
    if POPULATION_STATE_FILE.exists():
        return json.loads(POPULATION_STATE_FILE.read_text(encoding="utf-8"))
    return {
        gender: {
            "current": None,
            "previous": {**info, "url": f"{DAM_API}/dam/assets/{info['dam_id']}/master"},
        }
        for gender, info in POPULATION_STATE_SEED.items()
    }


def save_population_state(state: dict) -> None:
    POPULATION_STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def fetch_sdmx_data(gender: str) -> dict:
    """One request per gender - wildcard query for all GEO codes (Switzerland
    + 26 cantons) and all NAME codes, annual counts only (see SDMX_DATA_KEY)."""
    dataflow_id = SDMX_DATAFLOW_IDS[gender]
    url = f"{SDMX_API_BASE}/data/{SDMX_AGENCY},{dataflow_id},{SDMX_DATAFLOW_VERSION}/{SDMX_DATA_KEY}"
    try:
        response = requests.get(url, headers=SDMX_DATA_HEADERS, timeout=120)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise RuntimeError(
            f"stats.swiss SDMX API query failed for dataflow {dataflow_id}.\n"
            f"Original error: {exc}"
        ) from exc
    return response.json()


def sdmx_json_to_long_df(payload: dict, gender: str) -> pd.DataFrame:
    """Flattens one SDMX-JSON data message (dims: GEO x NAME x FREQ x UNIT,
    FREQ/UNIT fixed to a single value by the query) into a long dataframe:
    prenom, region_linguistique_canton, year, value, gender. Cantons are
    summed into their linguistic region here (see CANTON_TO_LINGUISTIC_REGION
    above); Switzerland-total (8100) is used as-is. Written generically
    against the dimensions' declared order/ids rather than assuming fixed
    positions, so a harmless BFS reordering wouldn't break it."""
    dims = payload["data"]["structure"]["dimensions"]
    series_dims = dims["series"]  # order matches series-key segment order (SDMX-JSON spec)
    geo_pos = next(i for i, d in enumerate(series_dims) if d["id"] == "GEO")
    name_pos = next(i for i, d in enumerate(series_dims) if d["id"] == "NAME")
    geo_values = series_dims[geo_pos]["values"]
    name_values = series_dims[name_pos]["values"]

    obs_dim = next(d for d in dims["observation"] if d["id"] == "TIME_PERIOD")
    obs_years = [int(v["id"]) for v in obs_dim["values"]]

    rows = []
    for series_key, series in payload["data"]["dataSets"][0]["series"].items():
        indices = [int(x) for x in series_key.split(":")]
        geo_code = geo_values[indices[geo_pos]]["id"]
        if geo_code == SDMX_NATIONAL_GEO_CODE:
            region_label = NATIONAL_REGION
        elif geo_code in CANTON_TO_LINGUISTIC_REGION:
            region_label = LINGUISTIC_REGIONS[CANTON_TO_LINGUISTIC_REGION[geo_code]]
        else:
            continue  # unmapped GEO code - shouldn't happen, see CANTON_TO_LINGUISTIC_REGION comment
        prenom = name_values[indices[name_pos]]["name"]
        for obs_i, obs in series["observations"].items():
            rows.append((prenom, region_label, obs_years[int(obs_i)], obs[0]))

    df = pd.DataFrame(rows, columns=["prenom", "region_linguistique_canton", "year", "value"])
    df = df.groupby(["prenom", "region_linguistique_canton", "year"], as_index=False)["value"].sum()
    df["gender"] = gender
    return df


def import_px_data(gender: str) -> pd.DataFrame:
    """One gender via the stats.swiss SDMX API (see SDMX_DATAFLOW_IDS above)."""
    cache_path = INPUT_RAW_DIR / f"sdmx_{gender}.json"
    if cache_path.exists() and not FORCE_REDOWNLOAD:
        print(f"  (skip download, already have {cache_path.name})")
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
    else:
        print(f"Querying stats.swiss SDMX API: {gender} ...")
        payload = fetch_sdmx_data(gender)
        cache_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    df = sdmx_json_to_long_df(payload, gender)
    print(f"  imported {len(df):,} rows for {gender} (native UTF-8, no encoding fallback needed)")
    return df


def import_population_data(url: str, filename_base: str, year, gender: str) -> pd.DataFrame:
    dest_path = INPUT_RAW_DIR / f"{filename_base}_{year}.csv"
    print(f"Downloading {dest_path.name} ...")
    download(url, dest_path)
    df, encoding_used = read_csv_robust_encoding(dest_path)
    df = clean_names(df)
    df["gender"] = gender
    df = df.drop(columns=["obs_status"], errors="ignore")
    print(f"  imported {len(df):,} rows (decoded as {encoding_used})")
    return df


# Characters that indicate a likely encoding problem: the Unicode replacement
# character (a decode failure), stray/lone diacritics with no base letter to
# attach to (the classic symptom of an accented character lost in translation,
# e.g. "Eli¨ka" instead of "Eliška"), and digits. Deliberately does NOT flag
# apostrophes, hyphens or spaces, which are legitimate in real Swiss-registered
# names (e.g. "N'Guessan", "Jean-Pierre", "Anne Sophie").
SUSPICIOUS_NAME_PATTERN = re.compile(r"[�¨´`^~¸°\d]")


def check_name_encoding(df: pd.DataFrame, column: str) -> pd.Series:
    values = df[column].dropna().astype(str)
    suspicious = values[values.str.contains(SUSPICIOUS_NAME_PATTERN, regex=True)]
    return pd.Series(sorted(suspicious.unique()))


# =============================================================================
# DATA IMPORT
# =============================================================================

def load_px_data():
    print("\n=== Importing newborn-name data (stats.swiss SDMX API) ===\n")
    px_female = import_px_data("female")
    px_male = import_px_data("male")
    return px_female, px_male


def detect_years(rankings: pd.DataFrame):
    """DATA_YEAR = most recent year with national ('Suisse') birth counts.
    Deliberately NOT based on today's date - BFS publishes with a lag, so
    "current year - 1" can point at a year that isn't published yet."""
    global DATA_YEAR, COMPARISON_YEAR, OUTPUT_DIR
    national = rankings[rankings["region_linguistique_canton"] == NATIONAL_REGION]
    DATA_YEAR = int(national["year"].max())
    COMPARISON_YEAR = DATA_YEAR - 1
    OUTPUT_DIR = BASE_DIR / "output" / str(DATA_YEAR)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Detected data year: {DATA_YEAR} (comparison year: {COMPARISON_YEAR})\n")


def load_population_data():
    print("=== Importing total-population data (auto-discovered via BFS DAM API) ===\n")

    state = load_population_state()
    frames = {}
    next_state = {}

    for gender, content_id in POPULATION_CONTENT_IDS.items():
        resolved = resolve_latest_population_asset(content_id)
        gender_state = state[gender]
        known_current = gender_state.get("current")

        if known_current is None or resolved["period"] != known_current["period"]:
            # A genuinely new BFS edition (or the very first run ever): roll the window
            # forward - today's "current" becomes tomorrow's "previous".
            current = resolved
            previous = known_current if known_current is not None else gender_state["previous"]
        else:
            # Same edition as last run (re-running the same year, e.g. while iterating) -
            # keep using the previous edition already on file instead of drifting.
            current = known_current
            previous = gender_state["previous"]

        next_state[gender] = {"current": current, "previous": previous}

        print(
            f"  {gender}: current edition {current['period']} (damId {current['dam_id']}), "
            f"previous edition {previous['period']} (damId {previous['dam_id']}, from local state)"
        )
        if int(current["period"]) - int(previous["period"]) != 1:
            print(
                f"  WARNING: the {gender} previous edition is {previous['period']}, expected "
                f"{int(current['period']) - 1}. {POPULATION_STATE_FILE.name} is probably stale "
                f"(script wasn't run in over a year?) - delete it to reset to the seed, or edit it by hand."
            )

        frames[f"all_{gender}_current"] = import_population_data(
            current["url"], f"all_{gender}_names_ch", current["period"], gender)
        frames[f"all_{gender}_previous"] = import_population_data(
            previous["url"], f"all_{gender}_names_ch", previous["period"], gender)

    save_population_state(next_state)
    print(f"  (updated {POPULATION_STATE_FILE.name})\n")

    return frames


# =============================================================================
# DATA QUALITY CHECKS (encoding + cross-source year consistency)
# =============================================================================

def run_data_quality_checks(px_female: pd.DataFrame, px_male: pd.DataFrame, population_frames: dict) -> list:
    """Two independent checks, both printed AND saved to a log file so they
    can be reviewed after the fact instead of scrolling back through console
    output:
    1. Encoding: do any names contain characters that suggest a decoding
       problem, as opposed to unusual-but-legitimate characters?
    2. Freshness: PX Web's most recent year (DATA_YEAR) is one BFS dataset;
       the population census is a completely independent one. If the census
       doesn't yet have any rows for DATA_YEAR births, that's a sign PX Web
       is ahead of the census - worth knowing before trusting figures that
       depend on it (most common name, name length)."""
    lines = [f"Data quality report - data year {DATA_YEAR}", ""]

    lines.append("--- Encoding check (names containing characters that suggest a decoding problem) ---")
    checks = [
        ("PX female (prenom)", px_female, "prenom"),
        ("PX male (prenom)", px_male, "prenom"),
        ("Population female (firstname)", population_frames["all_female_current"], "firstname"),
        ("Population male (firstname)", population_frames["all_male_current"], "firstname"),
    ]
    for label, df, col in checks:
        suspicious = check_name_encoding(df, col)
        if len(suspicious):
            lines.append(f"  {label}: {len(suspicious)} suspicious name(s) - {list(suspicious)}")
        else:
            lines.append(f"  {label}: OK, nothing suspicious")

    lines.append("")
    lines.append("--- PX Web vs. population census: does the census corroborate PX Web's latest year? ---")
    for gender in ("female", "male"):
        current = population_frames[f"all_{gender}_current"]
        years_present = set(current["yearofbirth"].unique())
        if DATA_YEAR in years_present:
            n = int((current["yearofbirth"] == DATA_YEAR).sum())
            lines.append(f"  {gender}: OK - population census has {n} name row(s) for yearofbirth={DATA_YEAR}.")
        else:
            latest_available = max(years_present) if years_present else "none"
            lines.append(
                f"  WARNING: {gender} population census has NO rows for yearofbirth={DATA_YEAR} "
                f"(PX Web's most recent year). Latest birth year found in the census: {latest_available}. "
                f"The census may not have caught up with this year's births yet - 'most common name' and "
                f"name-length figures that rely on {DATA_YEAR} births may be incomplete."
            )

    lines.append("")
    lines.append("--- Manual review reminder (not automatically verified) ---")
    lines.append(
        "  The 'Diversité des prénoms' / 'Vielfalt der Vornamen' paragraph (individualisation "
        "trend since 1980) and the 'nos cartes' / 'unsere Karten' sentence (regional map) are "
        "static text in build_article_sections() - PX Web only goes back to 2000, so neither is "
        "re-verified against data each run. Skim them before publishing."
    )

    return lines


def write_data_quality_log(lines: list) -> None:
    print("\n" + "\n".join(lines) + "\n")
    log_path = OUTPUT_DIR / f"data_quality_log_{DATA_YEAR}.txt"
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  (data quality log saved to {log_path.name})\n")


# =============================================================================
# RANKINGS (newborns of DATA_YEAR, with historical series for context)
# =============================================================================

def build_rankings(px_female: pd.DataFrame, px_male: pd.DataFrame) -> pd.DataFrame:
    """px_female/px_male already arrive long (prenom, region_linguistique_canton,
    year, value, gender) and pre-filtered to the "Nombre" unit by the PX Web
    API query - just combine and add a tie-free custom rank per
    region/year/gender (ties broken alphabetically, like the original script)."""
    combined = pd.concat([px_female, px_male], ignore_index=True)
    # BFS explicitly publishes "0" (not just a blank cell) for a name/year
    # with no births at all - those aren't a real rank and must be dropped,
    # or they'd all tie for last place and pollute rank-change computations.
    combined = combined[combined["value"] > 0].copy()

    combined = combined.sort_values(
        by=["region_linguistique_canton", "year", "gender", "value", "prenom"],
        ascending=[True, True, True, False, True],
    )
    combined["custom_rank"] = (
        combined.groupby(["region_linguistique_canton", "year", "gender"]).cumcount() + 1
    )
    return combined.reset_index(drop=True)


def top_n(rankings: pd.DataFrame, region: str, gender: str, year: int, n: int) -> pd.DataFrame:
    return (
        rankings[
            (rankings["region_linguistique_canton"] == region)
            & (rankings["gender"] == gender)
            & (rankings["year"] == year)
            & (rankings["custom_rank"] <= n)
        ]
        .sort_values("custom_rank")[["prenom", "value", "custom_rank"]]
        .reset_index(drop=True)
    )


def national_evolution_table(rankings: pd.DataFrame, gender: str) -> pd.DataFrame:
    """Rank, year by year, of today's top EVOLUTION_N_NAMES national names,
    over the last EVOLUTION_N_YEARS years. Rows ordered by current rank,
    columns are years; a name with no row in a given year (e.g. too rare to
    be published that year) shows as NaN, rendered as "-" by the caller."""
    current_top = top_n(rankings, NATIONAL_REGION, gender, DATA_YEAR, EVOLUTION_N_NAMES)["prenom"].tolist()
    years = list(range(DATA_YEAR - EVOLUTION_N_YEARS + 1, DATA_YEAR + 1))

    subset = rankings[
        (rankings["region_linguistique_canton"] == NATIONAL_REGION)
        & (rankings["gender"] == gender)
        & (rankings["year"].isin(years))
        & (rankings["prenom"].isin(current_top))
    ][["prenom", "year", "custom_rank"]]

    pivot = subset.pivot(index="prenom", columns="year", values="custom_rank")
    return pivot.reindex(index=current_top, columns=years)


# =============================================================================
# YEAR-OVER-YEAR RANK CHANGES (climbers / fallers / new entries / exits)
# =============================================================================

def build_rank_changes(rankings: pd.DataFrame) -> pd.DataFrame:
    """No cap here - every ranked name gets a row, however deep. Different
    consumers apply their own threshold: new_entries_and_exits() uses
    TOP_N_DISPLAY, climbers_and_fallers() uses CLIMBER_RANK_CEILING."""
    prev = rankings[
        (rankings["region_linguistique_canton"] == NATIONAL_REGION)
        & (rankings["year"] == COMPARISON_YEAR)
    ][["prenom", "gender", "value", "custom_rank"]].rename(
        columns={"value": "count_prev", "custom_rank": "rank_prev"}
    )

    curr = rankings[
        (rankings["region_linguistique_canton"] == NATIONAL_REGION)
        & (rankings["year"] == DATA_YEAR)
    ][["prenom", "gender", "value", "custom_rank"]].rename(
        columns={"value": "count_current", "custom_rank": "rank_current"}
    )

    changes = prev.merge(curr, on=["prenom", "gender"], how="outer")
    # Sentinel for a name entirely absent from one side (never given before, or
    # given zero times this year) - one rank worse than the least popular name
    # actually tracked that year.
    rank_prev_sentinel = int(prev["rank_prev"].max()) + 1
    rank_current_sentinel = int(curr["rank_current"].max()) + 1
    changes["rank_prev"] = changes["rank_prev"].fillna(rank_prev_sentinel).astype(int)
    changes["rank_current"] = changes["rank_current"].fillna(rank_current_sentinel).astype(int)
    changes["rank_change"] = changes["rank_prev"] - changes["rank_current"]  # positive = climbed
    changes["count_change"] = changes["count_current"] - changes["count_prev"]

    changes["status"] = "Continued"
    changes.loc[changes["count_prev"].isna() & changes["count_current"].notna(), "status"] = "New Entry"
    changes.loc[changes["count_prev"].notna() & changes["count_current"].isna(), "status"] = "Dropped Out"

    return changes


def climbers_and_fallers(changes: pd.DataFrame, n: int = 10, max_rank: int = CLIMBER_RANK_CEILING):
    """Top n climbers/fallers PER GENDER (not n total split however it falls).
    Restricted to names ranked within the top `max_rank` in BOTH years:
    build_rank_changes() itself has no cap (a name can validly be ranked
    900th), but for "biggest movers" specifically, a swing entirely outside
    the top {max_rank} isn't a meaningful headline claim - it's usually just
    small-sample noise from names given only a handful of times."""
    continued = changes[
        (changes["status"] == "Continued")
        & (changes["rank_prev"] <= max_rank)
        & (changes["rank_current"] <= max_rank)
    ]
    cols = ["prenom", "gender", "rank_prev", "rank_current", "rank_change", "count_prev", "count_current", "count_change"]

    climbers = (
        continued[continued["rank_change"] > 0]
        .sort_values("rank_change", ascending=False)
        .groupby("gender", group_keys=False)
        .head(n)[cols]
    )
    fallers = (
        continued[continued["rank_change"] < 0]
        .sort_values("rank_change")
        .groupby("gender", group_keys=False)
        .head(n)[cols]
    )
    return climbers.reset_index(drop=True), fallers.reset_index(drop=True)


def new_entries_and_exits(changes: pd.DataFrame):
    # new_entries keeps rank_prev/count_prev too (not just the current rank) so
    # the article can show where each name came from, not just where it landed.
    new_entries = changes[
        (changes["rank_current"] <= TOP_N_DISPLAY) & (changes["rank_prev"] > TOP_N_DISPLAY)
    ].sort_values("rank_current")[
        ["prenom", "gender", "rank_prev", "rank_current", "count_prev", "count_current"]
    ]

    exits = changes[
        (changes["rank_prev"] <= TOP_N_DISPLAY) & (changes["rank_current"] > TOP_N_DISPLAY)
    ].sort_values("rank_prev")[["prenom", "gender", "rank_prev", "count_prev"]]

    return new_entries.reset_index(drop=True), exits.reset_index(drop=True)


# =============================================================================
# TOTAL POPULATION ANALYSIS (all people alive, not just newborns)
# =============================================================================

def build_population_changes(all_current: pd.DataFrame, all_previous: pd.DataFrame) -> pd.DataFrame:
    current = (
        all_current.groupby(["firstname", "gender"])["value"]
        .sum()
        .reset_index(name="total_population_current")
    )
    previous = (
        all_previous.groupby(["firstname", "gender"])["value"]
        .sum()
        .reset_index(name="total_population_previous")
    )

    changes = current.merge(previous, on=["firstname", "gender"], how="left")
    changes["total_population_previous"] = changes["total_population_previous"].fillna(0)
    changes["population_change"] = changes["total_population_current"] - changes["total_population_previous"]
    return changes.sort_values("total_population_current", ascending=False).reset_index(drop=True)


def most_common_names(population_changes: pd.DataFrame, gender: str, n: int = 20) -> pd.DataFrame:
    return (
        population_changes[population_changes["gender"] == gender]
        .head(n)[["firstname", "total_population_current", "total_population_previous", "population_change"]]
        .reset_index(drop=True)
    )


def biggest_population_changes(population_changes: pd.DataFrame, n: int = 10):
    increases = population_changes[population_changes["population_change"] > 0].sort_values(
        "population_change", ascending=False
    ).head(n)
    decreases = population_changes[population_changes["population_change"] < 0].sort_values(
        "population_change"
    ).head(n)
    cols = ["firstname", "gender", "total_population_current", "total_population_previous", "population_change"]
    return increases[cols].reset_index(drop=True), decreases[cols].reset_index(drop=True)


# =============================================================================
# NAME LENGTH (average length of newborns' first names, DATA_YEAR only)
# =============================================================================

def _weighted_avg_length(df: pd.DataFrame) -> float:
    return (df["name_length"] * df["value"]).sum() / df["value"].sum()


def name_length_summary(all_names_current: pd.DataFrame) -> dict:
    """Two different, both legitimate, notions of "average name length":
    - weighted: length experienced by a randomly picked baby (short, popular
      names like Emma/Noah pull this down - this is what most people intuit
      when they say "names are short this year").
    - unweighted: simple mean across the roster of distinct names given that
      year, each name counted once regardless of how many babies got it.
    """
    births = all_names_current[all_names_current["yearofbirth"] == DATA_YEAR].copy()
    if births.empty:
        raise ValueError(
            f"No population rows with yearofbirth == {DATA_YEAR}. The population "
            f"snapshot may not cover births from {DATA_YEAR} yet (see the data quality log)."
        )
    births["name_length"] = births["firstname"].str.len()

    weighted_by_gender = {gender: _weighted_avg_length(g) for gender, g in births.groupby("gender")}
    unweighted_by_gender = births.groupby("gender")["name_length"].mean()

    return {
        "weighted": {
            "overall": round(_weighted_avg_length(births), 2),
            "female": round(weighted_by_gender.get("female", float("nan")), 2),
            "male": round(weighted_by_gender.get("male", float("nan")), 2),
        },
        "unweighted": {
            "overall": round(births["name_length"].mean(), 2),
            "female": round(unweighted_by_gender.get("female", float("nan")), 2),
            "male": round(unweighted_by_gender.get("male", float("nan")), 2),
        },
    }
    # Note: the unweighted "overall" figure sits between the unweighted male and
    # female figures (it's a pooled mean of all distinct names, both genders) -
    # it should never be compared directly against the WEIGHTED male/female
    # figures, which use a different method and are usually noticeably lower.


# =============================================================================
# TEXT GENERATION
# =============================================================================

def fmt_fr(n) -> str:
    """French-Swiss thousands separator: a plain space - 12 345."""
    return f"{int(round(n)):,}".replace(",", " ")


def fmt_de(n) -> str:
    """German-Swiss thousands separator: an apostrophe - 12'345."""
    return f"{int(round(n)):,}".replace(",", "'")


def fmt_by_lang(n, lang: str) -> str:
    return fmt_de(n) if lang == "de" else fmt_fr(n)


def round_nearest(n, base: int = 500) -> int:
    """Journalistic rounding for "près de X" style figures (e.g. 73'412 -> 73'500)."""
    return int(base * round(n / base))


def decimal_comma(x: float, decimals: int) -> str:
    """FR/DE both use a comma as the decimal separator: 5.6 -> "5,6"."""
    return f"{x:.{decimals}f}".replace(".", ",")


def ordinal_fr(n: int) -> str:
    """Every call site here is "la {ordinal} place" (a feminine noun), so 1
    needs the feminine "1ère", not the default masculine "1er"."""
    return "1ère" if n == 1 else f"{n}e"


def format_year_groups(years: list) -> list:
    """Sorted years -> list of consecutive-run lists, e.g. [2011,2012,2014] -> [[2011,2012],[2014]]."""
    if not years:
        return []
    runs, run = [], [years[0]]
    for y in years[1:]:
        if y == run[-1] + 1:
            run.append(y)
        else:
            runs.append(run)
            run = [y]
    runs.append(run)
    return runs


def format_years_fr(years: list) -> str:
    """1997, 1998, 1999 -> "de 1997 à 1999"; a run of exactly 2 -> "1997 et 1998";
    a lone year -> "1997". Groups are then joined with commas, and the final
    connector is "puis" if the last group is a range, "et" otherwise - this
    exactly reproduces how BFS-derived articles phrase these lists."""
    tokens, last_is_range = [], False
    for run in format_year_groups(years):
        if len(run) >= 3:
            tokens.append(f"de {run[0]} à {run[-1]}")
            last_is_range = True
        elif len(run) == 2:
            tokens.append(f"{run[0]} et {run[1]}")
            last_is_range = False
        else:
            tokens.append(str(run[0]))
            last_is_range = False
    if len(tokens) == 1:
        return tokens[0]
    connector = "puis" if last_is_range else "et"
    return ", ".join(tokens[:-1]) + f", {connector} " + tokens[-1]


def format_years_de(years: list) -> str:
    """Same idea as format_years_fr, but German style: runs (of any length >=2)
    are simply comma-joined (no "und" internally), only 3+ runs become a
    "von X bis Y" range, and the final connector is "sowie" for a trailing
    range, "und" otherwise."""
    tokens, last_is_range = [], False
    for run in format_year_groups(years):
        if len(run) >= 3:
            tokens.append(f"von {run[0]} bis {run[-1]}")
            last_is_range = True
        else:
            tokens.append(", ".join(str(y) for y in run))
            last_is_range = False
    if len(tokens) == 1:
        return tokens[0]
    connector = "sowie" if last_is_range else "und"
    return ", ".join(tokens[:-1]) + f" {connector} " + tokens[-1]


def gap_text_fr(n: int) -> str:
    return "une année" if n == 1 else f"{n} années"


def gap_text_de(n: int) -> str:
    return "einem Jahr" if n == 1 else f"{n} Jahren"


def round_down_to_ten(n: int) -> int:
    """For the "reculent de plus de X rangs" / "verloren über X Plätze"
    understatement: round a loss down to the nearest ten so the claim is
    always conservatively true."""
    return (n // 10) * 10


GERMAN_TENS = {
    10: "zehn", 20: "zwanzig", 30: "dreissig", 40: "vierzig", 50: "fünfzig",
    60: "sechzig", 70: "siebzig", 80: "achtzig", 90: "neunzig",
}


def number_word_de(n: int) -> str:
    """Spells out round tens under 100 for German prose (e.g. "über sechzig
    Plätze"), matching how this is actually written in practice. Compound
    hundred-words like "siebenhundertvierzig" read as unusual/stilted in a
    news sentence, so anything >= 100 is left as a plain digit instead."""
    return GERMAN_TENS.get(n, str(n))


# --- shared table-building helpers -------------------------------------------

GENDER_LABELS_PLURAL = {
    "fr": {"male": "Garçons", "female": "Filles"},
    "de": {"male": "Jungen", "female": "Mädchen"},
}
NAME_HEADER = {"fr": "Prénom", "de": "Name"}

_TH_STYLE = "text-align:left;border-bottom:2px solid #ccc;padding:4px 8px;"
_TD_STYLE = "padding:4px 8px;border-bottom:1px solid #eee;"
_TOP3_HIGHLIGHT_STYLE = "background:#fed7aa;font-weight:700;"


def _table(headers_html: str, rows_html: str, font_size: str = ".92rem") -> str:
    return (
        f"<table style='border-collapse:collapse;width:100%;margin:.5rem 0 1.25rem;font-size:{font_size};'>"
        f"<thead><tr>{headers_html}</tr></thead><tbody>{rows_html}</tbody></table>"
    )


def _th(headers) -> str:
    return "".join(f"<th style='{_TH_STYLE}'>{h}</th>" for h in headers)


def _td_row(cells, first_cell_bold: bool = False) -> str:
    tds = []
    for i, c in enumerate(cells):
        style = _TD_STYLE + ("font-weight:600;" if i == 0 and first_cell_bold else "")
        tds.append(f"<td style='{style}'>{c}</td>")
    return "<tr>" + "".join(tds) + "</tr>"


# --- 1. national top-10 evolution table --------------------------------------

def evolution_table_html(pivot: pd.DataFrame, lang: str) -> str:
    head_html = _th([NAME_HEADER[lang]] + list(pivot.columns))
    rows = []
    for prenom, row in pivot.iterrows():
        cells = [f"<td style='{_TD_STYLE}font-weight:600;'>{prenom}</td>"]
        for year in pivot.columns:
            value = row[year]
            if pd.isna(value):
                cells.append(f"<td style='{_TD_STYLE}'>-</td>")
            else:
                rank = int(value)
                style = _TD_STYLE + (_TOP3_HIGHLIGHT_STYLE if rank <= 3 else "")
                cells.append(f"<td style='{style}'>{rank}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return _table(head_html, "".join(rows), font_size=".85rem")


# --- 2. climbers / fallers (single gender, with counts, top 3 highlighted) ----

MOVERS_TABLE_HEADERS = {
    "fr": ["Prénom", "Rang {prev}", "Rang {curr}", "Effectif {prev}", "Effectif {curr}", "Évolution"],
    "de": ["Name", "Rang {prev}", "Rang {curr}", "Anzahl {prev}", "Anzahl {curr}", "Veränderung"],
}


def movers_table_html(df: pd.DataFrame, lang: str, comparison_year: int, data_year: int) -> str:
    """The first 3 rows are assumed to be the biggest movers (df must already
    be sorted that way) - their "Évolution" cell is highlighted."""
    headers = [h.format(prev=comparison_year, curr=data_year) for h in MOVERS_TABLE_HEADERS[lang]]
    rows = []
    for i, (_, row) in enumerate(df.iterrows()):
        change = int(row["rank_change"])
        change_str = f"+{change}" if change > 0 else str(change)
        cells = [
            row["prenom"], int(row["rank_prev"]), int(row["rank_current"]),
            int(row["count_prev"]), int(row["count_current"]), change_str,
        ]
        tds = []
        for j, c in enumerate(cells):
            style = _TD_STYLE
            if j == 0:
                style += "font-weight:600;"
            if j == len(cells) - 1 and i < 3:
                style += _TOP3_HIGHLIGHT_STYLE
            tds.append(f"<td style='{style}'>{c}</td>")
        rows.append("<tr>" + "".join(tds) + "</tr>")
    return _table(_th(headers), "".join(rows))


# --- 3. new entries into the national top 100 (single gender) -----------------
# Shows both where a name landed AND where it came from (rank_prev/count_prev),
# matching the same variables as the movers tables above.

NEW_ENTRIES_TABLE_HEADERS = {
    "fr": ["Prénom", "Rang {prev}", "Rang {curr}", "Effectif {prev}", "Effectif {curr}"],
    "de": ["Name", "Rang {prev}", "Rang {curr}", "Anzahl {prev}", "Anzahl {curr}"],
}


def new_entries_table_html(df: pd.DataFrame, lang: str, comparison_year: int, data_year: int) -> str:
    headers = [h.format(prev=comparison_year, curr=data_year) for h in NEW_ENTRIES_TABLE_HEADERS[lang]]
    rows = []
    for _, row in df.iterrows():
        # count_prev (and thus rank_prev) is NaN/sentinel only for a name that
        # was never given at all the year before - as opposed to a real, if
        # modest, rank below the top 100.
        if pd.isna(row["count_prev"]):
            rank_prev_display, count_prev_display = "-", "-"
        else:
            rank_prev_display, count_prev_display = str(int(row["rank_prev"])), int(row["count_prev"])
        cells = [row["prenom"], rank_prev_display, int(row["rank_current"]), count_prev_display, int(row["count_current"])]
        rows.append(_td_row(cells, first_cell_bold=True))
    return _table(_th(headers), "".join(rows))


# --- 4. regional top 3 (one table, all regions, colour-coded rows) -----------

REGIONAL_TABLE_HEADERS = {
    "fr": ["Région", "Rang", "Garçon", "Fille"],
    "de": ["Region", "Rang", "Junge", "Mädchen"],
}
REGION_DISPLAY_NAMES = {
    "fr": {
        "alemanique": "Suisse alémanique", "romande": "Suisse romande",
        "italienne": "Suisse italienne", "romanche": "Suisse romanche",
    },
    "de": {
        "alemanique": "Deutschschweiz", "romande": "Romandie",
        "italienne": "Italienische Schweiz", "romanche": "Rätoromanische Schweiz",
    },
}
REGION_ROW_COLORS = {
    "alemanique": "#dbeafe",  # light blue
    "romande": "#fef3c7",     # light amber
    "italienne": "#dcfce7",   # light green
    "romanche": "#ede9fe",    # light purple
}


def regional_table_html(rankings_regional: pd.DataFrame, lang: str) -> str:
    rows = []
    for key, region_label in LINGUISTIC_REGIONS.items():
        region_display = REGION_DISPLAY_NAMES[lang][key]
        bg = REGION_ROW_COLORS[key]
        sub = rankings_regional[rankings_regional["region"] == region_label]
        for rank in (1, 2, 3):
            male_row = sub[(sub["gender"] == "male") & (sub["custom_rank"] == rank)]
            female_row = sub[(sub["gender"] == "female") & (sub["custom_rank"] == rank)]
            male_name = male_row["prenom"].iloc[0] if len(male_row) else "-"
            female_name = female_row["prenom"].iloc[0] if len(female_row) else "-"
            cells = [region_display if rank == 1 else "", rank, male_name, female_name]
            tds = "".join(f"<td style='{_TD_STYLE}background:{bg};'>{c}</td>" for c in cells)
            rows.append(f"<tr>{tds}</tr>")
    return _table(_th(REGIONAL_TABLE_HEADERS[lang]), "".join(rows))


# --- 5. most common names overall (population), top 3 per gender, with change -

MOST_COMMON_TABLE_HEADERS = {
    "fr": ["Sexe", "Rang", "Prénom", "Effectif {curr}", "Effectif {prev}", "Évolution"],
    "de": ["Geschlecht", "Rang", "Name", "Anzahl {curr}", "Anzahl {prev}", "Veränderung"],
}


def most_common_table_html(male_top3: pd.DataFrame, female_top3: pd.DataFrame, lang: str,
                            comparison_year: int, data_year: int) -> str:
    headers = [h.format(prev=comparison_year, curr=data_year) for h in MOST_COMMON_TABLE_HEADERS[lang]]
    rows = []
    for gender, df in [("male", male_top3), ("female", female_top3)]:
        gender_label = GENDER_LABELS_PLURAL[lang][gender]
        for i, row in enumerate(df.itertuples(index=False), start=1):
            change = row.total_population_current - row.total_population_previous
            change_str = f"+{fmt_by_lang(change, lang)}" if change > 0 else (
                f"-{fmt_by_lang(abs(change), lang)}" if change < 0 else "0"
            )
            cells = [
                gender_label if i == 1 else "", i, row.firstname,
                fmt_by_lang(row.total_population_current, lang),
                fmt_by_lang(row.total_population_previous, lang),
                change_str,
            ]
            rows.append(_td_row(cells))
    return _table(_th(headers), "".join(rows))


# --- article assembly ---------------------------------------------------------
# Article text is kept as PLAIN TEXT (no HTML tags) since the target CMS takes
# plain text, not HTML - each section gets its own heading, a highly visible
# "code block" with the plain-text copy, then its supporting tables (with
# explicit, self-explanatory titles) right below.

TABLE_TITLES = {
    "fr": {
        "evolution": "Évolution du rang national, {gender} ({y0}–{y1})",
        "climbers": "Plus fortes progressions de rang, {gender} ({prev}→{curr})",
        "fallers": "Plus forts reculs de rang, {gender} ({prev}→{curr})",
        "new_entries": "Nouvelles entrées dans le top 100, {gender} ({prev}→{curr})",
        "regional": "Top 3 par région linguistique ({curr})",
        "most_common": "Prénoms les plus portés dans la population totale, par sexe ({curr} vs {prev})",
    },
    "de": {
        "evolution": "Entwicklung des nationalen Rangs, {gender} ({y0}–{y1})",
        "climbers": "Stärkste Rangaufstiege, {gender} ({prev}→{curr})",
        "fallers": "Stärkste Rangabstiege, {gender} ({prev}→{curr})",
        "new_entries": "Neueinsteiger in die Top 100, {gender} ({prev}→{curr})",
        "regional": "Top 3 nach Sprachregion ({curr})",
        "most_common": "Häufigste Vornamen der Gesamtbevölkerung, nach Geschlecht ({curr} vs. {prev})",
    },
}


# --- "years as national #1" narrative (leader continuity / return / first time) ----

def leader_history(rankings: pd.DataFrame, gender: str, name: str, year: int) -> dict:
    """All years strictly before `year` where `name` (gender) held national
    rank 1. Returns the status needed to phrase the leader paragraph:
    - "first_time": never been #1 before.
    - "continuing": was also #1 the immediately preceding year.
    - "returning": was #1 before, but not last year (includes the gap length)."""
    prior_years = sorted(rankings[
        (rankings["region_linguistique_canton"] == NATIONAL_REGION)
        & (rankings["gender"] == gender)
        & (rankings["prenom"] == name)
        & (rankings["custom_rank"] == 1)
        & (rankings["year"] < year)
    ]["year"].tolist())

    if not prior_years:
        return {"status": "first_time", "years": []}
    if prior_years[-1] == year - 1:
        return {"status": "continuing", "years": prior_years}
    return {"status": "returning", "years": prior_years, "gap": (year - 1) - prior_years[-1]}


def leader_paragraph_fr(name: str, history: dict, second: str, third: str, year: int,
                         podium_unchanged: bool, lead_sentence: str = None) -> str:
    default_lead = f"{name} prend la tête du classement, devant {second} et {third}."
    if history["status"] == "first_time":
        return lead_sentence or f"{name} prend la tête du classement pour la première fois, devant {second} et {third}."
    years_text = format_years_fr(history["years"])
    if history["status"] == "continuing":
        clause = f"Déjà en tête en {years_text}, {name} conserve sa première place en {year}."
        podium = (
            f"Comme l'année précédente, {second} et {third} complètent le podium."
            if podium_unchanged else f"{second} et {third} complètent le podium."
        )
        return f"{clause} {podium}"
    gap = gap_text_fr(history["gap"])
    lead = lead_sentence or default_lead
    return (
        f"{lead} Après {gap} d'interruption, le prénom retrouve ainsi la première place, "
        f"qu'il avait déjà occupée en {years_text}."
    )


def leader_paragraph_de(name: str, gender: str, history: dict, second: str, third: str, year: int,
                         podium_unchanged: bool, lead_sentence: str = None) -> str:
    pronoun = "Sie" if gender == "female" else "Er"
    rel_pronoun = "die" if gender == "female" else "der"
    default_lead = f"{name} ist neu an erster Stelle, gefolgt von {second} und {third}."
    if history["status"] == "first_time":
        return lead_sentence or f"{name} steht zum ersten Mal an erster Stelle, gefolgt von {second} und {third}."
    years_text = format_years_de(history["years"])
    if history["status"] == "continuing":
        clause = f"{name}, {rel_pronoun} bereits {years_text} am beliebtesten war, behielt auch {year} wieder die Spitzenposition."
        podium = (
            f"Dahinter folgen wie bereits letztes Jahr {second} und {third}."
            if podium_unchanged else f"Dahinter folgen {second} und {third}."
        )
        return f"{clause} {podium}"
    gap = gap_text_de(history["gap"])
    lead = lead_sentence or default_lead
    return (
        f"{lead} {name} nimmt damit nach {gap} Unterbruch wieder den ersten Platz ein. "
        f"{pronoun} war bereits {years_text} der beliebteste Vorname."
    )


def build_article_sections(ctx: dict, t: dict) -> dict:
    """Returns {lang: [section, ...]}, each section:
    {"label": str | None, "text": plain-text str (paragraphs separated by a
    blank line), "tables": [(title, html), ...]}.
    Consumed by build_article_page_html (one language) and build_report_html
    (all languages on one page). English is intentionally not generated -
    only FR/DE are produced, per editorial decision."""
    year, prev_year = ctx["data_year"], ctx["comparison_year"]
    y0 = year - EVOLUTION_N_YEARS + 1

    def title(lang, key, gender=None):
        kwargs = {"prev": prev_year, "curr": year, "y0": y0, "y1": year}
        if gender:
            kwargs["gender"] = GENDER_LABELS_PLURAL[lang][gender]
        return TABLE_TITLES[lang][key].format(**kwargs)

    sections = {}

    # =====================================================================
    # FRENCH
    # =====================================================================
    sections["fr"] = [
        {
            "label": "Titre",
            "text": "Voici les prénoms les plus populaires en Suisse – où se situe le vôtre dans le classement ?",
            "tables": [],
        },
        {
            "label": "Chapô",
            "text": (
                f"Quels ont été les prénoms les plus populaires en {year} ? Et comment la popularité du "
                f"vôtre a-t-elle évolué au fil des ans ? Découvrez-le grâce à notre outil interactif."
            ),
            "tables": [],
        },
        {
            "label": f"Les prénoms vedettes de {year}",
            "text": (
                f"{ctx['national_female_1']} et {ctx['national_male_1']} ont été les prénoms les plus donnés aux nouveau-nés en Suisse en {year}, selon la statistique annuelle publiée par l'Office fédéral de la statistique (OFS). L'an dernier, {ctx['national_male_1_count']} bébés ont été prénommés {ctx['national_male_1']} et {ctx['national_female_1_count']} {ctx['national_female_1']}.\n\n"
                f"{ctx['male_leader_text_fr']}\n\n"
                f"{ctx['female_leader_text_fr']}"
            ),
            "tables": [
                (title("fr", "evolution", "male"), evolution_table_html(t["evolution_male"], "fr")),
                (title("fr", "evolution", "female"), evolution_table_html(t["evolution_female"], "fr")),
            ],
        },
        {
            "label": "Qui progresse, qui recule ?",
            "text": (
                f"{ctx['female_climber']}, chez les filles, et {ctx['male_climber']}, chez les garçons, enregistrent de belles progressions : la première passe de la {ordinal_fr(ctx['female_climber_rank_prev'])} à la {ordinal_fr(ctx['female_climber_rank_curr'])} place, le second de la {ordinal_fr(ctx['male_climber_rank_prev'])} à la {ordinal_fr(ctx['male_climber_rank_curr'])}.\n\n"
                f"À l'inverse, {ctx['female_faller']} et {ctx['male_faller']} accusent un net recul. {ctx['female_faller']} passe de la {ordinal_fr(ctx['female_faller_rank_prev'])} à la {ordinal_fr(ctx['female_faller_rank_curr'])} place et {ctx['male_faller']} de la {ordinal_fr(ctx['male_faller_rank_prev'])} à la {ordinal_fr(ctx['male_faller_rank_curr'])}. Tous deux reculent ainsi de plus de {ctx['fallers_min_loss_rounded']} rangs."
            ),
            "tables": [
                (title("fr", "climbers", "male"), movers_table_html(t["climbers_male"], "fr", prev_year, year)),
                (title("fr", "climbers", "female"), movers_table_html(t["climbers_female"], "fr", prev_year, year)),
                (title("fr", "fallers", "male"), movers_table_html(t["fallers_male"], "fr", prev_year, year)),
                (title("fr", "fallers", "female"), movers_table_html(t["fallers_female"], "fr", prev_year, year)),
            ],
        },
        {
            "label": "Les petits nouveaux",
            "text": (
                f"{ctx['female_new_entry']} (rang {ctx['female_new_entry_rank']}) et {ctx['male_new_entry']} (rang {ctx['male_new_entry_rank']}) font leur entrée dans le top 100 des prénoms les plus donnés aux nouveau-nés."
            ),
            "tables": [
                (title("fr", "new_entries", "male"), new_entries_table_html(t["new_entries_male"], "fr", prev_year, year)),
                (title("fr", "new_entries", "female"), new_entries_table_html(t["new_entries_female"], "fr", prev_year, year)),
            ],
        },
        {
            "label": "Des préférences régionales",
            "text": (
                f"Les préférences varient aussi selon les régions linguistiques. En Suisse alémanique, {ctx['alemanique_male_1']} et {ctx['alemanique_female_1']} arrivent en tête, tandis qu'en Suisse romande, {ctx['romande_female_1']} et {ctx['romande_male_1']} dominent le classement. En Suisse italienne, {ctx['italienne_male_1']} et {ctx['italienne_female_1']} occupent la première marche du podium.\n\n"
                f"Les goûts diffèrent également d'un canton à l'autre, comme le montrent nos cartes."
            ),
            "tables": [(title("fr", "regional"), regional_table_html(t["regional"], "fr"))],
        },
        {
            "label": "Les prénoms les plus portés en Suisse",
            "text": (
                f"La statistique des prénoms de l'OFS permet aussi de mesurer leur fréquence relative pour chaque année de naissance. Il est ainsi possible de comparer de manière pertinente l'évolution de la popularité de différents prénoms au fil des décennies.\n\n"
                f"Même si {ctx['most_common_female_1']} est en net recul parmi les jeunes générations, il reste le prénom féminin le plus répandu dans l'ensemble de la population suisse, avec près de {fmt_fr(ctx['most_common_female_1_rounded'])} personnes qui le portent. Suivent {ctx['most_common_female_2']}, avec près de {fmt_fr(ctx['most_common_female_2_rounded'])} personnes, et {ctx['most_common_female_3']}, avec {fmt_fr(ctx['most_common_female_3_rounded'])}.\n\n"
                f"Chez les hommes, {ctx['most_common_male_1']} reste le prénom le plus répandu en Suisse : près de {fmt_fr(ctx['most_common_male_1_rounded'])} hommes et garçons le portent. {ctx['most_common_male_2']} et {ctx['most_common_male_3']} arrivent ensuite."
            ),
            "tables": [(title("fr", "most_common"), most_common_table_html(t["most_common_male"], t["most_common_female"], "fr", prev_year, year))],
        },
        {
            "label": "Diversité des prénoms",
            "text": (
                "Après une nette tendance à l'individualisation des prénoms entre 1980 et les années 1990, "
                "les parents suisses se tournent à nouveau davantage vers les mêmes prénoms depuis la fin "
                "des années 1990. La diversité tend ainsi à diminuer."
            ),
            "tables": [],
        },
        {
            "label": "Des prénoms de plus en plus courts ?",
            "text": (
                f"Avec {ctx['national_female_1']} et {ctx['national_male_1']}, ce sont une nouvelle fois deux prénoms courts qui ont été les plus donnés en {year}. Les prénoms des enfants nés cette année-là comptent en moyenne {ctx['avg_length_overall_fmt']} lettres. Ils sont légèrement plus longs chez les filles, avec {ctx['avg_length_female_fmt']} lettres en moyenne, que chez les garçons, avec {ctx['avg_length_male_fmt']}.\n\n"
                f"Les générations plus âgées ont, en moyenne, des prénoms plus longs."
            ),
            "tables": [],
        },
    ]

    # =====================================================================
    # GERMAN
    # =====================================================================
    sections["de"] = [
        {
            "label": "Titel",
            "text": "Das sind die beliebtesten Vornamen – wo liegt Ihrer in der Rangliste?",
            "tables": [],
        },
        {
            "label": "Lead",
            "text": (
                f"Welches waren die beliebtesten Vornamen {year}, und wie hat sich die Beliebtheit Ihres "
                f"eigenen Namens über die Jahre entwickelt? Finden Sie es mit unserem interaktiven Tool heraus."
            ),
            "tables": [],
        },
        {
            "label": f"Die Namen des Jahres {year}",
            "text": (
                f"{ctx['national_female_1']} und {ctx['national_male_1']} waren {year} in der Schweiz die beliebtesten Vornamen für Neugeborene, wie das Bundesamt für Statistik (BFS) in seiner alljährlichen Namens-Statistik mitteilt. {ctx['national_male_1']} wurden im vergangenen Jahr {ctx['national_male_1_count']} Babys genannt, {ctx['national_female_1']} {ctx['national_female_1_count']}.\n\n"
                f"{ctx['male_leader_text_de']}\n\n"
                f"{ctx['female_leader_text_de']}"
            ),
            "tables": [
                (title("de", "evolution", "male"), evolution_table_html(t["evolution_male"], "de")),
                (title("de", "evolution", "female"), evolution_table_html(t["evolution_female"], "de")),
            ],
        },
        {
            "label": "Wer steigt, wer fällt?",
            "text": (
                f"Die Vornamen {ctx['female_climber']} (von Rang {ctx['female_climber_rank_prev']} auf {ctx['female_climber_rank_curr']}) bei den Mädchen und {ctx['male_climber']} (von Rang {ctx['male_climber_rank_prev']} auf {ctx['male_climber_rank_curr']}) bei den Knaben legten in der Rangliste spürbar zu.\n\n"
                f"Deutlich verloren haben {ctx['female_faller']} (von Rang {ctx['female_faller_rank_prev']} auf {ctx['female_faller_rank_curr']}) bei den Mädchen und {ctx['male_faller']} (von Rang {ctx['male_faller_rank_prev']} auf {ctx['male_faller_rank_curr']}) bei den Knaben. Sie verloren beide über {ctx['fallers_min_loss_word_de']} Plätze."
            ),
            "tables": [
                (title("de", "climbers", "male"), movers_table_html(t["climbers_male"], "de", prev_year, year)),
                (title("de", "climbers", "female"), movers_table_html(t["climbers_female"], "de", prev_year, year)),
                (title("de", "fallers", "male"), movers_table_html(t["fallers_male"], "de", prev_year, year)),
                (title("de", "fallers", "female"), movers_table_html(t["fallers_female"], "de", prev_year, year)),
            ],
        },
        {
            "label": "Die Neuankömmlinge",
            "text": (
                f"Neu gehören {ctx['female_new_entry']} (Rang {ctx['female_new_entry_rank']}) und {ctx['male_new_entry']} (Rang {ctx['male_new_entry_rank']}) zu den 100 beliebtesten Vornamen von Neugeborenen."
            ),
            "tables": [
                (title("de", "new_entries", "male"), new_entries_table_html(t["new_entries_male"], "de", prev_year, year)),
                (title("de", "new_entries", "female"), new_entries_table_html(t["new_entries_female"], "de", prev_year, year)),
            ],
        },
        {
            "label": "Regionale Vorlieben",
            "text": (
                f"Die beliebtesten Vornamen {year} unterscheiden sich auch in den Sprachregionen: In der Deutschschweiz stehen {ctx['alemanique_male_1']} und {ctx['alemanique_female_1']} an erster Stelle, während in der Romandie {ctx['romande_female_1']} und {ctx['romande_male_1']} die Rangliste anführen. In der italienischsprachigen Schweiz stehen {ctx['italienne_male_1']} und {ctx['italienne_female_1']} zuoberst auf dem Treppchen.\n\n"
                f"Auch zwischen den Kantonen gibt es unterschiedliche Vorlieben, wie unsere Karten zeigen."
            ),
            "tables": [(title("de", "regional"), regional_table_html(t["regional"], "de"))],
        },
        {
            "label": "Die häufigsten Vornamen der Schweiz",
            "text": (
                f"In der Schweiz lässt sich die BFS-Vornamenstatistik auch in relativen Zahlen pro Jahrgang abfragen. So sind aussagekräftige Vergleiche verschiedener Vornamen punkto Beliebtheit über die Jahrzehnte möglich.\n\n"
                f"Auch wenn sich der Name {ctx['most_common_female_1']} bei jüngeren Jahrgängen im Abschwung befindet, ist er mit fast {fmt_de(ctx['most_common_female_1_rounded'])} Namensträgerinnen immer noch jener weibliche Vorname, der in der Gesamtbevölkerung am häufigsten vorkommt. Dahinter kommen die knapp {fmt_de(ctx['most_common_female_2_rounded'])} {ctx['most_common_female_2']}s und {fmt_de(ctx['most_common_female_3_rounded'])} {ctx['most_common_female_3']}s.\n\n"
                f"Bei den Männern ist {ctx['most_common_male_1']} immer noch der häufigste Vorname in der Schweiz. Fast {fmt_de(ctx['most_common_male_1_rounded'])} Männer und Knaben heissen so. Dahinter folgen {ctx['most_common_male_2']} und {ctx['most_common_male_3']}."
            ),
            "tables": [(title("de", "most_common"), most_common_table_html(t["most_common_male"], t["most_common_female"], "de", prev_year, year))],
        },
        {
            "label": "Vielfalt der Vornamen",
            "text": (
                "Nachdem es von 1980 bis in die 1990er-Jahre einen klaren Trend zur Individualisierung der "
                "Vornamen gegeben hat, wählen die Schweizer Eltern seit Ende der 1990er-Jahre wieder vermehrt "
                "ähnliche Vornamen aus. Die Vielfalt wird kleiner."
            ),
            "tables": [],
        },
        {
            "label": "Werden die Vornamen kürzer?",
            "text": (
                f"Mit {ctx['national_female_1']} und {ctx['national_male_1']} wurden im Jahr {year} erneut zwei kurze Namen am häufigsten vergeben. Die durchschnittliche Vornamenslänge der im Jahr {year} Geborenen liegt bei {ctx['avg_length_overall_fmt']} Zeichen. Bei Mädchen ist die Länge mit {ctx['avg_length_female_fmt']} Zeichen etwas umfangreicher als bei den Buben mit {ctx['avg_length_male_fmt']}.\n\n"
                f"Ältere Personen haben tendenziell längere Vornamen."
            ),
            "tables": [],
        },
    ]

    return sections

# =============================================================================
# HTML REPORT / STANDALONE ARTICLE PAGES
# =============================================================================

ARTICLE_PAGE_CSS = """
body { font-family: -apple-system, Arial, sans-serif; max-width: 900px; margin: 2rem auto; padding: 0 1rem; line-height: 1.5; color: #1a1a1a; }
h1 { font-size: 1.6rem; }
h2 { font-size: 1.2rem; margin-top: 2.5rem; border-bottom: 2px solid #ddd; padding-bottom: .3rem; }
h4 { font-size: .9rem; margin: 1.25rem 0 .3rem; color: #555; }
table { border-collapse: collapse; width: 100%; margin: .25rem 0 1.25rem; font-size: .87rem; }
th, td { border: 1px solid #ddd; padding: .4rem .6rem; text-align: left; }
th { background: #f5f5f5; }
.text-block { background: #f4f4f4; border-left: 4px solid #888; border-radius: 4px; padding: 1rem 1.25rem; margin: .5rem 0 1.25rem; font-family: 'SF Mono', Consolas, monospace; font-size: .92rem; white-space: pre-wrap; word-break: break-word; }
.lang-block { margin-top: 3.5rem; padding-top: 1.5rem; border-top: 4px solid #333; }
"""


def markdown_bold_to_html(text: str) -> str:
    """Converts **name** markers to real <strong> tags (escaping everything
    else first, so this is still safe against stray HTML-special characters).
    Real <strong> tags - not visible literal tag text - are what let a
    browser copy preserve bold via the rich-text (text/html) clipboard when
    pasted into a CMS that accepts rich text."""
    escaped = escape(text)
    return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)


def render_text_block(text: str) -> str:
    return f"<pre class='text-block'>{markdown_bold_to_html(text)}</pre>"


def render_sections_body(sections: list) -> str:
    parts = []
    for section in sections:
        if section.get("label"):
            parts.append(f"<h2>{section['label']}</h2>")
        parts.append(render_text_block(section["text"]))
        for table_title, table_html in section.get("tables", []):
            parts.append(f"<h4>{table_title}</h4>{table_html}")
    return "\n".join(parts)


def build_article_page_html(lang_label: str, sections: list) -> str:
    body = f"<h1>{lang_label} - {DATA_YEAR}</h1>" + render_sections_body(sections)
    return f"<!doctype html><html><head><meta charset='utf-8'><title>{lang_label} {DATA_YEAR}</title><style>{ARTICLE_PAGE_CSS}</style></head><body>{body}</body></html>"


def df_to_html(df: pd.DataFrame) -> str:
    return df.to_html(index=False, border=0, na_rep="-")


def build_report_html(sections_by_lang: dict, tables: dict) -> str:
    parts = [f"<h1>Swiss First Names Analysis - {DATA_YEAR}</h1>"]

    for lang, label in [("fr", "Français"), ("de", "Deutsch")]:
        parts.append(f"<div class='lang-block'><h1>{label}</h1>{render_sections_body(sections_by_lang[lang])}</div>")

    parts.append("<div class='lang-block'><h1>Reference tables (background data, not for publication)</h1>")
    parts.append(f"<h2>Top climbers (10/gender)</h2>{df_to_html(tables['climbers'])}")
    parts.append(f"<h2>Top fallers (10/gender)</h2>{df_to_html(tables['fallers'])}")
    parts.append(f"<h2>New entries in the top {TOP_N_DISPLAY}</h2>{df_to_html(tables['new_entries'])}")
    parts.append(f"<h2>Exits from the top {TOP_N_DISPLAY}</h2>{df_to_html(tables['exits'])}")
    parts.append(f"<h2>National top 3</h2>{df_to_html(tables['rankings_national'])}")
    parts.append(f"<h2>Regional top 3</h2>{df_to_html(tables['rankings_regional'])}")
    parts.append(f"<h2>Most common names (population)</h2>{df_to_html(tables['most_common'])}")
    parts.append("</div>")

    body = "\n".join(parts)
    return f"<!doctype html><html><head><meta charset='utf-8'><title>Baby names report {DATA_YEAR}</title><style>{ARTICLE_PAGE_CSS}</style></head><body>{body}</body></html>"


# =============================================================================
# MAIN PIPELINE
# =============================================================================

def main():
    px_female, px_male = load_px_data()
    rankings = build_rankings(px_female, px_male)
    detect_years(rankings)

    data = load_population_data()

    quality_log = run_data_quality_checks(px_female, px_male, data)
    write_data_quality_log(quality_log)

    # --- national & regional top 3 --------------------------------------
    national_male = top_n(rankings, NATIONAL_REGION, "male", DATA_YEAR, 3)
    national_female = top_n(rankings, NATIONAL_REGION, "female", DATA_YEAR, 3)

    regional_rows = []
    regional_top1 = {}
    for key, region_label in LINGUISTIC_REGIONS.items():
        for gender in ("male", "female"):
            top3 = top_n(rankings, region_label, gender, DATA_YEAR, 3)
            top3.insert(0, "region", region_label)
            top3.insert(1, "gender", gender)
            regional_rows.append(top3)
            regional_top1[(key, gender)] = top3.iloc[0]["prenom"] if len(top3) else None
    rankings_regional = pd.concat(regional_rows, ignore_index=True)

    rankings_national = pd.concat(
        [national_male.assign(gender="male"), national_female.assign(gender="female")],
        ignore_index=True,
    )[["gender", "prenom", "value", "custom_rank"]]

    # --- leader continuity narrative ("years as #1", continuing/returning/first time) ---
    national_male_1_count = int(national_male["value"].iloc[0])
    national_female_1_count = int(national_female["value"].iloc[0])

    national_male_prev3 = top_n(rankings, NATIONAL_REGION, "male", COMPARISON_YEAR, 3)
    national_female_prev3 = top_n(rankings, NATIONAL_REGION, "female", COMPARISON_YEAR, 3)
    male_podium_unchanged = (
        len(national_male_prev3) >= 3
        and set(national_male_prev3["prenom"].iloc[1:3]) == set(national_male["prenom"].iloc[1:3])
    )
    female_podium_unchanged = (
        len(national_female_prev3) >= 3
        and set(national_female_prev3["prenom"].iloc[1:3]) == set(national_female["prenom"].iloc[1:3])
    )

    male_history = leader_history(rankings, "male", national_male["prenom"].iloc[0], DATA_YEAR)
    female_history = leader_history(rankings, "female", national_female["prenom"].iloc[0], DATA_YEAR)

    # Bold markers (**name**) around every name mention - see markdown_bold_to_html().
    def b(name) -> str:
        return f"**{name}**"

    male1_b, male2_b, male3_b = (b(national_male["prenom"].iloc[i]) for i in range(3))
    female1_b, female2_b, female3_b = (b(national_female["prenom"].iloc[i]) for i in range(3))

    male_leader_text_fr = leader_paragraph_fr(
        male1_b, male_history, male2_b, male3_b, DATA_YEAR, male_podium_unchanged,
    )
    female_leader_text_fr = leader_paragraph_fr(
        female1_b, female_history, female2_b, female3_b, DATA_YEAR, female_podium_unchanged,
        lead_sentence=f"Chez les filles, {female1_b} prend la tête du classement, devant {female2_b} et {female3_b}.",
    )
    male_leader_text_de = leader_paragraph_de(
        male1_b, "male", male_history, male2_b, male3_b, DATA_YEAR, male_podium_unchanged,
    )
    female_leader_text_de = leader_paragraph_de(
        female1_b, "female", female_history, female2_b, female3_b, DATA_YEAR, female_podium_unchanged,
        lead_sentence=f"Bei den Mädchen ist {female1_b} neu an erster Stelle, gefolgt von {female2_b} und {female3_b}.",
    )

    # --- national top-10-over-10-years evolution --------------------------
    evolution_male = national_evolution_table(rankings, "male")
    evolution_female = national_evolution_table(rankings, "female")

    # --- year-over-year rank changes --------------------------------------
    changes = build_rank_changes(rankings)
    climbers, fallers = climbers_and_fallers(changes, n=10)
    new_entries, exits = new_entries_and_exits(changes)

    for label, df in [("climbers", climbers), ("fallers", fallers), ("new_entries", new_entries)]:
        for gender in ("male", "female"):
            if df[df["gender"] == gender].empty:
                raise RuntimeError(
                    f"No {gender} names found for '{label}' this year - the article text "
                    f"generation assumes at least one per gender. This would need a manual "
                    f"look at the {DATA_YEAR} vs {COMPARISON_YEAR} data before the script can "
                    f"produce a paragraph for that section."
                )

    male_climber = climbers[climbers["gender"] == "male"].head(1)
    female_climber = climbers[climbers["gender"] == "female"].head(1)
    male_faller = fallers[fallers["gender"] == "male"].head(1)
    female_faller = fallers[fallers["gender"] == "female"].head(1)

    # Split by gender for the article's own tables (TOP_N_MOVERS_IN_ARTICLE per gender).
    climbers_male = climbers[climbers["gender"] == "male"].head(TOP_N_MOVERS_IN_ARTICLE)
    climbers_female = climbers[climbers["gender"] == "female"].head(TOP_N_MOVERS_IN_ARTICLE)
    fallers_male = fallers[fallers["gender"] == "male"].head(TOP_N_MOVERS_IN_ARTICLE)
    fallers_female = fallers[fallers["gender"] == "female"].head(TOP_N_MOVERS_IN_ARTICLE)

    new_entries_male_top = new_entries[new_entries["gender"] == "male"].head(TOP_N_NEW_ENTRIES_IN_ARTICLE)
    new_entries_female_top = new_entries[new_entries["gender"] == "female"].head(TOP_N_NEW_ENTRIES_IN_ARTICLE)

    # Full year-over-year rank changes for every name in the analysis pool (not just the top movers).
    rank_changes_full = changes[
        ["prenom", "gender", "rank_prev", "rank_current", "rank_change", "count_prev", "count_current", "count_change", "status"]
    ].sort_values(["gender", "rank_current"]).reset_index(drop=True)

    new_entries_sample = (
        new_entries[new_entries["gender"] == "male"]["prenom"].head(2).tolist()
        + new_entries[new_entries["gender"] == "female"]["prenom"].head(2).tolist()
    )

    male_new_entry = new_entries[new_entries["gender"] == "male"].head(1)
    female_new_entry = new_entries[new_entries["gender"] == "female"].head(1)

    def row0(df, col):
        return df[col].iloc[0] if len(df) else None

    female_faller_loss = int(row0(female_faller, "rank_current")) - int(row0(female_faller, "rank_prev"))
    male_faller_loss = int(row0(male_faller, "rank_current")) - int(row0(male_faller, "rank_prev"))
    fallers_min_loss = min(female_faller_loss, male_faller_loss)
    # If the smaller loss is itself under 10, rounding down to the nearest
    # ten would give 0 - "plus de 0 rangs" is nonsense, so fall back to the
    # exact number in that (rare, small-swing) case.
    fallers_min_loss_rounded = round_down_to_ten(fallers_min_loss) or fallers_min_loss

    # --- total population analysis ----------------------------------------
    all_current = pd.concat([data["all_female_current"], data["all_male_current"]], ignore_index=True)
    all_previous = pd.concat([data["all_female_previous"], data["all_male_previous"]], ignore_index=True)
    population_changes = build_population_changes(all_current, all_previous)

    male_most_common = most_common_names(population_changes, "male", n=20)
    female_most_common = most_common_names(population_changes, "female", n=20)
    increases, decreases = biggest_population_changes(population_changes, n=10)

    # --- name length ---------------------------------------------------
    length = name_length_summary(data["all_female_current"].pipe(
        lambda df: pd.concat([df, data["all_male_current"]], ignore_index=True)
    ))

    # --- assemble article context ---------------------------------------
    ctx = {
        "data_year": DATA_YEAR,
        "comparison_year": COMPARISON_YEAR,
        "national_male_1": male1_b,
        "national_male_1_count": national_male_1_count,
        "national_female_1": female1_b,
        "national_female_1_count": national_female_1_count,
        "male_leader_text_fr": male_leader_text_fr,
        "female_leader_text_fr": female_leader_text_fr,
        "male_leader_text_de": male_leader_text_de,
        "female_leader_text_de": female_leader_text_de,
        "alemanique_male_1": b(regional_top1[("alemanique", "male")]),
        "alemanique_female_1": b(regional_top1[("alemanique", "female")]),
        "romande_male_1": b(regional_top1[("romande", "male")]),
        "romande_female_1": b(regional_top1[("romande", "female")]),
        "italienne_male_1": b(regional_top1[("italienne", "male")]),
        "italienne_female_1": b(regional_top1[("italienne", "female")]),
        "male_climber": b(row0(male_climber, "prenom")),
        "male_climber_rank_prev": int(row0(male_climber, "rank_prev")),
        "male_climber_rank_curr": int(row0(male_climber, "rank_current")),
        "female_climber": b(row0(female_climber, "prenom")),
        "female_climber_rank_prev": int(row0(female_climber, "rank_prev")),
        "female_climber_rank_curr": int(row0(female_climber, "rank_current")),
        "male_faller": b(row0(male_faller, "prenom")),
        "male_faller_rank_prev": int(row0(male_faller, "rank_prev")),
        "male_faller_rank_curr": int(row0(male_faller, "rank_current")),
        "female_faller": b(row0(female_faller, "prenom")),
        "female_faller_rank_prev": int(row0(female_faller, "rank_prev")),
        "female_faller_rank_curr": int(row0(female_faller, "rank_current")),
        "fallers_min_loss_rounded": fallers_min_loss_rounded,
        "fallers_min_loss_word_de": number_word_de(fallers_min_loss_rounded),
        "male_new_entry": b(row0(male_new_entry, "prenom")),
        "male_new_entry_rank": int(row0(male_new_entry, "rank_current")),
        "female_new_entry": b(row0(female_new_entry, "prenom")),
        "female_new_entry_rank": int(row0(female_new_entry, "rank_current")),
        "new_entries_text": ", ".join(b(n) for n in new_entries_sample),
        "most_common_male_1": b(male_most_common["firstname"].iloc[0]),
        "most_common_male_1_rounded": round_nearest(male_most_common["total_population_current"].iloc[0]),
        "most_common_male_2": b(male_most_common["firstname"].iloc[1]),
        "most_common_male_2_rounded": round_nearest(male_most_common["total_population_current"].iloc[1]),
        "most_common_male_3": b(male_most_common["firstname"].iloc[2]),
        "most_common_male_3_rounded": round_nearest(male_most_common["total_population_current"].iloc[2]),
        "most_common_female_1": b(female_most_common["firstname"].iloc[0]),
        "most_common_female_1_rounded": round_nearest(female_most_common["total_population_current"].iloc[0]),
        "most_common_female_2": b(female_most_common["firstname"].iloc[1]),
        "most_common_female_2_rounded": round_nearest(female_most_common["total_population_current"].iloc[1]),
        "most_common_female_3": b(female_most_common["firstname"].iloc[2]),
        "most_common_female_3_rounded": round_nearest(female_most_common["total_population_current"].iloc[2]),
        "avg_length_overall_fmt": decimal_comma(length["unweighted"]["overall"], 1),
        "avg_length_male_fmt": decimal_comma(length["unweighted"]["male"], 2),
        "avg_length_female_fmt": decimal_comma(length["unweighted"]["female"], 2),
    }

    article_tables = {
        "evolution_male": evolution_male,
        "evolution_female": evolution_female,
        "climbers_male": climbers_male,
        "climbers_female": climbers_female,
        "fallers_male": fallers_male,
        "fallers_female": fallers_female,
        "new_entries_male": new_entries_male_top,
        "new_entries_female": new_entries_female_top,
        "regional": rankings_regional,
        "most_common_male": male_most_common.head(3),
        "most_common_female": female_most_common.head(3),
    }
    article_sections = build_article_sections(ctx, article_tables)

    # =============================================================================
    # EXPORT
    # =============================================================================

    print(f"\n=== Writing output to {OUTPUT_DIR} ===\n")

    LANG_LABELS = {"fr": "Français", "de": "Deutsch"}
    for lang, lang_label in LANG_LABELS.items():
        path = OUTPUT_DIR / f"article_{lang}_{DATA_YEAR}.html"
        path.write_text(build_article_page_html(lang_label, article_sections[lang]), encoding="utf-8")
        print(f"  wrote {path.name}")

    tables = {
        "climbers": climbers,
        "fallers": fallers,
        "new_entries": new_entries,
        "exits": exits,
        "rankings_national": rankings_national,
        "rankings_regional": rankings_regional,
        "most_common": pd.concat(
            [male_most_common.assign(gender="male"), female_most_common.assign(gender="female")],
            ignore_index=True,
        ),
        # Every name in the analysis pool, not just the top movers - for further
        # analysis or for the graphics team to build their own visuals from.
        "rank_changes_full": rank_changes_full,
        # Full 10-year rank history of today's top-10 names (wide: one column per year).
        "evolution_male": evolution_male.reset_index(),
        "evolution_female": evolution_female.reset_index(),
    }
    for name, df in tables.items():
        path = OUTPUT_DIR / f"{name}_{DATA_YEAR}.csv"
        df.to_csv(path, index=False)
        print(f"  wrote {path.name}")

    for name, df in {"biggest_population_increases": increases, "biggest_population_decreases": decreases}.items():
        path = OUTPUT_DIR / f"{name}_{DATA_YEAR}.csv"
        df.to_csv(path, index=False)
        print(f"  wrote {path.name}")

    report_html = build_report_html(article_sections, tables)
    report_path = OUTPUT_DIR / f"report_{DATA_YEAR}.html"
    report_path.write_text(report_html, encoding="utf-8")
    print(f"  wrote {report_path.name}  <- open this one first")

    print("\nDone.\n")
    return ctx, tables, article_sections


if __name__ == "__main__":
    main()
