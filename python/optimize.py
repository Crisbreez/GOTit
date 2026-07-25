#!/usr/bin/env python3
"""
optimize.py — GOTit optimizer entry point.

Input  (stdin): JSON array of web-app props (from /api/slate)
Output (stdout): JSON { game_id: SystemDecision }

The System runs over the FULL SLATE at once (not per game).
max_same_game_legs=2 ensures diversity across games.
The result is then distributed back per game_id for the frontend.
Demons are handled separately by qualify_demons.py — never mixed here.
"""

import json
import logging
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List

logging.basicConfig(level=logging.WARNING)
sys.path.insert(0, str(Path(__file__).parent))

from gotit.leg_selector import run_the_system, format_system_output

log = logging.getLogger(__name__)


def main() -> None:
    raw = sys.stdin.read().strip()
    if not raw:
        print(json.dumps({}))
        return

    try:
        props: List[Dict] = json.loads(raw)
    except Exception as exc:
        log.error('optimize.py: JSON parse failed: %s', exc)
        print(json.dumps({'error': str(exc)}))
        sys.exit(1)

    # ── Strip demons — SEPARATION RULE ───────────────────────────────────────
    standard_props = [
        p for p in props
        if not (p.get('isDemon') or p.get('is_demon'))
        and (p.get('gameId') or p.get('game_id'))
    ]

    # Collect all game IDs for output scaffolding
    all_game_ids = list(dict.fromkeys(
        str(p.get('gameId') or p.get('game_id') or '')
        for p in standard_props
    ))

    output: Dict[str, Any] = {}

    if not standard_props:
        for gid in all_game_ids:
            output[gid] = _nogo(gid, 'no_standard_props')
        print(json.dumps(output))
        return

    # ── Run The System over the FULL SLATE ───────────────────────────────────
    try:
        decision  = run_the_system(
            board   = standard_props,
            models  = {},
            sharps  = {},
            context = {},
        )
        formatted = format_system_output(decision, slate_id='full_slate')
    except Exception as exc:
        log.exception('optimize.py: The System crashed')
        for gid in all_game_ids:
            output[gid] = _nogo(gid, str(exc))
        print(json.dumps(output))
        return

    path      = formatted.get('path', 'NO_GO')
    all_legs  = formatted.get('legs', [])

    # ── Pre-fill every game with NO_GO ────────────────────────────────────────
    for gid in all_game_ids:
        output[gid] = _nogo(gid, formatted.get('no_go_reason', ''))

    if path == 'SYSTEM_FIRE' and all_legs:
        # Distribute legs back to their respective game buckets
        by_game: Dict[str, List[Dict]] = defaultdict(list)
        for leg in all_legs:
            gid = str(leg.get('game_id') or leg.get('gameId') or '')
            by_game[gid].append(leg)

        # Each game that has legs gets a SYSTEM_FIRE result
        for gid, legs in by_game.items():
            output[gid] = {
                'six_legs':     legs,
                'two_demons':   [],
                'system':       formatted,
                'path':         'SYSTEM_FIRE',
                'slip_type':    formatted.get('slip_type'),
                'avg_p':        formatted.get('avg_p'),
                'p_be':         formatted.get('p_be'),
                'package_ev':   formatted.get('package_ev'),
                'no_go_reason': None,
            }

        # Games with no legs selected keep their NO_GO default
    elif path == 'LOCKED':
        # Distribute locked legs to their game
        for gid in all_game_ids:
            output[gid] = {
                'six_legs':     [],
                'two_demons':   [],
                'system':       formatted,
                'path':         'LOCKED',
                'slip_type':    None,
                'avg_p':        None,
                'p_be':         None,
                'package_ev':   None,
                'no_go_reason': None,
            }

    print(json.dumps(output))


def _nogo(game_id: str, reason: str) -> Dict[str, Any]:
    return {
        'six_legs':     [],
        'two_demons':   [],
        'system':       None,
        'path':         'NO_GO',
        'slip_type':    None,
        'avg_p':        None,
        'p_be':         None,
        'package_ev':   None,
        'no_go_reason': reason or 'no_package_found',
    }


if __name__ == '__main__':
    main()
