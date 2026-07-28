#!/usr/bin/env python3
"""
optimize.py — GOTit optimizer entry point.

Input  (stdin): JSON array of web-app props (from /api/slate)
Output (stdout): JSON { game_id: SystemDecision }

Calls run_system_for_game (The System spec) per game.
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

from gotit.leg_selector import (
    run_system_for_game,
    format_system_output,
    DEFAULT_CFG,
)

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

    # Collect all game IDs from non-demon props
    by_game: Dict[str, List[Dict]] = defaultdict(list)
    for p in props:
        if p.get('isDemon') or p.get('is_demon'):
            continue  # SEPARATION RULE: demons never enter The System
        gid = str(p.get('gameId') or p.get('game_id') or '')
        if gid:
            by_game[gid].append(p)

    output: Dict[str, Any] = {}

    for game_id, game_props in by_game.items():
        try:
            # run_system_for_game — spec-compliant per-game System
            decision = run_system_for_game(
                game_id  = game_id,
                tiles    = game_props,
                models   = {},       # no model feed yet
                sharps   = {},       # sharp quotes via sharpFairLine on prop row
                context  = {},       # no context feed yet
                cfg      = DEFAULT_CFG,
            )

            formatted = format_system_output(decision, slate_id=game_id)
            legs      = formatted.get('legs', [])
            status    = decision.get('status', decision.get('path', 'NO_GO'))

            output[game_id] = {
                'six_legs':     legs,
                'two_demons':   [],
                'system':       formatted,
                'path':         formatted.get('path', 'NO_GO'),
                'status':       status,            # PLAY | LEAN | NO_GO
                'slip_type':    formatted.get('slip_type'),
                'avg_p':        formatted.get('avg_p'),
                'p_be':         formatted.get('p_be'),
                'package_ev':   formatted.get('package_ev'),
                'no_go_reason': formatted.get('no_go_reason'),
                'stake_pct':    decision.get('stake_pct'),
            }
        except Exception as exc:
            log.exception('optimize.py: game %s failed', game_id)
            output[game_id] = {
                'six_legs':     [],
                'two_demons':   [],
                'path':         'NO_GO',
                'status':       'NO_GO',
                'no_go_reason': str(exc),
                'system':       None,
            }

    print(json.dumps(output))


if __name__ == '__main__':
    main()
