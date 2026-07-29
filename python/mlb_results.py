#!/usr/bin/env python3
"""
mlb_results.py — Auto-settle MLB legs using MLB Stats API (free, no key).

Input  (stdin): JSON array of pending legs
  [{ legId, playerName, statType, lineScore, gameId, gameDate }]

Output (stdout): JSON array of settled legs
  [{ legId, playerName, statType, lineScore, actualValue, hit, source }]

MLB Stats API base: https://statsapi.mlb.com/api/v1
"""

import json
import logging
import sys
import re
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional
from urllib.request import urlopen
from urllib.error import URLError

logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
log = logging.getLogger(__name__)

MLB_API = "https://statsapi.mlb.com/api/v1"

# ── Stat type → MLB box score field mapping ──────────────────────────────────
# Maps GOTit stat names → (stat_group, field_path)
# stat_group: 'hitting' | 'pitching'
STAT_MAP: Dict[str, Dict] = {
    # Hitter stats
    "Total Bases":          {"group": "hitting", "field": "totalBases"},
    "Hits":                 {"group": "hitting", "field": "hits"},
    "Hits+Runs+RBIs":       {"group": "hitting", "field": None, "computed": ["hits", "runs", "rbi"]},
    "Hitter Fantasy Score": {"group": "hitting", "field": None, "computed": "hitter_fs"},
    "Home Runs":            {"group": "hitting", "field": "homeRuns"},
    "RBIs":                 {"group": "hitting", "field": "rbi"},
    "Runs":                 {"group": "hitting", "field": "runs"},
    "Stolen Bases":         {"group": "hitting", "field": "stolenBases"},
    "Walks":                {"group": "hitting", "field": "baseOnBalls"},
    "Singles":              {"group": "hitting", "field": None, "computed": "singles"},
    "Doubles":              {"group": "hitting", "field": "doubles"},
    "Triples":              {"group": "hitting", "field": "triples"},
    "Plate Appearances":    {"group": "hitting", "field": "plateAppearances"},
    "Hitter Strikeouts":    {"group": "hitting", "field": "strikeOuts"},
    # Pitcher stats
    "Pitcher Strikeouts":   {"group": "pitching", "field": "strikeOuts"},
    "Pitches Thrown":       {"group": "pitching", "field": "pitchesThrown"},
    "Pitching Outs":        {"group": "pitching", "field": "outs"},
    "Hits Allowed":         {"group": "pitching", "field": "hits"},
    "Earned Runs Allowed":  {"group": "pitching", "field": "earnedRuns"},
    "Walks Allowed":        {"group": "pitching", "field": "baseOnBalls"},
    "Pitcher Fantasy Score":{"group": "pitching", "field": None, "computed": "pitcher_fs"},
}

# Hitter Fantasy Score weights (PP formula)
# FS = TB*3 + R*2 + RBI*2 + BB*2 + SB*5 - K*1
def hitter_fs(stats: Dict) -> float:
    return (
        int(stats.get("totalBases", 0) or 0) * 3
        + int(stats.get("runs", 0) or 0) * 2
        + int(stats.get("rbi", 0) or 0) * 2
        + int(stats.get("baseOnBalls", 0) or 0) * 2
        + int(stats.get("stolenBases", 0) or 0) * 5
        - int(stats.get("strikeOuts", 0) or 0) * 1
    )

# Pitcher Fantasy Score weights (PP formula)
# FS = K*4 + IP*2.25 - ER*2 - BB*1 + CG_bonus
def pitcher_fs(stats: Dict) -> float:
    outs = int(stats.get("outs", 0) or 0)
    ip   = outs / 3.0
    return (
        int(stats.get("strikeOuts", 0) or 0) * 4
        + ip * 2.25
        - int(stats.get("earnedRuns", 0) or 0) * 2
        - int(stats.get("baseOnBalls", 0) or 0) * 1
    )

def singles(stats: Dict) -> int:
    h  = int(stats.get("hits", 0) or 0)
    d  = int(stats.get("doubles", 0) or 0)
    t  = int(stats.get("triples", 0) or 0)
    hr = int(stats.get("homeRuns", 0) or 0)
    return max(0, h - d - t - hr)


def _get(url: str) -> Optional[Dict]:
    try:
        with urlopen(url, timeout=10) as r:
            return json.loads(r.read())
    except (URLError, json.JSONDecodeError) as e:
        log.warning("GET %s failed: %s", url, e)
        return None


def get_games_for_date(date_str: str) -> List[Dict]:
    """Fetch all MLB games for a date (YYYY-MM-DD)."""
    url = f"{MLB_API}/schedule?sportId=1&date={date_str}&hydrate=linescore"
    data = _get(url)
    if not data:
        return []
    games = []
    for d in data.get("dates", []):
        for g in d.get("games", []):
            games.append({
                "gamePk":   g["gamePk"],
                "status":   g["status"]["abstractGameState"],  # Final / Live / Preview
                "home":     g["teams"]["home"]["team"]["name"],
                "away":     g["teams"]["away"]["team"]["name"],
                "gameDate": g["officialDate"],
            })
    return games


def get_box_score(game_pk: int) -> Optional[Dict]:
    """Fetch full box score for a game."""
    url = f"{MLB_API}/game/{game_pk}/boxscore"
    return _get(url)


def extract_player_stats(box_score: Dict) -> Dict[str, Dict]:
    """
    Returns { playerName_normalized: { hitting: {...}, pitching: {...} } }
    Normalizes names to lowercase stripped for fuzzy match.
    """
    players: Dict[str, Dict] = {}

    for side in ("home", "away"):
        team = box_score.get("teams", {}).get(side, {})
        for pid, info in team.get("players", {}).items():
            name = info.get("person", {}).get("fullName", "")
            if not name:
                continue
            key = _normalize(name)
            stats: Dict[str, Any] = {}
            for group in ("batting", "pitching", "fielding"):
                s = info.get("stats", {}).get(group, {}).get("summary", None)
                raw = info.get("stats", {}).get(group, {})
                if raw:
                    stats[group] = raw
            players[key] = {
                "name": name,
                "hitting":  info.get("stats", {}).get("batting",  {}).get("summary", {}),
                "pitching": info.get("stats", {}).get("pitching", {}).get("summary", {}),
                "hitting_raw":  info.get("stats", {}).get("batting",  {}),
                "pitching_raw": info.get("stats", {}).get("pitching", {}),
            }
    return players


def _normalize(name: str) -> str:
    """Lowercase, strip punctuation, collapse spaces."""
    return re.sub(r"[^a-z0-9 ]", "", name.lower()).strip()


def _get_actual_value(player_stats: Dict, stat_type: str) -> Optional[float]:
    """Extract actual value for a GOTit stat_type from player box score stats."""
    mapping = STAT_MAP.get(stat_type)
    if not mapping:
        return None

    group   = mapping["group"]
    raw_key = "hitting_raw" if group == "hitting" else "pitching_raw"
    raw     = player_stats.get(raw_key, {})
    if not raw:
        return None

    # Computed stats
    computed = mapping.get("computed")
    if computed == "hitter_fs":
        return float(hitter_fs(raw))
    if computed == "pitcher_fs":
        return float(pitcher_fs(raw))
    if computed == "singles":
        return float(singles(raw))
    if isinstance(computed, list):
        # Sum of multiple fields (e.g. Hits+Runs+RBIs)
        return float(sum(int(raw.get(f, 0) or 0) for f in computed))

    field = mapping.get("field")
    if field and field in raw:
        return float(raw[field] or 0)

    return None


def _date_from_game_id(game_id: str) -> Optional[str]:
    """
    Try to extract a date from a gameId string.
    Common formats: 'mlb-nyy-bos-2026-07-28', '745123', '2026-07-28-nyy-bos'
    Falls back to today CDT.
    """
    m = re.search(r"(\d{4}-\d{2}-\d{2})", game_id or "")
    if m:
        return m.group(1)
    return None


def auto_settle(pending_legs: List[Dict]) -> List[Dict]:
    """
    Main entry point.
    pending_legs: [{ legId, playerName, statType, lineScore, gameId, gameDate }]
    Returns: [{ legId, playerName, statType, lineScore, actualValue, hit, source, error? }]
    """
    if not pending_legs:
        return []

    # ── Collect unique dates to query ──────────────────────────────────────
    dates_needed: set = set()
    for leg in pending_legs:
        d = leg.get("gameDate") or _date_from_game_id(leg.get("gameId", ""))
        if d:
            dates_needed.add(d)

    # If no date on legs, try yesterday + today (CDT = UTC-5)
    if not dates_needed:
        now_cdt = datetime.now(timezone.utc) - timedelta(hours=5)
        dates_needed.add(now_cdt.strftime("%Y-%m-%d"))
        yesterday = now_cdt - timedelta(days=1)
        dates_needed.add(yesterday.strftime("%Y-%m-%d"))

    # ── Fetch all final games for those dates ──────────────────────────────
    all_games: List[Dict] = []
    for d in sorted(dates_needed):
        games = get_games_for_date(d)
        all_games.extend([g for g in games if g["status"] == "Final"])

    log.info("Found %d final games across dates %s", len(all_games), dates_needed)

    # ── Fetch box scores (one per game, cache) ────────────────────────────
    box_cache: Dict[int, Dict[str, Dict]] = {}
    for game in all_games:
        pk = game["gamePk"]
        box = get_box_score(pk)
        if box:
            box_cache[pk] = extract_player_stats(box)

    # Build a flat player → stats lookup across all games
    # Key: normalized player name → list of stats dicts (player may appear in multiple games? no, but safe)
    player_lookup: Dict[str, Dict] = {}
    for stats_by_player in box_cache.values():
        for norm_name, pstats in stats_by_player.items():
            player_lookup[norm_name] = pstats  # last game wins if dup (shouldn't happen)

    # ── Settle each leg ───────────────────────────────────────────────────
    results = []
    for leg in pending_legs:
        leg_id      = leg.get("legId")
        player_name = leg.get("playerName", "")
        stat_type   = leg.get("statType", "")
        line        = float(leg.get("lineScore", 0) or 0)
        norm        = _normalize(player_name)

        pstats = player_lookup.get(norm)

        # Fuzzy fallback: try partial match if exact fails
        if not pstats:
            parts = norm.split()
            for k, v in player_lookup.items():
                if all(p in k for p in parts[-1:]):  # last name match
                    pstats = v
                    break

        if not pstats:
            results.append({
                "legId":       leg_id,
                "playerName":  player_name,
                "statType":    stat_type,
                "lineScore":   line,
                "actualValue": None,
                "hit":         None,
                "source":      "mlb_stats_api",
                "error":       "player_not_found",
            })
            continue

        actual = _get_actual_value(pstats, stat_type)
        if actual is None:
            results.append({
                "legId":       leg_id,
                "playerName":  player_name,
                "statType":    stat_type,
                "lineScore":   line,
                "actualValue": None,
                "hit":         None,
                "source":      "mlb_stats_api",
                "error":       f"stat_not_mapped:{stat_type}",
            })
            continue

        hit = actual > line  # PP More wins if actual > line (strict greater)
        results.append({
            "legId":       leg_id,
            "playerName":  player_name,
            "statType":    stat_type,
            "lineScore":   line,
            "actualValue": actual,
            "hit":         hit,
            "source":      "mlb_stats_api",
            "error":       None,
        })

    return results


if __name__ == "__main__":
    raw = sys.stdin.read().strip()
    if not raw:
        print(json.dumps([]))
        sys.exit(0)
    try:
        legs = json.loads(raw)
        results = auto_settle(legs)
        print(json.dumps(results))
    except Exception as e:
        log.exception("mlb_results fatal error")
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
