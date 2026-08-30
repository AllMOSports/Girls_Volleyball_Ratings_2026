"""
MSHSAA Girls Volleyball GAMES Scraper - 2026 Season (schedule pull, no ratings)
=================================================================================

Adapted from scrape_football_games_2026.py / scrape_fall_softball_games_2026.py,
both proven to work correctly against the live MSHSAA scoreboard. Same
request approach (requests + BeautifulSoup, NOT Playwright -- the page is
plain server-rendered HTML, no JS execution needed), same session/retry
logic, same name-resolution via school ID in the team link href -- none
of that is sport-specific, it's the same /MySchool/Schedule.aspx link
pattern MSHSAA uses on every scoreboard.

WHAT'S DIFFERENT FROM FOOTBALL/FALL SOFTBALL
-----------------------------------------------
1. BASE_URL's "alg" parameter changed to 57 (MSHSAA Girls Volleyball).
   Confirmed via web search results referencing MSHSAA's own district-
   winner and state-championship pages using alg=57 for girls volleyball
   -- NOT verified by actually loading the scoreboard page, since this
   environment can't reach mshsaa.org (robots.txt disallows it). Load
   https://www.mshsaa.org/Activities/Scoreboard.aspx?alg=57 yourself once
   before relying on this and confirm the table layout matches what
   resolve_name_or_raw/is_mshsaa_team expect (same 2-team-rows-per-table
   shape football/softball's scoreboards use).
2. MAX_POINTS lowered to 5. THIS IS THE BIG STRUCTURAL DIFFERENCE:
   volleyball is scored in SETS, not a single running point total the
   way football/softball are. A match is best-of-5 sets (first to 3 set
   wins), so whatever number MSHSAA's scoreboard shows per team is very
   likely SETS WON (0-3), not total points across all sets played --
   parse_score()/is_mshsaa_team()/the scrape_date() table-pairing logic
   are unchanged and should work fine against a "sets won" number the
   same way they work against football's/softball's single score, but
   this is UNVERIFIED against a real live scoreboard. If it turns out
   MSHSAA actually displays total rally points summed across sets (a
   much bigger, less meaningful number) instead of sets won, MAX_POINTS
   will need to go back up and score1/score2 will mean something
   different than they do for every other sport on this site -- check
   a real match's scoreboard page before trusting this.
3. No extra_innings/overtime equivalent. Football has overtime periods,
   softball has extra innings -- volleyball doesn't have anything
   analogous at the match level (a single SET can go past 25 points,
   win-by-2, but that's not something MSHSAA's scoreboard is likely to
   flag as a separate marker the way "Final/OT" is on other scoreboards,
   and it isn't something the District Points Calculator asked for here
   the way it did for football). Dropped entirely rather than guessing
   at a flag with no real equivalent -- only "forfeit" carries over.
4. Output filenames changed to girls_volleyball_games_* throughout.
5. is_forfeit() is unchanged -- "forfeit" is the same word regardless of
   sport, no reason to expect MSHSAA's scoreboard to phrase it
   differently for volleyball.
6. SEASON_START/SEASON_END set to Aug 1 - Nov 14, 2026. MSHSAA's 2025
   girls volleyball state championship ran Nov 5-8 at the Civic Arena in
   St. Joseph, so SEASON_END here is padded about a week past that in
   case 2026's dates shift slightly. SEASON_START stays Aug 1, same
   reasoning as the other two scripts: scraping days before the season
   actually starts just returns 0 games, harmless.

REQUIRES (same shape as the football/softball pipelines, must be in the
same directory or update the paths below):
  - classifications.json  (2026-27 projected classifications -- THIS MUST
    BE GIRLS VOLLEYBALL'S OWN classifications.json, not football's or
    fall softball's copied over. Volleyball's class count/district
    groupings are their own thing, not the same schools-to-classes
    mapping the other sports use.)
  - mshsaa_schools.csv     (school_id -> school_name lookup -- this one
    genuinely is sport-agnostic, the same file football/softball use
    works here too, just needs to be copied into this repo)

Usage:
    python3 scrape_girls_volleyball_games_2026.py
"""

import requests
from bs4 import BeautifulSoup
import json
import csv
import re
import pandas as pd
from datetime import date, timedelta
import time

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

SEASON_YEAR   = 2026
SEASON_START  = date(2026, 8, 1)
SEASON_END    = date(2026, 11, 14)   # ~1 week past the 2025 state finals (Nov 5-8); adjust if 2026's championship dates land differently
BASE_URL      = "https://www.mshsaa.org/activities/scoreboard.aspx?alg=57&date={}"
MAX_POINTS    = 5   # sets won per match (best-of-5), NOT total rally points -- see docstring point 2, unverified
OUTPUT_JSON   = f"girls_volleyball_games_{SEASON_YEAR}.json"
OUTPUT_CSV    = f"girls_volleyball_games_{SEASON_YEAR}.csv"
OUTPUT_JSON_ALL = f"girls_volleyball_games_{SEASON_YEAR}_all.json"
OUTPUT_CSV_ALL  = f"girls_volleyball_games_{SEASON_YEAR}_all.csv"
CLASSIFICATIONS_PATH = "classifications.json"   # must be girls volleyball's own -- see docstring
SCHOOLS_CSV           = "mshsaa_schools.csv"

REQUEST_DELAY = 0.5  # seconds between requests, matches the football/softball scripts

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://www.mshsaa.org/"
}

# ---------------------------------------------------------------------------
# HTTP SESSION (identical to the football/softball scripts)
# ---------------------------------------------------------------------------

def build_session():
    from requests.adapters import HTTPAdapter
    try:
        from urllib3.util.retry import Retry
    except ImportError:
        from requests.packages.urllib3.util.retry import Retry

    session = requests.Session()
    retry = Retry(
        total=1,
        connect=1,
        read=1,
        backoff_factor=1.5,
        status_forcelist=[500, 502, 503, 504],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=1, pool_maxsize=1)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


# ---------------------------------------------------------------------------
# CLASSIFICATIONS / NAME RESOLUTION (identical to football/softball --
# generic MSHSAA site plumbing, not sport-specific. MANUAL_OVERRIDES below
# is copied as a starting point from football_ratings_2025.py; some
# entries may not apply to volleyball (a co-op that plays football
# together but not volleyball, or vice versa) and some volleyball-only
# co-ops may be missing. Treat this as a first draft, not a verified
# volleyball list -- cross-check against girls volleyball's actual
# classifications.json once you have it.)
# ---------------------------------------------------------------------------

def load_classifications(path=CLASSIFICATIONS_PATH):
    with open(path) as f:
        data = json.load(f)
    team_to_class    = {}
    team_to_district = {}
    for entry in data["teams"]:
        school = entry["school"]
        team_to_class[school]    = entry["classification"]
        team_to_district[school] = entry["district"]
    return team_to_class, team_to_district


def build_id_to_classname(team_to_class, schools_csv=SCHOOLS_CSV):
    """
    Build { school_id_str : classification_name }. NOTE: the MANUAL_OVERRIDES
    dict below is copied from the football script as of its last known good
    run -- see the module docstring. If your girls volleyball classifications.json
    renames/adds/removes any co-op programs, this list may need updating.
    """
    MANUAL_OVERRIDES = {
        "271": "Clopton with Elsberry",
        "331": "King City with Pattonsburg",
        "126": "Lockwood with Golden City",
        "421": "Princeton with Mercer",
        "424": "Rich Hill with Hume",
        "431": "Salisbury",
        "435": "Scott City",
        "443": "Skyline",
        "193": "Slater",
        "194": "Smith-Cotton",
        "197": "South Callaway",
        "549": "St. Mary's South Side",
        "463": "Stockton",
        "207": "Sullivan",
        "208": "Sumner",
        "469": "Sweet Springs with Malta Bend",
        "198": "Truman",
        "479": "University Academy Charter",
        "204": "Van Horn",
        "206": "Vashon",
        "20": "Appleton City with Montrose",
        "275": "Drexel with Miami (Amoret)",
        "575": "Renaissance Academy Charter",
        "172": "St. James",
        "35": "DeSoto with Kingston",
        "917": "Father Tolton with Calvary Lutheran",
        "342": "Liberal with Bronaugh",
        "776": "Transportation and Law with Beaumont",
        "483": "Van-Far with Community",
    }

    df = pd.read_csv(schools_csv)
    known_class_names = set(team_to_class.keys())

    id_to_classname = {}
    for _, row in df.iterrows():
        full_name = row["school_name"]
        sid       = str(row["school_id"])
        stripped  = full_name.replace(" High School", "").strip()

        if stripped in known_class_names:
            id_to_classname[sid] = stripped
        elif full_name in known_class_names:
            id_to_classname[sid] = full_name

    id_to_classname.update(MANUAL_OVERRIDES)

    print(f"  [name-resolve] {len(id_to_classname)} schools mapped by ID "
          f"({len(MANUAL_OVERRIDES)} via manual overrides)")
    return id_to_classname


def resolve_name(cell, id_to_classname, known_teams):
    """Identical logic to the football/softball scripts."""
    a = cell.find("a", href=lambda h: h and "/MySchool/Schedule.aspx" in h)
    if not a:
        return None

    href  = a.get("href", "")
    match = re.search(r"[?&]s=(\d+)", href, re.IGNORECASE)
    if match:
        sid = match.group(1)
        if sid in id_to_classname:
            return id_to_classname[sid]

    display_text = a.get_text(strip=True)
    if display_text in known_teams:
        return display_text

    return None


# ---------------------------------------------------------------------------
# SCRAPING
# ---------------------------------------------------------------------------

def resolve_name_or_raw(cell, id_to_classname, known_teams):
    """
    Like resolve_name(), but never returns None for a cell that has an
    MSHSAA schedule link -- if the school ID/display text doesn't match
    classifications.json (an unclassified or missing school), this falls
    back to whatever display text the scoreboard link shows, and reports
    that the team is unclassified via the second return value.
    Returns (name, classified: bool).
    """
    name = resolve_name(cell, id_to_classname, known_teams)
    if name is not None:
        return name, True

    a = cell.find("a", href=lambda h: h and "/MySchool/Schedule.aspx" in h)
    raw = a.get_text(strip=True) if a else None
    return raw, False


def is_mshsaa_team(cell):
    return cell.find(
        "a", href=lambda h: h and "/MySchool/Schedule.aspx" in h
    ) is not None


def parse_score(text):
    text = text.strip()
    if not text:
        return None
    try:
        score = int(text)
    except ValueError:
        return None
    return score if 0 <= score <= MAX_POINTS else None


def is_forfeit(row1, row2):
    return "forfeit" in (row1.get_text() + row2.get_text()).lower()


def scrape_date(target_date, id_to_classname, known_teams, session):
    """
    Generalized version of the football/softball scripts' scrape_date():
    scans every row in every table for a cell containing an MSHSAA team
    link (rather than assuming team names always sit at a fixed row/column
    index), so it works whether the table has a score column (completed
    matches) or not (scheduled matches). Pairs up tables with exactly 2
    team-rows as a single match. Score is captured if present, else None
    -- see MAX_POINTS above regarding what "score" means for volleyball
    (sets won, not rally points -- unverified).
    """
    url = BASE_URL.format(target_date.strftime("%m%d%Y"))
    try:
        resp = session.get(url, timeout=(10, 25), headers=HEADERS)
        resp.raise_for_status()
    except requests.exceptions.Timeout as e:
        print(f"  TIMEOUT {target_date}: {e}")
        return [], "timeout"
    except requests.RequestException as e:
        print(f"  Failed {target_date}: {e}")
        return [], "error"

    soup  = BeautifulSoup(resp.text, "html.parser")
    games = []

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        team_rows = []  # list of (name, classified, score, row) for rows with a team link
        for row in rows:
            cells = row.find_all("td")
            if not cells:
                continue
            team_cell = next((c for c in cells if is_mshsaa_team(c)), None)
            if team_cell is None:
                continue

            name, classified = resolve_name_or_raw(team_cell, id_to_classname, known_teams)
            if name is None:
                # Has the schedule link but somehow no display text either --
                # too broken to use, skip just this row.
                continue

            score = None
            for c in cells:
                s = parse_score(c.get_text())
                if s is not None:
                    score = s
                    break

            team_rows.append((name, classified, score, row))

        if len(team_rows) != 2:
            continue  # not a clean 2-team match table -- skip

        (name1, classified1, s1, row1), (name2, classified2, s2, row2) = team_rows
        if name1 == name2:
            continue

        games.append({
            "date": target_date.strftime("%Y-%m-%d"),
            "team1": name1,
            "team1_classified": classified1,
            "score1": s1,
            "team2": name2,
            "team2_classified": classified2,
            "score2": s2,
            "forfeit": is_forfeit(row1, row2),
        })

    return games, None


def scrape_full_season(id_to_classname, known_teams):
    all_games   = []
    current     = SEASON_START
    scrape_t0   = time.perf_counter()
    slow_days   = []
    failed_days = []
    session     = build_session()

    while current <= SEASON_END:
        day_t0 = time.perf_counter()
        print(f"  Scraping {current}...", end=" ", flush=True)
        day_games, fail_reason = scrape_date(current, id_to_classname, known_teams, session)
        all_games.extend(day_games)
        day_elapsed = time.perf_counter() - day_t0
        print(f"{len(day_games)} games ({day_elapsed:.1f}s)")
        if day_elapsed > 3.0:
            slow_days.append((current, day_elapsed))
        if fail_reason is not None:
            failed_days.append((current, fail_reason))
        current += timedelta(days=1)
        time.sleep(REQUEST_DELAY)

    scrape_elapsed = time.perf_counter() - scrape_t0
    print(f"\n  [TIMING] Scraping took {scrape_elapsed:.1f}s total "
          f"for {len(all_games)} games.")
    if slow_days:
        print(f"  [TIMING] {len(slow_days)} slow day(s) (>3s each):")
        for d, secs in slow_days:
            print(f"    {d}: {secs:.1f}s")
    if failed_days:
        print(f"\n  *** {len(failed_days)} date(s) NEVER returned data, "
              f"even after retry: ***")
        for d, reason in failed_days:
            print(f"    {d} ({reason})")
    else:
        print("  All dates returned successfully -- no known data gaps "
              "from scraping failures.")
    return all_games


def deduplicate_games(all_games):
    """Same score-independent dedup key as the football/softball scripts."""
    seen = set()
    unique_games = []
    duplicates = 0
    for g in all_games:
        key = (g["date"], frozenset([g["team1"], g["team2"]]))
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        unique_games.append(g)

    if duplicates:
        print(f"  Removed {duplicates} duplicate game(s). "
              f"{len(unique_games)} unique games remain.")
    else:
        print(f"  No duplicates found. {len(unique_games)} games.")
    return unique_games


def strict_games_from_all(all_games):
    """
    Filters the full (>=1 classified team) game list down to games where
    BOTH teams are in classifications.json, and reshapes each record back
    to the original schema (no *_classified fields) so this stays a
    drop-in replacement for whatever already consumes girls_volleyball_games_2026.json.
    """
    strict = []
    for g in all_games:
        if not (g["team1_classified"] and g["team2_classified"]):
            continue
        strict.append({
            "date": g["date"],
            "team1": g["team1"],
            "score1": g["score1"],
            "team2": g["team2"],
            "score2": g["score2"],
            "forfeit": g["forfeit"],
        })
    return strict


def save_json(all_games, path=OUTPUT_JSON):
    with open(path, "w") as f:
        json.dump(all_games, f, indent=2)
    print(f"Saved {len(all_games)} games to {path}")


def save_csv(all_games, path=OUTPUT_CSV):
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "team1", "score1", "team2", "score2", "forfeit"])
        for g in all_games:
            writer.writerow([g["date"], g["team1"], g["score1"], g["team2"], g["score2"],
                              g["forfeit"]])
    print(f"Saved {len(all_games)} games to {path}")


def save_csv_all(all_games, path=OUTPUT_CSV_ALL):
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "team1", "team1_classified", "score1",
                          "team2", "team2_classified", "score2",
                          "forfeit"])
        for g in all_games:
            writer.writerow([g["date"], g["team1"], g["team1_classified"], g["score1"],
                              g["team2"], g["team2_classified"], g["score2"],
                              g["forfeit"]])
    print(f"Saved {len(all_games)} games to {path}")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"=== MSHSAA Girls Volleyball Games Pull {SEASON_YEAR} (schedule, no ratings) ===")

    print("\nLoading classifications...")
    team_to_class, team_to_district = load_classifications()
    known_teams = set(team_to_class.keys())
    print(f"  Loaded {len(team_to_class)} teams from {CLASSIFICATIONS_PATH}")

    print("\nBuilding school ID -> classification name lookup...")
    id_to_classname = build_id_to_classname(team_to_class, SCHOOLS_CSV)

    print(f"\nScraping {SEASON_START} to {SEASON_END}...")
    all_games = scrape_full_season(id_to_classname, known_teams)
    print(f"\nTotal games found (before dedup, >=1 classified team): {len(all_games)}")

    if not all_games:
        print("No games found. This is expected if the 2026 schedule "
              "hasn't been posted to MSHSAA yet -- try again closer to "
              "the season, or check a known date manually in a browser. "
              "Also double check alg=57 is really girls volleyball, and "
              "that its scoreboard shows sets won rather than total "
              "rally points -- see the module docstring, neither was "
              "verified against the live page.")

    print("\nDeduplicating...")
    all_games = deduplicate_games(all_games)

    strict_games = strict_games_from_all(all_games)
    print(f"Of those, {len(strict_games)} have both teams classified "
          f"({len(all_games) - len(strict_games)} have exactly one classified side).")

    print("\nSaving output...")
    save_json(strict_games, OUTPUT_JSON)
    save_csv(strict_games, OUTPUT_CSV)
    save_json(all_games, OUTPUT_JSON_ALL)
    save_csv_all(all_games, OUTPUT_CSV_ALL)

    print("\n=== Done ===")
