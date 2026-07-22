#!/usr/bin/env python3
"""
GOTit Demon Qualifier — subprocess entry point called by Express per game.

Input  (stdin): JSON array of ALL props for ONE game (web-app prop dicts)
Output (stdout): JSON array of exactly top-2 qualified demon prop dicts,
                 with demonScore attached. Empty array if none qualify.

Rules (per spec):
  - PP is the only authority on demon identity (isDemon=true)
  - GOTit applies 4 elimination gates via qualify_demons()
  - Returns top 2 distinct-player demons only
  - If fewer than 2 survive, returns fewer — no substitutions
"""
from __future__ import annotations
import sys, json, logging
from pathlib import Path
from typing import Dict, List

logging.basicConfig(level=logging.WARNING)
sys.path.insert(0, str(Path(__file__).parent))

from gotit.leg_selector import (
    PPProp, Tier, Direction,
    select_legs_for_slate,
    get_default_calibration,
)
from gotit.sharp_consensus import load_sharp_consensus


# ── Helpers ───────────────────────────────────────────────────────────────────
def _prop_id(d: dict) -> str:
    pid = d.get('id') or d.get('prop_id') or ''
    if pid: return str(pid)
    import hashlib
    key = f"{d.get('playerName','')}{d.get('statType','')}{d.get('lineScore','')}{d.get('gameId','')}"
    return hashlib.md5(key.encode()).hexdigest()[:12]

def _player_id(name: str) -> str:
    import hashlib
    return hashlib.md5(name.lower().encode()).hexdigest()[:8]

def _tier(d: dict) -> Tier:
    if d.get('isDemon'): return Tier.DEMON
    if d.get('isGoblin'): return Tier.GOBLIN
    return Tier.STANDARD

def _build_pp_prop(d: dict) -> PPProp:
    tier = _tier(d)
    line = float(d.get('lineScore') or 0.5)
    return PPProp(
        prop_id=_prop_id(d),
        game_id=d.get('gameId') or 'unknown',
        player_id=_player_id(d.get('playerName') or ''),
        player_name=d.get('playerName') or '',
        stat_type=d.get('statType') or '',
        tiers_offered=[tier],
        lines={tier: line},
        hours_to_lock=4.0,
        public_over_pct=None,
        dnp_prob=0.0,
        correlation_partners=[],
        stored_direction=Direction.OVER,  # demons always over
        perf=None,
    )


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    raw = sys.stdin.read().strip()
    if not raw:
        print(json.dumps([])); sys.exit(0)

    try:
        props_data = json.loads(raw)
    except Exception as e:
        print(json.dumps({'error': str(e)})); sys.exit(1)

    # Keep only demon props for this game
    demon_data = [d for d in props_data if d.get('isDemon')]
    if not demon_data:
        print(json.dumps([])); sys.exit(0)

    # Build PPProps
    pp_props = [_build_pp_prop(d) for d in props_data]  # pass all so SC loads correctly
    demon_ids = {_prop_id(d) for d in demon_data}

    # Load sharp consensus
    try:
        sc_map = load_sharp_consensus(pp_props)
    except Exception:
        sc_map = {}

    # Run the full optimizer — it runs qualify_demons internally
    cal = get_default_calibration()
    try:
        result = select_legs_for_slate(pp_props, sc_map, cal)
    except Exception as e:
        logging.warning(f'select_legs_for_slate failed: {e}')
        print(json.dumps([])); sys.exit(0)

    # Extract demons from optimizer output — already top-2 per game, ranked
    id_to_orig = {_prop_id(d): d for d in demon_data}
    output = []

    game_ids = {_prop_id(d): d.get('gameId','unknown') for d in props_data}
    game_id = demon_data[0].get('gameId', 'unknown') if demon_data else 'unknown'

    game_result = result.get(game_id, {})
    two_demons = game_result.get('two_demons', [])

    seen_players: set = set()
    for leg in two_demons:
        pid = leg.get('prop_id', '')
        pname = leg.get('player_name', '')
        if pname in seen_players:
            continue
        seen_players.add(pname)
        orig = id_to_orig.get(pid) or next(
            (d for d in demon_data if d.get('playerName') == pname), None
        )
        if not orig:
            continue
        output.append({
            **orig,
            'isDemon': True,
            'demonScore': leg.get('demon_score', {
                'composite': leg.get('p_win', 0),
                'p_win': leg.get('p_win', 0),
            }),
        })
        if len(output) == 2:
            break

    print(json.dumps(output))


if __name__ == '__main__':
    main()
