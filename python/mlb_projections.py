#!/usr/bin/env python3
"""
mlb_projections.py — Real mu/sigma projections for GOTit.

Pulls two free sources (no API key):
  1. MLB Stats API  — season batting/pitching stat lines + game logs
  2. Baseball Savant — Statcast xStats (xBA, xSLG, hard_hit%, barrel%)

Builds per-player mu/sigma for every PP-relevant stat type.
Output: JSON dict { "PlayerName||StatType": { mu, sigma, n_games, source } }

Usage:
  python3 mlb_projections.py [--sport MLB] > projections_cache.json
  python3 mlb_projections.py --player "Aaron Judge" --stat "Total Bases"
"""

import csv
import io
import json
import logging
import math
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.request import urlopen, Request
from urllib.error import URLError

logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
log = logging.getLogger(__name__)

MLB_API  = "https://statsapi.mlb.com/api/v1"
SAVANT_XSTATS = (
    "https://baseballsavant.mlb.com/leaderboard/expected_statistics"
    "?type={type}&year={year}&position=&team=&min=20&csv=true"
)

CURRENT_YEAR = datetime.now(timezone.utc).year

# ── Stat-type → mu builder config ────────────────────────────────────────────
# Each entry: how to derive mu (expected value at the per-game level)
# from season stats.
# formula: lambda(season_stats, pa_per_game) → mu
# sigma_ratio: stat-specific dispersion (sigma = mu * ratio, floored at 0.3)

STAT_CONFIGS: Dict[str, Dict] = {
    "Total Bases": {
        "group":       "hitting",
        "formula":     lambda s, _: s.get("totalBases", 0) / max(s.get("gamesPlayed", 1), 1),
        "sigma_ratio": 0.65,
        "min_pa":      50,
    },
    "Hits": {
        "group":       "hitting",
        "formula":     lambda s, _: s.get("hits", 0) / max(s.get("gamesPlayed", 1), 1),
        "sigma_ratio": 0.70,
        "min_pa":      50,
    },
    "Hits+Runs+RBIs": {
        "group":       "hitting",
        "formula":     lambda s, _: (
            s.get("hits", 0) + s.get("runs", 0) + s.get("rbi", 0)
        ) / max(s.get("gamesPlayed", 1), 1),
        "sigma_ratio": 0.65,
        "min_pa":      50,
    },
    "Hitter Fantasy Score": {
        # PP FS = TB*3 + R*2 + RBI*2 + BB*2 + SB*5 - K*1
        "group":       "hitting",
        "formula":     lambda s, _: (
            s.get("totalBases", 0) * 3
            + s.get("runs", 0) * 2
            + s.get("rbi", 0) * 2
            + s.get("baseOnBalls", 0) * 2
            + s.get("stolenBases", 0) * 5
            - s.get("strikeOuts", 0) * 1
        ) / max(s.get("gamesPlayed", 1), 1),
        "sigma_ratio": 0.55,
        "min_pa":      50,
    },
    "Pitcher Strikeouts": {
        "group":       "pitching",
        "formula":     lambda s, _: s.get("strikeOuts", 0) / max(s.get("gamesStarted", 1), 1),
        "sigma_ratio": 0.38,
        "min_gs":      5,
    },
    "Pitches Thrown": {
        "group":       "pitching",
        "formula":     lambda s, _: s.get("pitchesThrown", 0) / max(s.get("gamesStarted", 1), 1)
                       if s.get("gamesStarted", 0) > 0 else 0,
        "sigma_ratio": 0.12,
        "min_gs":      5,
    },
    "Pitching Outs": {
        "group":       "pitching",
        "formula":     lambda s, _: s.get("outs", 0) / max(s.get("gamesStarted", 1), 1)
                       if s.get("gamesStarted", 0) > 0 else 0,
        "sigma_ratio": 0.30,
        "min_gs":      5,
    },
    "Hits Allowed": {
        "group":       "pitching",
        "formula":     lambda s, _: s.get("hits", 0) / max(s.get("gamesStarted", 1), 1),
        "sigma_ratio": 0.50,
        "min_gs":      5,
    },
    "Earned Runs Allowed": {
        "group":       "pitching",
        "formula":     lambda s, _: s.get("earnedRuns", 0) / max(s.get("gamesStarted", 1), 1),
        "sigma_ratio": 0.75,
        "min_gs":      5,
    },
    "Walks Allowed": {
        "group":       "pitching",
        "formula":     lambda s, _: s.get("baseOnBalls", 0) / max(s.get("gamesStarted", 1), 1),
        "sigma_ratio": 0.70,
        "min_gs":      3,
    },
    "Pitcher Fantasy Score": {
        # PP FS = K*4 + IP*2.25 - ER*2 - BB*1
        "group":       "pitching",
        "formula":     lambda s, _: (
            s.get("strikeOuts", 0) * 4
            + (s.get("outs", 0) / 3.0) * 2.25
            - s.get("earnedRuns", 0) * 2
            - s.get("baseOnBalls", 0) * 1
        ) / max(s.get("gamesStarted", 1), 1),
        "sigma_ratio": 0.40,
        "min_gs":      5,
    },
    "Hitter Strikeouts": {
        "group":       "hitting",
        "formula":     lambda s, _: s.get("strikeOuts", 0) / max(s.get("gamesPlayed", 1), 1),
        "sigma_ratio": 0.55,
        "min_pa":      30,
    },
    "Runs": {
        "group":       "hitting",
        "formula":     lambda s, _: s.get("runs", 0) / max(s.get("gamesPlayed", 1), 1),
        "sigma_ratio": 0.80,
        "min_pa":      30,
    },
    "RBIs": {
        "group":       "hitting",
        "formula":     lambda s, _: s.get("rbi", 0) / max(s.get("gamesPlayed", 1), 1),
        "sigma_ratio": 0.80,
        "min_pa":      30,
    },
    "Home Runs": {
        "group":       "hitting",
        "formula":     lambda s, _: s.get("homeRuns", 0) / max(s.get("gamesPlayed", 1), 1),
        "sigma_ratio": 1.20,
        "min_pa":      30,
    },
    "Stolen Bases": {
        "group":       "hitting",
        "formula":     lambda s, _: s.get("stolenBases", 0) / max(s.get("gamesPlayed", 1), 1),
        "sigma_ratio": 1.50,
        "min_pa":      20,
    },
    "Plate Appearances": {
        "group":       "hitting",
        "formula":     lambda s, _: s.get("plateAppearances", 0) / max(s.get("gamesPlayed", 1), 1),
        "sigma_ratio": 0.25,
        "min_pa":      30,
    },
}


def _get(url: str, headers: Dict = {}) -> Optional[bytes]:
    try:
        req = Request(url, headers={"User-Agent": "GOTit/1.0", **headers})
        with urlopen(req, timeout=15) as r:
            return r.read()
    except Exception as e:
        log.warning("GET %s failed: %s", url, e)
        return None


def _normalize(name: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", name.lower()).strip()


# ─────────────────────────────────────────────────────────────────────────────
# Source 1: MLB Stats API — season hitting/pitching stats
# ─────────────────────────────────────────────────────────────────────────────

def fetch_mlb_season_stats(year: int = CURRENT_YEAR) -> Tuple[Dict, Dict]:
    """
    Returns (hitting_by_name, pitching_by_name) dicts.
    hitting_by_name[normalized_name] = { gamesPlayed, hits, totalBases, runs, rbi, ... }
    """
    hitting:  Dict[str, Dict] = {}
    pitching: Dict[str, Dict] = {}

    # Hitting — playerPool=All gets every player with AB, not just qualified
    url = (f"{MLB_API}/stats?stats=season&group=hitting&gameType=R"
           f"&season={year}&sportId=1&limit=2000&offset=0&playerPool=All")
    raw = _get(url)
    if raw:
        try:
            data = json.loads(raw)
            for split in data.get("stats", [{}])[0].get("splits", []):
                name = split.get("player", {}).get("fullName", "")
                if not name:
                    continue
                s = split.get("stat", {})
                s["gamesPlayed"] = s.get("gamesPlayed", 0)
                hitting[_normalize(name)] = {**s, "_name": name}
        except Exception as e:
            log.warning("MLB hitting stats parse error: %s", e)

    # Pitching — playerPool=All gets relievers + starters
    url = (f"{MLB_API}/stats?stats=season&group=pitching&gameType=R"
           f"&season={year}&sportId=1&limit=2000&offset=0&playerPool=All")
    raw = _get(url)
    if raw:
        try:
            data = json.loads(raw)
            for split in data.get("stats", [{}])[0].get("splits", []):
                name = split.get("player", {}).get("fullName", "")
                if not name:
                    continue
                s = split.get("stat", {})
                pitching[_normalize(name)] = {**s, "_name": name}
        except Exception as e:
            log.warning("MLB pitching stats parse error: %s", e)

    log.info("MLB Stats API: %d hitters, %d pitchers", len(hitting), len(pitching))
    return hitting, pitching


# ─────────────────────────────────────────────────────────────────────────────
# Source 2: Baseball Savant — xStats (xBA, xSLG, hard_hit%, barrel%)
# ─────────────────────────────────────────────────────────────────────────────

def fetch_savant_xstats(year: int = CURRENT_YEAR) -> Dict[str, Dict]:
    """
    Returns xstats_by_name[normalized_name] = { xba, xslg, xwoba, hard_hit_pct, barrel_pct, pa }
    """
    result: Dict[str, Dict] = {}
    for player_type in ("batter",):
        url = SAVANT_XSTATS.format(type=player_type, year=year)
        raw = _get(url)
        if not raw:
            continue
        try:
            text = raw.decode("utf-8-sig", errors="replace")  # strips BOM
            reader = csv.DictReader(io.StringIO(text))
            # Normalize column names: strip quotes, spaces, BOM
            fieldnames = [f.strip().strip('"') for f in (reader.fieldnames or [])]
            for row in reader:
                # Re-key row with clean names
                clean_row = {}
                for k, v in row.items():
                    ck = k.strip().strip('"') if k else k
                    clean_row[ck] = v

                # Name is "Last, First" in first two columns OR combined
                last  = clean_row.get("last_name", "").strip()
                first = clean_row.get("first_name", "").strip()
                if last and first:
                    name = f"{first} {last}"
                else:
                    combined = clean_row.get("last_name, first_name", "")
                    if "," in combined:
                        parts = [p.strip() for p in combined.split(",", 1)]
                        name = f"{parts[1]} {parts[0]}"
                    else:
                        name = combined.strip()
                if not name:
                    continue
                key = _normalize(name)
                try:
                    result[key] = {
                        "_name":        name,
                        # 2026 Savant uses est_ba / est_slg / est_woba
                        "xba":          float(clean_row.get("est_ba")  or clean_row.get("xba")  or 0),
                        "xslg":         float(clean_row.get("est_slg") or clean_row.get("xslg") or 0),
                        "xwoba":        float(clean_row.get("est_woba")or clean_row.get("xwoba")or 0),
                        "hard_hit_pct": float(clean_row.get("hard_hit_percent") or 0),
                        "barrel_pct":   float(clean_row.get("barrel_batted_rate") or clean_row.get("brl_percent") or 0),
                        "pa":           int(float(clean_row.get("pa") or 0)),
                    }
                except (ValueError, TypeError):
                    continue
        except Exception as e:
            log.warning("Savant xstats parse error: %s", e)

    log.info("Savant xStats: %d batters", len(result))
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Build mu/sigma projections
# ─────────────────────────────────────────────────────────────────────────────

def _xstats_mu_adjustment(xstats: Dict, stat_type: str, base_mu: float) -> float:
    """
    Use Statcast xStats to adjust mu for hitter stats.
    xSLG is a strong proxy for total bases production.
    """
    if not xstats or base_mu <= 0:
        return base_mu

    xslg = xstats.get("xslg", 0)
    hard = xstats.get("hard_hit_pct", 0)
    barrel = xstats.get("barrel_pct", 0)

    if stat_type == "Total Bases":
        # xSLG ≈ TB/AB; adjust mu using xSLG vs league avg xSLG (≈0.400)
        league_xslg = 0.400
        if xslg > 0:
            adj = (xslg / league_xslg) ** 0.5  # square root dampens extremes
            return base_mu * adj

    elif stat_type in ("Hitter Fantasy Score", "Hits+Runs+RBIs"):
        # Hard hit% above league avg (≈38%) signals upside
        league_hard = 38.0
        if hard > 0:
            adj = 1.0 + (hard - league_hard) / 100.0 * 0.5
            return base_mu * max(0.7, min(1.4, adj))

    elif stat_type == "Hits":
        xba = xstats.get("xba", 0)
        league_xba = 0.248
        if xba > 0:
            adj = (xba / league_xba) ** 0.6
            return base_mu * adj

    return base_mu


def build_projections(
    hitting:  Dict[str, Dict],
    pitching: Dict[str, Dict],
    xstats:   Dict[str, Dict],
) -> Dict[str, Dict]:
    """
    Build { "PlayerName||StatType": { mu, sigma, n_games, source, ... } }
    """
    projections: Dict[str, Dict] = {}

    for stat_type, cfg in STAT_CONFIGS.items():
        group = cfg["group"]
        source_dict = hitting if group == "hitting" else pitching
        min_pa = cfg.get("min_pa", 0)
        min_gs = cfg.get("min_gs", 0)

        for norm_name, stats in source_dict.items():
            real_name = stats.get("_name", norm_name)

            # Sample size gate
            if group == "hitting":
                n = stats.get("gamesPlayed", 0)
                if n < max(5, min_pa // 4):
                    continue
            else:
                gs = stats.get("gamesStarted", 0)
                if gs < min_gs:
                    continue
                n = gs

            # Compute base mu
            try:
                base_mu = cfg["formula"](stats, None)
            except (ZeroDivisionError, TypeError):
                continue

            if base_mu <= 0:
                continue

            # xStats adjustment for hitters
            xs = xstats.get(norm_name, {})
            mu = _xstats_mu_adjustment(xs, stat_type, base_mu)

            # Sigma
            ratio = cfg["sigma_ratio"]
            sigma = max(0.3, mu * ratio)

            key = f"{real_name}||{stat_type}"
            projections[key] = {
                "player_name": real_name,
                "stat_type":   stat_type,
                "mu":          round(mu, 4),
                "sigma":       round(sigma, 4),
                "n_games":     n,
                "base_mu":     round(base_mu, 4),
                "xslg":        round(xs.get("xslg", 0), 4),
                "hard_hit_pct":round(xs.get("hard_hit_pct", 0), 2),
                "barrel_pct":  round(xs.get("barrel_pct", 0), 2),
                "source":      "mlb_stats_api+savant" if xs else "mlb_stats_api",
                "built_at":    datetime.now(timezone.utc).isoformat(),
            }

    log.info("Built %d projections", len(projections))
    return projections


# ─────────────────────────────────────────────────────────────────────────────
# Disk cache — avoids re-fetching MLB Stats API + Savant on every pull
# Cache is valid for the calendar day (Central time). Stored in /tmp.
# ─────────────────────────────────────────────────────────────────────────────

import tempfile

def _cache_path(year: int) -> str:
    today = datetime.now(timezone(timedelta(hours=-5))).strftime("%Y%m%d")  # CDT approx
    return os.path.join(tempfile.gettempdir(), f"gotit_proj_{year}_{today}.json")


def run(year: int = CURRENT_YEAR) -> Dict[str, Dict]:
    cache = _cache_path(year)
    # Try disk cache first
    try:
        if os.path.exists(cache):
            mtime = os.path.getmtime(cache)
            age_hours = (time.time() - mtime) / 3600
            if age_hours < 23:  # fresh enough
                with open(cache) as f:
                    proj = json.load(f)
                log.info("Projection cache hit: %d entries (%.1fh old)", len(proj), age_hours)
                return proj
    except Exception as e:
        log.warning("Cache read failed: %s", e)

    log.info("Fetching MLB season stats for %d...", year)
    hitting, pitching = fetch_mlb_season_stats(year)

    log.info("Fetching Savant xStats for %d...", year)
    xstats = fetch_savant_xstats(year)

    log.info("Building projections...")
    proj = build_projections(hitting, pitching, xstats)

    # Write to disk cache
    try:
        with open(cache, 'w') as f:
            json.dump(proj, f)
        log.info("Projection cache written: %s (%d entries)", cache, len(proj))
    except Exception as e:
        log.warning("Cache write failed: %s", e)

    return proj


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, default=CURRENT_YEAR)
    parser.add_argument("--player", type=str, default=None)
    parser.add_argument("--stat",   type=str, default=None)
    parser.add_argument("--out",    type=str, default=None, help="Save to file")
    args = parser.parse_args()

    logging.getLogger().setLevel(logging.INFO)
    proj = run(args.year)

    if args.player and args.stat:
        key = f"{args.player}||{args.stat}"
        match = proj.get(key)
        if not match:
            # Fuzzy
            norm = _normalize(args.player)
            for k, v in proj.items():
                if _normalize(v["player_name"]) == norm and v["stat_type"] == args.stat:
                    match = v
                    break
        print(json.dumps(match or {"error": "not found"}, indent=2))
    else:
        output = proj
        if args.out:
            with open(args.out, "w") as f:
                json.dump(output, f, indent=2)
            print(f"Saved {len(output)} projections to {args.out}", file=sys.stderr)
        else:
            print(json.dumps(output))
