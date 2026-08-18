"""
Swiss First Names Analysis - Automated Annual Report
Author: Olaf Koenig

Downloads newborn-name rankings (PX Web, automatic) and total-population
name counts (BFS assets, manual URLs) for Switzerland, computes rankings,
year-over-year changes and a few key stats, then generates ready-to-paste
article text in FR/DE/EN plus a handful of CSV/HTML tables.

Requirements: Python 3.8+, pandas, requests (see requirements.txt).
Run with:     python babynames_script.py
"""

import json
import re
import unicodedata
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

# How many ranks to look at when computing climbers/fallers/new entries.
TOP_N_ANALYSIS = 200   # pool used to compute rank changes (climbers/fallers)
TOP_N_DISPLAY = 100    # threshold used for "new entry" / "exit" from the top
TOP_N_MOVERS_IN_ARTICLE = 5  # climbers/fallers shown per gender in the article's own tables

# Re-download files even if they already exist locally for this run.
# Set to False while iterating locally to avoid hammering the BFS servers.
FORCE_REDOWNLOAD = True

# Linguistic regions as labelled by the BFS data (kept in French - it's the
# raw data value, not display text).
NATIONAL_REGION = "Suisse"
LINGUISTIC_REGIONS = {
    "alemanique": "Suisse alémanique",
    "romande": "Suisse romande",
    "italienne": "Suisse italienne",
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

# PX Web (automatic - full historical newborn-name series, stable endpoint,
# never needs updating).
URLS = {
    "px_female": "https://www.pxweb.bfs.admin.ch/sq/ea826a26-7e80-4906-a4f2-d89a145cd19e",
    "px_male": "https://www.pxweb.bfs.admin.ch/sq/7a95b4e9-f6d8-49cc-858d-cea658ac3a2b",
}

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


def import_px_data(url: str, filename_base: str) -> pd.DataFrame:
    """PX Web export: comma-separated, Windows-1252 encoded. Always the full
    historical series, so there's no year to pin the filename to."""
    dest_path = INPUT_RAW_DIR / f"{filename_base}.csv"
    print(f"Downloading {dest_path.name} ...")
    download(url, dest_path)
    df = pd.read_csv(dest_path, encoding="cp1252")
    df = clean_names(df)
    print(f"  imported {len(df):,} rows")
    return df


def import_population_data(url: str, filename_base: str, year: int, gender: str) -> pd.DataFrame:
    """BFS asset CSV: comma-separated, UTF-8 (with BOM)."""
    dest_path = INPUT_RAW_DIR / f"{filename_base}_{year}.csv"
    print(f"Downloading {dest_path.name} ...")
    download(url, dest_path)
    df = pd.read_csv(dest_path, encoding="utf-8-sig")
    df = clean_names(df)
    df["gender"] = gender
    df = df.drop(columns=["obs_status"], errors="ignore")
    print(f"  imported {len(df):,} rows")
    return df


def check_encoding(df: pd.DataFrame, column: str) -> pd.Series:
    """Names containing anything other than letters/hyphens - quick data-quality check."""
    values = df[column].dropna().astype(str)
    suspicious = values[values.str.contains(r"[^A-Za-zÀ-ÿ\-]", regex=True)]
    return pd.Series(sorted(suspicious.unique()))


# =============================================================================
# DATA IMPORT
# =============================================================================

def load_px_data():
    print("\n=== Importing newborn-name data (PX Web) ===\n")
    px_female = import_px_data(URLS["px_female"], "px_names_female")
    px_male = import_px_data(URLS["px_male"], "px_names_male")

    print("\n--- Encoding sanity check (names with unexpected characters) ---")
    for label, df in [("PX female", px_female), ("PX male", px_male)]:
        suspicious = check_encoding(df, "prenom")
        if len(suspicious):
            print(f"  {label}: {list(suspicious)}")
    print("  done (no output above = nothing suspicious)\n")

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
        if int(current["period"]) != DATA_YEAR:
            print(
                f"  NOTE: the {gender} population snapshot is for {current['period']}, while the "
                f"newborn data points to {DATA_YEAR} as the reference year (BFS publishes these on "
                f"different schedules). Population figures in the article will refer to {current['period']}."
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

    print("--- Encoding sanity check (names with unexpected characters) ---")
    for label, df in [("Population female", frames["all_female_current"]), ("Population male", frames["all_male_current"])]:
        suspicious = check_encoding(df, "firstname")
        if len(suspicious):
            print(f"  {label}: {list(suspicious)}")
    print("  done (no output above = nothing suspicious)\n")

    return frames


# =============================================================================
# RANKINGS (newborns of DATA_YEAR, with historical series for context)
# =============================================================================

def build_rankings(px_female: pd.DataFrame, px_male: pd.DataFrame) -> pd.DataFrame:
    """Wide (one column per year) -> long, with a tie-free custom rank per
    region/year/gender (ties broken alphabetically, like the original script)."""

    def to_long(df, gender):
        year_cols = [c for c in df.columns if re.fullmatch(r"\d{4}", c)]
        long_df = df.melt(
            id_vars=["prenom", "region_linguistique_canton", "unite_de_mesure"],
            value_vars=year_cols,
            var_name="year",
            value_name="value",
        )
        long_df["year"] = long_df["year"].astype(int)
        long_df["gender"] = gender
        return long_df.dropna(subset=["value"])

    combined = pd.concat([to_long(px_female, "female"), to_long(px_male, "male")], ignore_index=True)
    combined = combined[combined["unite_de_mesure"] == "Nombre"].copy()

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


# =============================================================================
# YEAR-OVER-YEAR RANK CHANGES (climbers / fallers / new entries / exits)
# =============================================================================

def build_rank_changes(rankings: pd.DataFrame) -> pd.DataFrame:
    prev = rankings[
        (rankings["region_linguistique_canton"] == NATIONAL_REGION)
        & (rankings["year"] == COMPARISON_YEAR)
        & (rankings["custom_rank"] <= TOP_N_ANALYSIS)
    ][["prenom", "gender", "value", "custom_rank"]].rename(
        columns={"value": "count_prev", "custom_rank": "rank_prev"}
    )

    curr = rankings[
        (rankings["region_linguistique_canton"] == NATIONAL_REGION)
        & (rankings["year"] == DATA_YEAR)
        & (rankings["custom_rank"] <= TOP_N_ANALYSIS)
    ][["prenom", "gender", "value", "custom_rank"]].rename(
        columns={"value": "count_current", "custom_rank": "rank_current"}
    )

    changes = prev.merge(curr, on=["prenom", "gender"], how="outer")
    changes["rank_prev"] = changes["rank_prev"].fillna(TOP_N_ANALYSIS + 1).astype(int)
    changes["rank_current"] = changes["rank_current"].fillna(TOP_N_ANALYSIS + 1).astype(int)
    changes["rank_change"] = changes["rank_prev"] - changes["rank_current"]  # positive = climbed
    changes["count_change"] = changes["count_current"] - changes["count_prev"]

    changes["status"] = "Continued"
    changes.loc[changes["count_prev"].isna() & changes["count_current"].notna(), "status"] = "New Entry"
    changes.loc[changes["count_prev"].notna() & changes["count_current"].isna(), "status"] = "Dropped Out"

    return changes


def climbers_and_fallers(changes: pd.DataFrame, n: int = 10):
    """Top n climbers/fallers PER GENDER (not n total split however it falls)."""
    continued = changes[changes["status"] == "Continued"]
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
    new_entries = changes[
        (changes["rank_current"] <= TOP_N_DISPLAY) & (changes["rank_prev"] > TOP_N_DISPLAY)
    ].sort_values("rank_current")[["prenom", "gender", "rank_current", "count_current"]]

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
            f"snapshot may not cover births from {DATA_YEAR} yet."
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


# =============================================================================
# TEXT GENERATION
# =============================================================================

def fmt_ch(n) -> str:
    """Swiss-style thousands separator: 12'345."""
    return f"{int(round(n)):,}".replace(",", "'")


def fmt_en(n) -> str:
    return f"{int(round(n)):,}"


# --- concise "top movers" tables embedded right in the article text ---------

GENDER_LABELS = {
    "fr": {"male": "Garçon", "female": "Fille"},
    "de": {"male": "Junge", "female": "Mädchen"},
    "en": {"male": "Boy", "female": "Girl"},
}
MOVERS_TABLE_HEADERS = {
    "fr": ["Prénom", "Sexe", "Rang {prev}", "Rang {curr}", "Évolution"],
    "de": ["Name", "Geschlecht", "Rang {prev}", "Rang {curr}", "Veränderung"],
    "en": ["Name", "Gender", "Rank {prev}", "Rank {curr}", "Change"],
}
_TH_STYLE = "text-align:left;border-bottom:2px solid #ccc;padding:4px 8px;"
_TD_STYLE = "padding:4px 8px;border-bottom:1px solid #eee;"


def movers_table_html(df: pd.DataFrame, lang: str, comparison_year: int, data_year: int) -> str:
    headers = [h.format(prev=comparison_year, curr=data_year) for h in MOVERS_TABLE_HEADERS[lang]]
    head_html = "".join(f"<th style='{_TH_STYLE}'>{h}</th>" for h in headers)

    rows_html = []
    for _, row in df.iterrows():
        change = int(row["rank_change"])
        change_str = f"+{change}" if change > 0 else str(change)
        cells = [
            row["prenom"],
            GENDER_LABELS[lang][row["gender"]],
            int(row["rank_prev"]),
            int(row["rank_current"]),
            change_str,
        ]
        rows_html.append("<tr>" + "".join(f"<td style='{_TD_STYLE}'>{c}</td>" for c in cells) + "</tr>")

    return (
        "<table style='border-collapse:collapse;width:100%;margin:.5rem 0 1.25rem;font-size:.92rem;'>"
        f"<thead><tr>{head_html}</tr></thead><tbody>{''.join(rows_html)}</tbody></table>"
    )


def build_article_texts(ctx: dict, climbers_top: pd.DataFrame, fallers_top: pd.DataFrame) -> dict:
    year, prev_year = ctx["data_year"], ctx["comparison_year"]

    fr = f"""
<p><strong>Quels étaient les prénoms les plus populaires en {year} ? Et comment les tendances ont-elles évolué au fil des décennies ? Découvrez-le grâce à notre outil interactif.</strong></p>

<h2>Les prénoms vedettes de {year}</h2>

<p>Voici les stars de {year} : <strong>{ctx['national_female_1']}</strong> et <strong>{ctx['national_male_1']}</strong> sont les prénoms qui ont été donnés le plus souvent aux nouveau-nés en Suisse l'an passé.</p>

<p><strong>{ctx['national_male_1']}</strong> garde la première place, comme lors d'années précédentes où il dominait les classements. Le top 3 est resté stable : <strong>{ctx['national_male_1']}</strong> est encore suivi par <strong>{ctx['national_male_2']}</strong> et par <strong>{ctx['national_male_3']}</strong> au niveau national.</p>

<p>Chez les filles, <strong>{ctx['national_female_1']}</strong> reprend la première place, suivie par <strong>{ctx['national_female_2']}</strong> et par <strong>{ctx['national_female_3']}</strong>. Cela représente les tendances de préférences actuelles pour les prénoms féminins.</p>

<h2>Qui progresse, qui recule ?</h2>

<p>Quels sont les prénoms qui grimpent le plus ? Il y a <strong>{ctx['female_climber']}</strong> chez les filles et <strong>{ctx['male_climber']}</strong> chez les garçons. Ce sont les prénoms qui ont gagné le plus de places dans le classement. Ils ont progressé respectivement de {ctx['female_climber_gain']} et {ctx['male_climber_gain']} places entre {prev_year} et {year}.</p>

{movers_table_html(climbers_top, "fr", prev_year, year)}

<p>Parmi les prénoms qui ont dégringolé, il y a <strong>{ctx['female_faller']}</strong> chez les filles et <strong>{ctx['male_faller']}</strong> chez les garçons. Ils ont respectivement perdu plus de {ctx['female_faller_loss']} et {ctx['male_faller_loss']} places.</p>

{movers_table_html(fallers_top, "fr", prev_year, year)}

<h2>Les petits nouveaux</h2>

<p>Parmi les nouveaux venus dans le top 100 des prénoms les plus populaires pour les nouveaux-nés, on trouve désormais {ctx['new_entries_text']}.</p>

<h2>Des préférences différentes selon les régions</h2>

<p>Les prénoms les plus populaires en {year} ne sont pas les mêmes partout : en Suisse alémanique, <strong>{ctx['alemanique_male_1']}</strong> et <strong>{ctx['alemanique_female_1']}</strong> occupent la première place, mais en Suisse romande, ce sont <strong>{ctx['romande_male_1']}</strong> et <strong>{ctx['romande_female_1']}</strong> qui sont en tête du classement. En Suisse italienne, ce sont <strong>{ctx['italienne_male_1']}</strong> et <strong>{ctx['italienne_female_1']}</strong> qui occupent la première place du podium.</p>

<h2>Les prénoms les plus portés en Suisse</h2>

<p>Même si le prénom <strong>{ctx['most_common_female']}</strong> est en perte de vitesse, il reste le prénom féminin le plus répandu dans l'ensemble de la population, avec près de {fmt_ch(ctx['most_common_female_count'])} femmes portant ce prénom, contre {fmt_ch(ctx['most_common_female_count_prev'])} l'année précédente.</p>

<p>Chez les hommes, <strong>{ctx['most_common_male']}</strong> est toujours le prénom le plus fréquent en Suisse. Près de {fmt_ch(ctx['most_common_male_count'])} hommes et garçons s'appellent ainsi, contre {fmt_ch(ctx['most_common_male_count_prev'])} en {prev_year}.</p>

<h2>Des prénoms de plus en plus courts ?</h2>

<p>Avec <strong>{ctx['national_female_1']}</strong> et <strong>{ctx['national_male_1']}</strong>, ce sont à nouveau deux prénoms courts qui ont été attribués le plus souvent en {year}. La longueur moyenne des prénoms donnés en {year} est de {ctx['avg_length_weighted_overall']} caractères si l'on tient compte de la fréquence de chaque prénom (soit la longueur vécue en moyenne par un nouveau-né), ou de {ctx['avg_length_unweighted_overall']} caractères si l'on fait la moyenne simple de tous les prénoms différents donnés cette année-là, sans tenir compte de leur popularité. Chez les femmes, la longueur pondérée par la fréquence est légèrement plus importante ({ctx['avg_length_weighted_female']} caractères) que chez les hommes ({ctx['avg_length_weighted_male']}).</p>
""".strip()

    de = f"""
<p><strong>Welches waren die beliebtesten Vornamen im Jahr {year}? Und wie haben sich die Trends über die Jahrzehnte entwickelt? Entdecken Sie es mit unserem interaktiven Tool.</strong></p>

<h2>Die Namen des Jahres {year}</h2>

<p>Hier sind die Stars von {year}: <strong>{ctx['national_female_1']}</strong> und <strong>{ctx['national_male_1']}</strong> sind die Namen, die Neugeborenen in der Schweiz im vergangenen Jahr am häufigsten gegeben wurden.</p>

<p><strong>{ctx['national_male_1']}</strong> behält den ersten Platz, wie in früheren Jahren, in denen er die Rankings dominierte. Die Top 3 blieben stabil: <strong>{ctx['national_male_1']}</strong> wird noch immer von <strong>{ctx['national_male_2']}</strong> und <strong>{ctx['national_male_3']}</strong> auf nationaler Ebene gefolgt.</p>

<p>Bei den Mädchen nimmt <strong>{ctx['national_female_1']}</strong> den ersten Platz ein, gefolgt von <strong>{ctx['national_female_2']}</strong> und <strong>{ctx['national_female_3']}</strong>. Dies repräsentiert die aktuellen Präferenztrends für weibliche Vornamen.</p>

<h2>Wer steigt, wer fällt?</h2>

<p>Welche Namen steigen am meisten? Da sind <strong>{ctx['female_climber']}</strong> bei den Mädchen und <strong>{ctx['male_climber']}</strong> bei den Jungen. Das sind die Namen, die die meisten Plätze im Ranking gewonnen haben. Sie sind zwischen {prev_year} und {year} um {ctx['female_climber_gain']} bzw. {ctx['male_climber_gain']} Plätze gestiegen.</p>

{movers_table_html(climbers_top, "de", prev_year, year)}

<p>Unter den Namen, die gefallen sind, gibt es <strong>{ctx['female_faller']}</strong> bei den Mädchen und <strong>{ctx['male_faller']}</strong> bei den Jungen. Sie haben jeweils mehr als {ctx['female_faller_loss']} bzw. {ctx['male_faller_loss']} Plätze verloren.</p>

{movers_table_html(fallers_top, "de", prev_year, year)}

<h2>Die Neuankömmlinge</h2>

<p>Unter den Neueinsteigern in die Top 100 der beliebtesten Namen für Neugeborene finden wir nun {ctx['new_entries_text']}.</p>

<h2>Regionale Unterschiede</h2>

<p>Die beliebtesten Namen in {year} sind nicht überall gleich: In der Deutschschweiz belegen <strong>{ctx['alemanique_male_1']}</strong> und <strong>{ctx['alemanique_female_1']}</strong> den ersten Platz, aber in der Romandie stehen <strong>{ctx['romande_male_1']}</strong> und <strong>{ctx['romande_female_1']}</strong> an der Spitze der Rangliste. In der italienischsprachigen Schweiz belegen <strong>{ctx['italienne_male_1']}</strong> und <strong>{ctx['italienne_female_1']}</strong> den ersten Platz auf dem Podium.</p>

<h2>Die häufigsten Vornamen der Schweiz</h2>

<p>Auch wenn der Name <strong>{ctx['most_common_female']}</strong> an Boden verliert, bleibt er der am weitesten verbreitete weibliche Vorname in der Gesamtbevölkerung, mit fast {fmt_ch(ctx['most_common_female_count'])} Frauen, die diesen Namen tragen, gegenüber {fmt_ch(ctx['most_common_female_count_prev'])} im Vorjahr.</p>

<p>Bei den Männern ist <strong>{ctx['most_common_male']}</strong> immer noch der häufigste Name in der Schweiz. Fast {fmt_ch(ctx['most_common_male_count'])} Männer und Jungen heissen so, gegenüber {fmt_ch(ctx['most_common_male_count_prev'])} in {prev_year}.</p>

<h2>Werden die Vornamen kürzer?</h2>

<p>Mit <strong>{ctx['national_female_1']}</strong> und <strong>{ctx['national_male_1']}</strong> wurden im Jahr {year} erneut zwei kurze Namen am häufigsten vergeben. Die durchschnittliche Vornamenslänge der im Jahr {year} Geborenen liegt bei {ctx['avg_length_weighted_overall']} Zeichen, wenn man die Häufigkeit jedes Namens berücksichtigt (also die Länge, die ein durchschnittliches Neugeborenes tatsächlich trägt), oder bei {ctx['avg_length_unweighted_overall']} Zeichen, wenn man einfach den Durchschnitt über alle im Jahr vergebenen unterschiedlichen Namen bildet, unabhängig von ihrer Häufigkeit. Bei Frauen ist die (häufigkeitsgewichtete) Länge mit {ctx['avg_length_weighted_female']} Zeichen etwas umfangreicher als bei den Männern mit {ctx['avg_length_weighted_male']}.</p>
""".strip()

    en = f"""
<p><strong>What were the most popular first names in {year}? And how have trends evolved over the decades? Discover it with our interactive tool.</strong></p>

<h2>The names of {year}</h2>

<p>Here are the stars of {year}: <strong>{ctx['national_female_1']}</strong> and <strong>{ctx['national_male_1']}</strong> are the names that were most often given to newborns in Switzerland last year.</p>

<p><strong>{ctx['national_male_1']}</strong> maintains first place, as in previous years when it dominated the rankings. The top 3 has remained stable: <strong>{ctx['national_male_1']}</strong> is still followed by <strong>{ctx['national_male_2']}</strong> and <strong>{ctx['national_male_3']}</strong> at the national level.</p>

<p>Among girls, <strong>{ctx['national_female_1']}</strong> takes first place, followed by <strong>{ctx['national_female_2']}</strong> and <strong>{ctx['national_female_3']}</strong>. This represents the current preference trends for female names.</p>

<h2>Who's climbing, who's falling?</h2>

<p>What are the names that are climbing the most? There are <strong>{ctx['female_climber']}</strong> among girls and <strong>{ctx['male_climber']}</strong> among boys. These are the names that have gained the most places in the rankings. They progressed by {ctx['female_climber_gain']} and {ctx['male_climber_gain']} places respectively between {prev_year} and {year}.</p>

{movers_table_html(climbers_top, "en", prev_year, year)}

<p>Among the names that have fallen, there are <strong>{ctx['female_faller']}</strong> among girls and <strong>{ctx['male_faller']}</strong> among boys. They lost more than {ctx['female_faller_loss']} and {ctx['male_faller_loss']} places respectively.</p>

{movers_table_html(fallers_top, "en", prev_year, year)}

<h2>The newcomers</h2>

<p>Among the newcomers in the top 100 of the most popular names for newborns, we now find {ctx['new_entries_text']}.</p>

<h2>Regional differences</h2>

<p>The most popular names in {year} are not the same everywhere: in German-speaking Switzerland, <strong>{ctx['alemanique_male_1']}</strong> and <strong>{ctx['alemanique_female_1']}</strong> occupy first place, but in French-speaking Switzerland, <strong>{ctx['romande_male_1']}</strong> and <strong>{ctx['romande_female_1']}</strong> are at the top of the rankings. In Italian-speaking Switzerland, <strong>{ctx['italienne_male_1']}</strong> and <strong>{ctx['italienne_female_1']}</strong> occupy first place on the podium.</p>

<h2>The most common names in Switzerland</h2>

<p>Even if the name <strong>{ctx['most_common_female']}</strong> is losing ground, it remains the most widespread female name in the entire population, with nearly {fmt_en(ctx['most_common_female_count'])} women bearing this name, compared to {fmt_en(ctx['most_common_female_count_prev'])} the previous year.</p>

<p>Among men, <strong>{ctx['most_common_male']}</strong> is still the most frequent name in Switzerland. Nearly {fmt_en(ctx['most_common_male_count'])} men and boys are called this, compared to {fmt_en(ctx['most_common_male_count_prev'])} in {prev_year}.</p>

<h2>Are names getting shorter?</h2>

<p>With <strong>{ctx['national_female_1']}</strong> and <strong>{ctx['national_male_1']}</strong>, these are once again two short names that were most often given in {year}. The average length of first names given in {year} is {ctx['avg_length_weighted_overall']} characters when weighted by how often each name was used (i.e. the length actually experienced by an average newborn), or {ctx['avg_length_unweighted_overall']} characters as a simple average across every distinct name given that year, regardless of popularity. Among women, the frequency-weighted length is slightly longer ({ctx['avg_length_weighted_female']} characters) than among men ({ctx['avg_length_weighted_male']}).</p>
""".strip()

    return {"fr": fr, "de": de, "en": en}


# =============================================================================
# HTML REPORT (texts + tables in one page, for quick review / copy-paste)
# =============================================================================

REPORT_CSS = """
body { font-family: -apple-system, Arial, sans-serif; max-width: 900px; margin: 2rem auto; padding: 0 1rem; line-height: 1.5; color: #1a1a1a; }
h1 { font-size: 1.6rem; } h2 { font-size: 1.25rem; margin-top: 2.5rem; border-bottom: 2px solid #ddd; padding-bottom: .3rem; }
h3 { font-size: 1.05rem; margin-top: 1.5rem; color: #444; }
table { border-collapse: collapse; width: 100%; margin: .75rem 0 1.5rem; font-size: .9rem; }
th, td { border: 1px solid #ddd; padding: .4rem .6rem; text-align: left; }
th { background: #f5f5f5; }
tr:nth-child(even) { background: #fafafa; }
.article-block { background: #f8f8f8; border: 1px solid #ddd; border-radius: 6px; padding: 1rem 1.25rem; margin: .75rem 0 1.5rem; }
"""


def df_to_html(df: pd.DataFrame) -> str:
    return df.to_html(index=False, border=0, na_rep="-")


def build_report_html(articles: dict, tables: dict) -> str:
    sections = [f"<h1>Swiss First Names Analysis - {DATA_YEAR}</h1>"]

    sections.append("<h2>Automated article text</h2>")
    for lang, label in [("fr", "Français"), ("de", "Deutsch"), ("en", "English")]:
        sections.append(f"<h3>{label}</h3><div class='article-block'>{articles[lang]}</div>")

    sections.append("<h2>Top climbers / fallers (national ranking, births)</h2>")
    sections.append(f"<h3>Top climbers</h3>{df_to_html(tables['climbers'])}")
    sections.append(f"<h3>Top fallers</h3>{df_to_html(tables['fallers'])}")
    sections.append(f"<h3>New entries in the top {TOP_N_DISPLAY}</h3>{df_to_html(tables['new_entries'])}")
    sections.append(f"<h3>Exits from the top {TOP_N_DISPLAY}</h3>{df_to_html(tables['exits'])}")

    sections.append("<h2>National &amp; regional top 3 (births)</h2>")
    sections.append(f"<h3>National</h3>{df_to_html(tables['rankings_national'])}")
    sections.append(f"<h3>Regional</h3>{df_to_html(tables['rankings_regional'])}")

    sections.append("<h2>Most common names (total living population)</h2>")
    sections.append(df_to_html(tables['most_common']))

    body = "\n".join(sections)
    return f"<!doctype html><html><head><meta charset='utf-8'><title>Baby names report {DATA_YEAR}</title><style>{REPORT_CSS}</style></head><body>{body}</body></html>"


# =============================================================================
# MAIN PIPELINE
# =============================================================================

def main():
    px_female, px_male = load_px_data()
    rankings = build_rankings(px_female, px_male)
    detect_years(rankings)

    data = load_population_data()

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

    # --- year-over-year rank changes --------------------------------------
    changes = build_rank_changes(rankings)
    climbers, fallers = climbers_and_fallers(changes, n=10)
    new_entries, exits = new_entries_and_exits(changes)

    male_climber = climbers[climbers["gender"] == "male"].head(1)
    female_climber = climbers[climbers["gender"] == "female"].head(1)
    male_faller = fallers[fallers["gender"] == "male"].head(1)
    female_faller = fallers[fallers["gender"] == "female"].head(1)

    # Concise tables embedded directly in the article text (TOP_N_MOVERS_IN_ARTICLE per gender).
    climbers_top = climbers.groupby("gender", group_keys=False).head(TOP_N_MOVERS_IN_ARTICLE)
    fallers_top = fallers.groupby("gender", group_keys=False).head(TOP_N_MOVERS_IN_ARTICLE)

    # Full year-over-year rank changes for every name in the analysis pool (not just the top movers).
    rank_changes_full = changes[
        ["prenom", "gender", "rank_prev", "rank_current", "rank_change", "count_prev", "count_current", "count_change", "status"]
    ].sort_values(["gender", "rank_current"]).reset_index(drop=True)

    new_entries_sample = (
        new_entries[new_entries["gender"] == "male"]["prenom"].head(2).tolist()
        + new_entries[new_entries["gender"] == "female"]["prenom"].head(2).tolist()
    )

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
    def safe(series, col, default="-"):
        return series[col] if len(series) else default

    ctx = {
        "data_year": DATA_YEAR,
        "comparison_year": COMPARISON_YEAR,
        "national_male_1": national_male["prenom"].iloc[0],
        "national_male_2": national_male["prenom"].iloc[1],
        "national_male_3": national_male["prenom"].iloc[2],
        "national_female_1": national_female["prenom"].iloc[0],
        "national_female_2": national_female["prenom"].iloc[1],
        "national_female_3": national_female["prenom"].iloc[2],
        "alemanique_male_1": regional_top1[("alemanique", "male")],
        "alemanique_female_1": regional_top1[("alemanique", "female")],
        "romande_male_1": regional_top1[("romande", "male")],
        "romande_female_1": regional_top1[("romande", "female")],
        "italienne_male_1": regional_top1[("italienne", "male")],
        "italienne_female_1": regional_top1[("italienne", "female")],
        "male_climber": safe(male_climber.squeeze(), "prenom"),
        "male_climber_gain": int(safe(male_climber.squeeze(), "rank_change", 0)),
        "female_climber": safe(female_climber.squeeze(), "prenom"),
        "female_climber_gain": int(safe(female_climber.squeeze(), "rank_change", 0)),
        "male_faller": safe(male_faller.squeeze(), "prenom"),
        "male_faller_loss": abs(int(safe(male_faller.squeeze(), "rank_change", 0))),
        "female_faller": safe(female_faller.squeeze(), "prenom"),
        "female_faller_loss": abs(int(safe(female_faller.squeeze(), "rank_change", 0))),
        "new_entries_text": ", ".join(new_entries_sample),
        "most_common_male": male_most_common["firstname"].iloc[0],
        "most_common_male_count": male_most_common["total_population_current"].iloc[0],
        "most_common_male_count_prev": male_most_common["total_population_previous"].iloc[0],
        "most_common_female": female_most_common["firstname"].iloc[0],
        "most_common_female_count": female_most_common["total_population_current"].iloc[0],
        "most_common_female_count_prev": female_most_common["total_population_previous"].iloc[0],
        "avg_length_weighted_overall": length["weighted"]["overall"],
        "avg_length_weighted_male": length["weighted"]["male"],
        "avg_length_weighted_female": length["weighted"]["female"],
        "avg_length_unweighted_overall": length["unweighted"]["overall"],
    }

    articles = build_article_texts(ctx, climbers_top, fallers_top)

    # =============================================================================
    # EXPORT
    # =============================================================================

    print(f"\n=== Writing output to {OUTPUT_DIR} ===\n")

    for lang in ("fr", "de", "en"):
        path = OUTPUT_DIR / f"article_{lang}_{DATA_YEAR}.html"
        path.write_text(articles[lang], encoding="utf-8")
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
    }
    for name, df in tables.items():
        path = OUTPUT_DIR / f"{name}_{DATA_YEAR}.csv"
        df.to_csv(path, index=False)
        print(f"  wrote {path.name}")

    for name, df in {"biggest_population_increases": increases, "biggest_population_decreases": decreases}.items():
        path = OUTPUT_DIR / f"{name}_{DATA_YEAR}.csv"
        df.to_csv(path, index=False)
        print(f"  wrote {path.name}")

    report_html = build_report_html(articles, tables)
    report_path = OUTPUT_DIR / f"report_{DATA_YEAR}.html"
    report_path.write_text(report_html, encoding="utf-8")
    print(f"  wrote {report_path.name}  <- open this one first")

    print("\nDone.\n")
    return ctx, tables, articles


if __name__ == "__main__":
    main()
