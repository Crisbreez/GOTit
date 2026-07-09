"""
sharp_consensus.py — Real SharpConsensus from SportsGameOdds fairOdds.

SportsGameOdds already computes de-vigged fair odds (`fairOdds`) and a fair
line (`fairOverUnder`) for each player prop. This is better than raw Pinnacle
XML — SGO aggregates multiple sharp books and removes vig for us.

Key fields we use:
    fairOdds        — American odds string (e.g. "+113", "-131")
                      converted → p_win_over = 1 / (1 + 1.13) = 0.469
    fairOverUnder   — The fair/sharp consensus line number (e.g. "4.5")
                      used as synthetic_median in the distribution model
    playerID        — e.g. "BRYCE_ELDER_1_MLB"
    players[id].name — "Bryce Elder"  (matched to PP playerName)
    statID          — e.g. "pitching_strikeouts"  (mapped to PP stat_type)

Matching strategy:
    PP prop → SGO prop via (normalized_player_name, mapped_stat_type)
    Returns SharpConsensus keyed by PP prop_id.
    Unmatched props get a tier-aware fallback (no real sharp data).

Store:
    Results cached to python/config/sharp_store.json
    Refreshed on each /api/pull call for the same league.
"""
from __future__ import annotations

import json
import logging
import math
import re
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import httpx
    _HAS_HTTPX = True
except ImportError:
    _HAS_HTTPX = False

try:
    import requests as _requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

from gotit.leg_selector import PPProp, SharpConsensus, Tier

log = logging.getLogger("gotit.sharp_consensus")

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────
SGO_BASE = "https://api.sportsgameodds.com/v2"
SGO_KEY  = "cdcc1cf23a44326b7e4d190616787462"

# Freshness: consider sharp data stale after 30 minutes
FRESH_SEC     = 1800
FALLBACK_SEC  = 9999.0

# Store path — written by pull, read by optimize.py
STORE_PATH = Path(__file__).parent.parent / "config" / "sharp_store.json"

# ─────────────────────────────────────────────────────────────────────────────
# SGO league IDs
# ─────────────────────────────────────────────────────────────────────────────
SGO_LEAGUE: Dict[str, str] = {
    "MLB": "MLB",
    "NBA": "NBA",
    "NFL": "NFL",
    "MMA": "MMA",
}

# ─────────────────────────────────────────────────────────────────────────────
# Stat type mapping: SGO statID → PP stat_type
# ─────────────────────────────────────────────────────────────────────────────
SGO_TO_PP: Dict[str, str] = {
    # MLB batting
    "batting_hits":            "Hits",
    "batting_homeRuns":        "Home Runs",
    "batting_totalBases":      "Total Bases",
    "batting_hits+runs+rbi":   "Hits+Runs+RBIs",
    "batting_RBI":             "RBIs",
    "batting_basesOnBalls":    "Walks",
    "batting_stolenBases":     "Stolen Bases",
    "batting_strikeouts":      "Strikeouts",
    "batting_runs":            "Runs",
    # MLB pitching
    "pitching_strikeouts":     "Strikeouts",
    "pitching_outs":           "Pitching Outs",
    "pitching_basesOnBalls":   "Walks Allowed",
    "pitching_earnedRuns":     "Earned Runs Allowed",
    "pitching_hits":           "Hits Allowed",
    # NBA / multi-sport
    "points":                  "Points",
    "rebounds":                "Rebounds",
    "assists":                 "Assists",
    "threePointersMade":       "3-PT Made",
    "steals":                  "Steals",
    "blocks":                  "Blocks",
    "turnovers":               "Turnovers",
    "fantasyScore":            "Fantasy Score",
    "hitterFantasyScore":      "Hitter Fantasy Score",
    "points+rebounds+assists": "Pts+Reb+Ast",
    "receptions":              "Receptions",
    # NFL
    "passingYards":            "Passing Yards",
    "rushingYards":            "Rushing Yards",
    "receivingYards":          "Receiving Yards",
    "passingTouchdowns":       "Passing TDs",
    "rushingAttempts":         "Rush Attempts",
    "receptions_nfl":          "Receptions",
}

# Reverse map: PP stat_type → SGO statID (for lookup)
PP_TO_SGO: Dict[str, str] = {v: k for k, v in SGO_TO_PP.items()}


# ─────────────────────────────────────────────────────────────────────────────
# Name normalization
# ─────────────────────────────────────────────────────────────────────────────
def _normalize_name(name: str) -> str:
    """Lowercase, strip accents, remove non-alpha, collapse spaces."""
    n = unicodedata.normalize("NFD", name)
    n = "".join(c for c in n if unicodedata.category(c) != "Mn")
    n = re.sub(r"[^a-z ]", "", n.lower())
    return " ".join(n.split())


# ─────────────────────────────────────────────────────────────────────────────
# Fair odds → p_win conversion
# ─────────────────────────────────────────────────────────────────────────────
def _american_to_prob(american: str) -> Optional[float]:
    """
    Convert American odds string to implied probability.
    Already de-vigged by SGO, so this IS the fair p_win.
    Returns None if parsing fails.
    """
    try:
        v = float(american.replace("+", "").strip())
        if v >= 0:
            # +113 → prob = 100 / (100 + 113) = 0.469
            return 100.0 / (100.0 + v)
        else:
            # -131 → prob = 131 / (131 + 100) = 0.567
            return abs(v) / (abs(v) + 100.0)
    except (ValueError, AttributeError):
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Fallback SharpConsensus (no real data)
# ─────────────────────────────────────────────────────────────────────────────
def _fallback_sc(prop_id: str, line: float, tier: Tier, stat_type: str) -> SharpConsensus:
    """
    Tier-aware fallback when no SGO match is found.
    Uses calibration delta for a synthetic median shift.
    """
    cal_path = Path(__file__).parent.parent / "config" / "calibration_latest.json"
    delta_demon = {}
    delta_goblin = {}
    if cal_path.exists():
        try:
            with open(cal_path) as f:
                cal = json.load(f)
            delta_demon  = cal.get("delta_demon", {})
            delta_goblin = cal.get("delta_goblin", {})
        except Exception:
            pass

    if tier == Tier.DEMON:
        delta = delta_demon.get(stat_type, delta_demon.get("default", 0.0))
        median = line - delta
    elif tier == Tier.GOBLIN:
        delta = delta_goblin.get(stat_type, delta_goblin.get("default", 0.0))
        median = line + delta
    else:
        median = line

    return SharpConsensus(
        prop_id=prop_id,
        median=median,
        shape_params={},
        timestamp=datetime.now(timezone.utc).isoformat(),
        books_used=[],
        freshness_sec=FALLBACK_SEC,
    )


# ─────────────────────────────────────────────────────────────────────────────
# HTTP helper (sync, works without async framework)
# ─────────────────────────────────────────────────────────────────────────────
def _get_json(url: str, params: dict) -> Optional[dict]:
    """Simple sync HTTP GET → JSON. Uses requests or httpx."""
    headers = {"User-Agent": "GOTit/2.0"}
    if _HAS_REQUESTS:
        try:
            r = _requests.get(url, params=params, headers=headers, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning(f"requests GET failed: {e}")
            return None
    if _HAS_HTTPX:
        try:
            import httpx as _hx
            with _hx.Client(timeout=15) as c:
                r = c.get(url, params=params, headers=headers)
                r.raise_for_status()
                return r.json()
        except Exception as e:
            log.warning(f"httpx GET failed: {e}")
            return None
    log.error("No HTTP library available (install requests or httpx)")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# SGO fetch: all player props for a league
# ─────────────────────────────────────────────────────────────────────────────
def _fetch_sgo_props(league: str) -> List[dict]:
    """
    Fetch all player prop odds for a league from SGO.
    Returns a flat list of prop dicts with fields:
        player_name, stat_type_pp, fair_line, fair_p_win_over,
        fair_p_win_under, books_used, fetched_at
    """
    sgo_league = SGO_LEAGUE.get(league.upper(), league.upper())
    page = 1
    all_events = []

    while True:
        data = _get_json(
            f"{SGO_BASE}/events",
            {
                "apiKey":         SGO_KEY,
                "leagueID":       sgo_league,
                "oddsAvailable":  "true",
                "limit":          100,
                "page":           page,
            },
        )
        if not data:
            break
        events = data.get("data", [])
        if not events:
            break
        all_events.extend(events)
        # Only 1 page needed for today's slate
        break

    props_out: List[dict] = []
    fetched_at = datetime.now(timezone.utc).isoformat()

    for ev in all_events:
        players_map: Dict[str, str] = {}
        for pid, pdata in ev.get("players", {}).items():
            name = pdata.get("name", "")
            if name:
                players_map[pid] = name

        odds = ev.get("odds", {})
        # Only OVER odds — we pair with UNDER by opposingOddID
        for odd_id, odd in odds.items():
            if odd.get("sideID") != "over":
                continue
            if odd.get("betTypeID") != "ou":
                continue

            stat_id    = odd.get("statID", "")
            player_id  = odd.get("playerID", "")
            pp_stat    = SGO_TO_PP.get(stat_id)
            if not pp_stat:
                continue  # stat type not in our map

            # Skip team-level odds
            if player_id in ("all", "home", "away", ""):
                continue

            player_name = players_map.get(player_id, "")
            if not player_name:
                # Try parsing from player_id: "BRYCE_ELDER_1_MLB" → "Bryce Elder"
                parts = player_id.split("_")
                # Drop last two tokens (number + league)
                if len(parts) > 2:
                    parts = parts[:-2]
                player_name = " ".join(p.capitalize() for p in parts)

            fair_line_str  = odd.get("fairOverUnder") or odd.get("bookOverUnder") or ""
            fair_odds_over = odd.get("fairOdds") or odd.get("bookOdds") or ""

            # Get UNDER fair odds from opposing odd
            opp_id = odd.get("opposingOddID", "")
            opp    = odds.get(opp_id, {})
            fair_odds_under = opp.get("fairOdds") or opp.get("bookOdds") or ""

            try:
                fair_line = float(fair_line_str)
            except (ValueError, TypeError):
                continue

            p_over  = _american_to_prob(fair_odds_over)
            p_under = _american_to_prob(fair_odds_under)

            if p_over is None and p_under is None:
                continue

            # If only one side available, derive the other
            if p_over is None and p_under is not None:
                p_over = 1.0 - p_under
            if p_under is None and p_over is not None:
                p_under = 1.0 - p_over

            books_used = list(odd.get("byBookmaker", {}).keys())

            props_out.append({
                "player_name":    player_name,
                "player_id_sgo":  player_id,
                "stat_type_pp":   pp_stat,
                "stat_id_sgo":    stat_id,
                "fair_line":      fair_line,
                "fair_p_win_over":  round(p_over,  4),
                "fair_p_win_under": round(p_under, 4),
                "books_used":     books_used,
                "fetched_at":     fetched_at,
            })

    log.info(f"[sharp] fetched {len(props_out)} SGO props for {league}")
    return props_out


# ─────────────────────────────────────────────────────────────────────────────
# Match PP props → SGO props
# ─────────────────────────────────────────────────────────────────────────────
def _match_props(
    pp_props: List[PPProp],
    sgo_props: List[dict],
) -> Dict[str, SharpConsensus]:
    """
    Match each PP prop to the best SGO prop by:
      1. Normalized player name (exact)
      2. PP stat type == SGO pp_stat_type
      3. Line proximity: |pp_line - sgo_fair_line| ≤ 1.5

    Returns dict: prop_id → SharpConsensus
    """
    # Build SGO lookup: (norm_name, stat_type_pp) → list of sgo props
    sgo_lookup: Dict[Tuple[str, str], List[dict]] = {}
    for sp in sgo_props:
        key = (_normalize_name(sp["player_name"]), sp["stat_type_pp"])
        sgo_lookup.setdefault(key, []).append(sp)

    result: Dict[str, SharpConsensus] = {}

    for pp in pp_props:
        # Get the prop's line (first tier offered)
        tier = pp.tiers_offered[0] if pp.tiers_offered else Tier.STANDARD
        pp_line = pp.lines.get(tier, list(pp.lines.values())[0])

        norm_name = _normalize_name(pp.player_name)
        key = (norm_name, pp.stat_type)

        candidates = sgo_lookup.get(key, [])

        # Find closest line match
        best: Optional[dict] = None
        best_diff = float("inf")
        for sp in candidates:
            diff = abs(sp["fair_line"] - pp_line)
            if diff < best_diff:
                best_diff = diff
                best = sp

        if best and best_diff <= 1.5:
            # Real sharp data — use de-vigged p_win + fair line as median
            direction = "over"  # PP demons/goblins are always OVER
            p_win = best["fair_p_win_over"]
            # Use fair_line as the sharp consensus median
            # (fair line is the book's best estimate of the true median)
            median = best["fair_line"]

            # shape_params: empty — optimizer uses calibration dist_params
            result[pp.prop_id] = SharpConsensus(
                prop_id=pp.prop_id,
                median=median,
                shape_params={"fair_p_win_over": p_win, "fair_p_win_under": best["fair_p_win_under"]},
                timestamp=best["fetched_at"],
                books_used=best["books_used"],
                freshness_sec=0.0,
            )
            log.debug(
                f"[sharp] matched {pp.player_name} {pp.stat_type} "
                f"PP_line={pp_line} SGO_line={best['fair_line']} "
                f"p_win_over={p_win:.3f}"
            )
        else:
            # Fallback: calibration tier-delta
            result[pp.prop_id] = _fallback_sc(pp.prop_id, pp_line, tier, pp.stat_type)
            if candidates:
                log.debug(
                    f"[sharp] no line match {pp.player_name} {pp.stat_type} "
                    f"PP_line={pp_line} closest_diff={best_diff:.1f}"
                )
            else:
                log.debug(f"[sharp] no SGO data for {pp.player_name} {pp.stat_type}")

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Store read/write
# ─────────────────────────────────────────────────────────────────────────────
def _load_store() -> Dict[str, dict]:
    """Load sharp_store.json from disk. Returns {} if missing/corrupt."""
    if STORE_PATH.exists():
        try:
            with open(STORE_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_store(store: Dict[str, dict]) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(STORE_PATH, "w") as f:
        json.dump(store, f, indent=2)


def _sc_to_dict(sc: SharpConsensus) -> dict:
    return {
        "prop_id":      sc.prop_id,
        "median":       sc.median,
        "shape_params": sc.shape_params,
        "timestamp":    sc.timestamp,
        "books_used":   sc.books_used,
        "freshness_sec": sc.freshness_sec,
    }


def _sc_from_dict(d: dict) -> SharpConsensus:
    return SharpConsensus(
        prop_id=d["prop_id"],
        median=d["median"],
        shape_params=d.get("shape_params", {}),
        timestamp=d["timestamp"],
        books_used=d.get("books_used", []),
        freshness_sec=d.get("freshness_sec", FALLBACK_SEC),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────
def pull_sharp_consensus(league: str, pp_props: List[PPProp]) -> Dict[str, SharpConsensus]:
    """
    Fetch fresh SGO sharp data for a league, match to PP props, persist to disk.
    Called by the /api/pull route (same time as PP prop pull).

    Returns: prop_id → SharpConsensus
    """
    log.info(f"[sharp] pulling SGO for {league} ({len(pp_props)} PP props)")

    sgo_props = _fetch_sgo_props(league)
    consensus = _match_props(pp_props, sgo_props)

    # Merge into persistent store
    store = _load_store()
    for prop_id, sc in consensus.items():
        store[prop_id] = _sc_to_dict(sc)

    _save_store(store)

    matched = sum(1 for sc in consensus.values() if sc.freshness_sec == 0.0)
    log.info(f"[sharp] {matched}/{len(pp_props)} props matched to real SGO data")
    return consensus


def load_sharp_consensus(pp_props: List[PPProp]) -> Dict[str, SharpConsensus]:
    """
    Load sharp consensus from disk store.
    For any PP prop not in the store (or stale), returns a tier-aware fallback.

    Called by optimize.py — does NOT fetch from network.
    """
    store = _load_store()
    now_ts = time.time()

    result: Dict[str, SharpConsensus] = {}
    for pp in pp_props:
        tier = pp.tiers_offered[0] if pp.tiers_offered else Tier.STANDARD
        pp_line = pp.lines.get(tier, list(pp.lines.values())[0])

        stored = store.get(pp.prop_id)
        if stored:
            sc = _sc_from_dict(stored)
            # Check freshness: if stored timestamp is recent, use it
            try:
                stored_ts = datetime.fromisoformat(sc.timestamp.replace("Z", "+00:00")).timestamp()
                age_sec = now_ts - stored_ts
                if age_sec < FRESH_SEC:
                    result[pp.prop_id] = sc
                    continue
            except Exception:
                pass

        # Fallback
        result[pp.prop_id] = _fallback_sc(pp.prop_id, pp_line, tier, pp.stat_type)

    fresh_count = sum(1 for sc in result.values() if sc.freshness_sec < FALLBACK_SEC)
    log.info(f"[sharp] loaded {fresh_count}/{len(pp_props)} fresh sharp entries from store")
    return result
