#!/usr/bin/env python3
"""
GOTit Optimizer — subprocess entry point called by Express /api/optimize.

Input  (stdin): JSON array of web-app props (from Supabase)
Output (stdout): JSON { game_id: { six_legs, two_demons, meta } }

Data flow (PURE — no reimplementation):
  1. Convert web-app prop dicts → PPProp objects
  2. Load real SharpConsensus from sharp_store.json
     → sc.median = SGO fairOverUnder  (sharp consensus line, the oracle)
     → sc.shape_params = {}  (CDF owns p_win, not odds)
  3. Call select_legs_for_slate(pp_props, sharp_consensus, calibration, ...)
     This is the ONLY place p_win is computed and all hard filters applied.
  4. Output the result.

DO NOT compute p_win here. DO NOT call MILP here.
select_legs_for_slate does all of that faithfully.
"""
import sys
import json
import hashlib
import logging
import os
import urllib.request
from pathlib import Path
from typing import Dict, List

logging.basicConfig(level=logging.WARNING)
sys.path.insert(0, str(Path(__file__).parent))

from gotit.leg_selector import (
    PPProp,
    Tier,
    Direction,
    CalibrationParams,
    get_default_calibration,
    select_legs_for_slate,
)
from gotit.sharp_consensus import load_sharp_consensus


# ─────────────────────────────────────────────────────────────────────────────
# 1. Calibration loader
# ─────────────────────────────────────────────────────────────────────────────
def load_calibration() -> CalibrationParams:
    cal_path = Path(__file__).parent / "config" / "calibration_latest.json"
    if cal_path.exists():
        with open(cal_path) as f:
            data = json.load(f)
        try:
            return CalibrationParams(**data)
        except Exception as e:
            logging.warning(f"calibration load failed: {e}, using default")
    return get_default_calibration()


# ─────────────────────────────────────────────────────────────────────────────
# 2. Prop conversion  (web-app dict → PPProp)
# ─────────────────────────────────────────────────────────────────────────────
def _player_id(name: str) -> str:
    return hashlib.md5(name.lower().encode()).hexdigest()[:16]


def _prop_id(d: dict) -> str:
    pid = d.get("propId") or d.get("id") or d.get("sourcePropId")
    if pid:
        return str(pid)
    key = f"{d.get('playerName','')}-{d.get('statType','')}-{d.get('gameId','')}"
    return hashlib.md5(key.encode()).hexdigest()[:16]


def _tier(d: dict) -> Tier:
    if d.get("isDemon"):
        return Tier.DEMON
    if d.get("isGoblin"):
        return Tier.GOBLIN
    raw = (d.get("tier") or "").lower()
    if raw == "demon":
        return Tier.DEMON
    if raw == "goblin":
        return Tier.GOBLIN
    return Tier.STANDARD


# ─────────────────────────────────────────────────────────────────────────────
# 2b. Player performance loader (learning loop)
# ─────────────────────────────────────────────────────────────────────────────
def load_player_performance() -> Dict[str, dict]:
    """
    Fetch all player_performance rows from Supabase.
    Returns a dict keyed by  "playerName::statType::league".
    Falls back to empty dict on any error so scoring is unaffected.
    """
    url   = os.environ.get('SUPABASE_URL', 'https://iikjgxnjmyzlivaukabc.supabase.co')
    key   = os.environ.get('SUPABASE_ANON_KEY', 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imlpa2pneG5qbXl6bGl2YXVrYWJjIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODM1NDg1NjgsImV4cCI6MjA5OTEyNDU2OH0.IFY9ocTpySWvyGXyUt615bkpwDs634T1wRUu97WbyTg')
    try:
        req = urllib.request.Request(
            f"{url}/rest/v1/player_performance?select=player_name,stat_type,league,hit_count,miss_count,last_5,avg_margin",
            headers={'apikey': key, 'Authorization': f'Bearer {key}'},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            rows = json.loads(resp.read())
        perf_map: Dict[str, dict] = {}
        for r in rows:
            k = f"{r['player_name']}::{r['stat_type']}::{r['league']}"
            try:
                last5 = json.loads(r.get('last_5') or '[]')
            except Exception:
                last5 = []
            perf_map[k] = {
                'hitCount':  r.get('hit_count', 0),
                'missCount': r.get('miss_count', 0),
                'last5':     last5,
                'avgMargin': r.get('avg_margin'),
            }
        logging.info(f"[learning] loaded {len(perf_map)} player performance records")
        return perf_map
    except Exception as e:
        logging.warning(f"[learning] could not load player_performance: {e}")
        return {}


def build_pp_prop(d: dict) -> PPProp:
    """Convert web-app prop dict to PPProp. Called by both optimize.py and sharp_pull.py."""
    from gotit.leg_selector import Direction as Dir
    tier = _tier(d)
    line = float(d.get("lineScore") or d.get("line_score") or 0.5)
    raw_dir = (d.get("direction") or "over").strip().upper()
    stored_dir = Dir.UNDER if raw_dir == "UNDER" else Dir.OVER
    return PPProp(
        prop_id=_prop_id(d),
        game_id=d.get("gameId") or d.get("game_id") or "unknown",
        player_id=_player_id(d.get("playerName") or d.get("player_name") or ""),
        player_name=d.get("playerName") or d.get("player_name") or "",
        stat_type=d.get("statType") or d.get("stat_type") or "",
        tiers_offered=[tier],
        lines={tier: line},
        hours_to_lock=4.0,
        public_over_pct=None,
        dnp_prob=0.0,
        correlation_partners=[],
        stored_direction=stored_dir,
        perf=d.get('_perf'),  # injected by main() from player_performance
    )


# ─────────────────────────────────────────────────────────────────────────────
# 3. Main
# ─────────────────────────────────────────────────────────────────────────────
def main():
    raw = sys.stdin.read().strip()
    if not raw:
        print(json.dumps({"error": "empty input"}))
        sys.exit(1)

    try:
        props_data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"json parse: {e}"}))
        sys.exit(1)

    if not props_data:
        print(json.dumps({"error": "no props"}))
        sys.exit(1)

    # ── Load calibration ────────────────────────────────────────────────────
    cal = load_calibration()

    # ── Load player performance (learning loop) ─────────────────────────────
    # Determine league from the first prop so we look up the right rows
    first_league = (props_data[0].get('league') or '').upper() if props_data else ''
    perf_map = load_player_performance()

    # ── Build PPProp list ───────────────────────────────────────────────────
    pp_props: List[PPProp] = []
    for d in props_data:
        try:
            # Inject performance record for this player+stat+league
            player  = d.get('playerName') or d.get('player_name') or ''
            stat    = d.get('statType')   or d.get('stat_type')   or ''
            league  = (d.get('league') or first_league).upper()
            perf_key = f"{player}::{stat}::{league}"
            d['_perf'] = perf_map.get(perf_key)  # None if no history yet
            pp_props.append(build_pp_prop(d))
        except Exception as e:
            logging.warning(f"skip prop {d.get('playerName','?')}: {e}")

    if not pp_props:
        print(json.dumps({"error": "no valid props after conversion"}))
        sys.exit(1)

    # ── Load SharpConsensus from store (written by /api/pull → sharp_pull.py) ──
    # sc.median = SGO fairOverUnder  ← this is what anchors the CDF
    # sc.shape_params = {}           ← CDF computes p_win, not raw odds
    sc_map = load_sharp_consensus(pp_props)

    # ── Call the pure original pipeline ────────────────────────────────────
    # select_legs_for_slate handles ALL of:
    #   • _calibrated_p_win with anchored median + stat-family CDF
    #   • micro-line safety cap
    #   • Demon extra floor (p_win >= r*+0.03)
    #   • hard filter p_win < r*-0.02
    #   • DNP model gate
    #   • pre-filter MAX_SHAPLEY per game (demon-slot reservation)
    #   • Shapley marginal EV per-game
    #   • Gaussian-copula correlation adjustment
    #   • MILP (OR-Tools SCIP) — exactly 6 legs, 2 distinct-player demons
    #   • calibration hash verification
    output = select_legs_for_slate(
        pp_props=pp_props,
        sharp_consensus=sc_map,
        calibration=cal,
        game_scripts={},    # no game-script model yet
        rho_map={},         # no correlation map yet — defaults to 0 (independent)
        dnp_model={},       # no DNP model yet — all props pass 0.15 gate
    )

    if not output:
        print(json.dumps({"error": "no feasible games — need ≥6 candidates + ≥2 demon players per game"}))
        sys.exit(0)

    print(json.dumps(output))


if __name__ == "__main__":
    main()
