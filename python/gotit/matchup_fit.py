#!/usr/bin/env python3
"""
matchup_fit.py — Platoon-split matchup fit layer (Phase 1).

Pre-slate pull (runs inside sharp_pull, before scoring):
  1. Today's MLB schedule → probable pitchers per team
  2. For every batter with active props: vs-L / vs-R split stats (OPS)
     from MLB Stats API statSplits (sitCodes vr / vl)
  3. fit_score        = (split_value − season_value) / season_value
     fit_score_shrunk = fit_score × (PA / (PA + K))      K = 80
  4. Rows upserted to Supabase matchup_fit_scores
  5. Returns {normalized_player_name: fit_dict} for enrichment stamping

Phase 2 (NOT implemented): pitch_mix_fit_score via pybaseball statcast —
column stays NULL until platoon layer is validated on 200–300 logged picks.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

MLB_API = "https://statsapi.mlb.com/api/v1"
CURRENT_YEAR = datetime.now(timezone.utc).year

# Shrinkage constant (PA needed before split is ~50% trusted). Tune later: 60–100.
SHRINK_K = 75

SUPABASE_URL = os.environ.get('SUPABASE_URL', 'https://iikjgxnjmyzlivaukabc.supabase.co')
SUPABASE_KEY = os.environ.get('SUPABASE_ANON_KEY', 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imlpa2pneG5qbXl6bGl2YXVrYWJjIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODM1NDg1NjgsImV4cCI6MjA5OTEyNDU2OH0.IFY9ocTpySWvyGXyUt615bkpwDs634T1wRUu97WbyTg')

_CACHE_FILE = Path(__file__).parent / "matchup_fit_store.json"
_CACHE_TTL_SEC = 6 * 3600  # re-pull splits at most every 6h


def _normalize(name: str) -> str:
    import unicodedata
    s = unicodedata.normalize('NFKD', name).encode('ascii', 'ignore').decode()
    return ' '.join(s.lower().replace('.', '').replace('-', ' ').split())


def _get_json(url: str, timeout: int = 20) -> Optional[dict]:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "gotit/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception as e:
        log.warning("[matchup_fit] GET failed %s: %s", url[:100], e)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Schedule → probable pitchers
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_probables() -> Dict[str, dict]:
    """
    Returns {team_abbr: {"opp_pitcher_id": int, "game_id": str}} for today.
    The pitcher stored per team is the OPPOSING probable starter that team faces.
    """
    # team id → abbreviation
    teams_raw = _get_json(f"{MLB_API}/teams?sportId=1&season={CURRENT_YEAR}")
    abbr: Dict[int, str] = {}
    for t in (teams_raw or {}).get("teams", []):
        abbr[t["id"]] = t.get("abbreviation", "")

    today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    sched = _get_json(f"{MLB_API}/schedule?sportId=1&date={today}&hydrate=probablePitcher")
    out: Dict[str, dict] = {}
    for d in (sched or {}).get("dates", []):
        for g in d.get("games", []):
            game_pk = str(g.get("gamePk", ""))
            home = g.get("teams", {}).get("home", {})
            away = g.get("teams", {}).get("away", {})
            home_abbr = abbr.get(home.get("team", {}).get("id"), "")
            away_abbr = abbr.get(away.get("team", {}).get("id"), "")
            home_pp = (home.get("probablePitcher") or {}).get("id")
            away_pp = (away.get("probablePitcher") or {}).get("id")
            # home batters face the away probable, and vice versa
            if home_abbr and away_pp:
                out[home_abbr] = {"opp_pitcher_id": away_pp, "game_id": game_pk}
            if away_abbr and home_pp:
                out[away_abbr] = {"opp_pitcher_id": home_pp, "game_id": game_pk}
    log.info("[matchup_fit] probables mapped for %d teams", len(out))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Player resolution + splits
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_player_ids() -> Dict[str, int]:
    """normalized name → MLB person id (all hitters with an AB this season)."""
    out: Dict[str, int] = {}
    url = (f"{MLB_API}/stats?stats=season&group=hitting&gameType=R"
           f"&season={CURRENT_YEAR}&sportId=1&limit=2000&offset=0&playerPool=All")
    data = _get_json(url)
    for split in (data or {}).get("stats", [{}])[0].get("splits", []):
        p = split.get("player", {})
        if p.get("id") and p.get("fullName"):
            out[_normalize(p["fullName"])] = p["id"]
    return out


def _chunk(lst: list, n: int):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def _fetch_splits(person_ids: List[int]) -> Dict[int, dict]:
    """
    person_id → {
      "hand": batSide code (L/R/S),
      "season_ops": float, "vl_ops": float, "vl_pa": int,
      "vr_ops": float, "vr_pa": int,
    }
    """
    out: Dict[int, dict] = {}
    for chunk in _chunk(person_ids, 40):
        ids = ",".join(str(i) for i in chunk)
        url = (f"{MLB_API}/people?personIds={ids}"
               f"&hydrate=stats(group=[hitting],type=[statSplits,season],"
               f"sitCodes=[vr,vl],season={CURRENT_YEAR})")
        data = _get_json(url, timeout=30)
        for person in (data or {}).get("people", []):
            pid = person.get("id")
            rec: dict = {"hand": (person.get("batSide") or {}).get("code", "")}
            for block in person.get("stats", []):
                btype = (block.get("type") or {}).get("displayName", "")
                for sp in block.get("splits", []):
                    st = sp.get("stat", {})
                    ops = st.get("ops")
                    pa  = st.get("plateAppearances", 0)
                    try:
                        ops = float(ops) if ops is not None else None
                    except (TypeError, ValueError):
                        ops = None
                    if btype == "season":
                        rec["season_ops"] = ops
                    elif btype == "statSplits":
                        code = (sp.get("split") or {}).get("code", "")
                        if code == "vl":
                            rec["vl_ops"], rec["vl_pa"] = ops, int(pa or 0)
                        elif code == "vr":
                            rec["vr_ops"], rec["vr_pa"] = ops, int(pa or 0)
            if pid:
                out[pid] = rec
    return out


def _fetch_pitcher_hands(pitcher_ids: List[int]) -> Dict[int, str]:
    out: Dict[int, str] = {}
    for chunk in _chunk(list(set(pitcher_ids)), 60):
        ids = ",".join(str(i) for i in chunk)
        data = _get_json(f"{MLB_API}/people?personIds={ids}")
        for person in (data or {}).get("people", []):
            out[person["id"]] = (person.get("pitchHand") or {}).get("code", "")
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Fit score
# ─────────────────────────────────────────────────────────────────────────────

def _fit_score(season: Optional[float], split: Optional[float], pa: int) -> Optional[float]:
    if season is None or split is None or season <= 0:
        return None
    raw = (split - season) / season
    shrunk = raw * (pa / (pa + SHRINK_K))
    return round(shrunk, 4)


# ─────────────────────────────────────────────────────────────────────────────
# Supabase upsert
# ─────────────────────────────────────────────────────────────────────────────

def _upsert_rows(rows: List[dict]) -> None:
    if not rows:
        return
    try:
        body = json.dumps(rows).encode()
        req = urllib.request.Request(
            f"{SUPABASE_URL}/rest/v1/matchup_fit_scores",
            data=body, method="POST",
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
                "Content-Type": "application/json",
                "Prefer": "return=minimal",
            })
        urllib.request.urlopen(req, timeout=30).read()
        log.info("[matchup_fit] upserted %d rows", len(rows))
    except Exception as e:
        log.warning("[matchup_fit] supabase upsert failed: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# Public entry
# ─────────────────────────────────────────────────────────────────────────────

def pull_matchup_fits(props: List[dict], league: str = "MLB") -> Dict[str, dict]:
    """
    Main entry — called from sharp_pull BEFORE scoring.
    props: webapp prop dicts (playerName, statType, teamAbbr, ...)
    Returns {normalized_player_name: {"matchup_fit_score": float, ...}}
    """
    if league.upper() != "MLB":
        return {}

    batter_names = sorted({
        _normalize(str(p.get('playerName') or p.get('player_name') or ''))
        for p in props
        if (p.get('playerName') or p.get('player_name'))
    })

    # Disk cache (6h TTL) — avoids re-hitting statsapi on every manual pull.
    # Only valid if the cached pull covered every player in this request
    # (a small test pull must not satisfy a full-slate pull).
    try:
        if _CACHE_FILE.exists():
            cached = json.loads(_CACHE_FILE.read_text())
            fresh = time.time() - cached.get("_ts", 0) < _CACHE_TTL_SEC
            covered = set(batter_names) <= set(cached.get("requested", []))
            if fresh and covered:
                log.info("[matchup_fit] using cached fits (%d players)", len(cached.get("fits", {})))
                return cached.get("fits", {})
    except Exception:
        pass
    teams_by_player: Dict[str, str] = {}
    for p in props:
        nm = _normalize(str(p.get('playerName') or p.get('player_name') or ''))
        ab = str(p.get('teamAbbr') or p.get('team_abbr') or '')
        if nm and ab and nm not in teams_by_player:
            teams_by_player[nm] = ab

    stat_by_player: Dict[str, str] = {}
    for p in props:
        nm = _normalize(str(p.get('playerName') or p.get('player_name') or ''))
        if nm and nm not in stat_by_player:
            stat_by_player[nm] = str(p.get('statType') or p.get('stat_type') or '')

    probables = _fetch_probables()
    name_to_id = _fetch_player_ids()

    # Resolve batters that have both an MLB id and a probable opposing pitcher
    targets: Dict[str, dict] = {}
    for nm in batter_names:
        pid = name_to_id.get(nm)
        team = teams_by_player.get(nm, "")
        prob = probables.get(team)
        if pid and prob:
            targets[nm] = {"pid": pid, **prob}

    if not targets:
        log.info("[matchup_fit] no batters resolved (%d names tried)", len(batter_names))
        return {}

    splits = _fetch_splits([t["pid"] for t in targets.values()])
    hands = _fetch_pitcher_hands([t["opp_pitcher_id"] for t in targets.values()])

    now_iso = datetime.now(timezone.utc).isoformat()
    fits: Dict[str, dict] = {}
    rows: List[dict] = []

    for nm, t in targets.items():
        rec = splits.get(t["pid"])
        if not rec:
            continue
        p_hand = hands.get(t["opp_pitcher_id"], "")
        if p_hand == "L":
            split_ops, pa = rec.get("vl_ops"), rec.get("vl_pa", 0)
        elif p_hand == "R":
            split_ops, pa = rec.get("vr_ops"), rec.get("vr_pa", 0)
        else:
            continue
        season_ops = rec.get("season_ops")
        score = _fit_score(season_ops, split_ops, pa)
        if score is None:
            continue

        fits[nm] = {
            "matchup_fit_score": score,
            "batter_hand": rec.get("hand", ""),
            "pitcher_hand": p_hand,
            "sample_size_pa": pa,
        }
        rows.append({
            "player_id": str(t["pid"]),
            "game_id": t["game_id"],
            "opponent_pitcher_id": str(t["opp_pitcher_id"]),
            "stat_type": stat_by_player.get(nm, ""),
            "batter_hand": rec.get("hand", ""),
            "pitcher_hand": p_hand,
            "season_baseline_value": season_ops,
            "split_specific_value": split_ops,
            "sample_size_pa": pa,
            "pitch_mix_fit_score": None,   # Phase 2 — stays NULL until platoon layer validated
            "matchup_fit_score": score,
            "data_source": "mlb_stats_api",
            "pulled_at": now_iso,
        })

    _upsert_rows(rows)

    try:
        _CACHE_FILE.write_text(json.dumps({"_ts": time.time(), "requested": batter_names, "fits": fits}))
    except Exception:
        pass

    log.info("[matchup_fit] computed fits for %d batters", len(fits))
    return fits
