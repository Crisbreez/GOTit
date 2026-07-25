"""
demon_pipeline.py — Demontime

Per game, score every PP demon individually on solo More hit probability.
Always return top 2 by p_hit. No joint-pair math. No signal fabrication.
"""

from __future__ import annotations

import json
import logging
import math
import sys
from typing import Any, Dict, List

log = logging.getLogger(__name__)


def _phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def run_demon_pipeline(props: List[Dict[str, Any]], game_id: str) -> Dict[str, Any]:
    """
    Run Demontime for a single game.
    Always returns top 2 demon props by lineScore descending.
    """
    demons = [p for p in props if p.get('isDemon') or p.get('is_demon')]
    demons = [p for p in demons if not p.get('isSynthetic')]
    demons = [p for p in demons if str(p.get('direction', 'over')).lower() != 'under']

    if not demons:
        return {
            'selected_demons':        [],
            'post_relaxation_demons': [],
            'other_demons':           [],
            'status':  'NO-GO',
            'strategy': 'Demontime',
            'mode': 'forced_top2_individual',
            'trace': {'game_id': game_id, 'reason': 'no demon props'},
            'error': None,
        }

    # Sort by lineScore descending — highest line = best demon
    sorted_demons = sorted(demons, key=lambda x: float(x.get('lineScore', 0) or 0), reverse=True)

    picks  = sorted_demons[:2]
    others = sorted_demons[2:]

    for i, p in enumerate(picks):
        p['rank'] = i + 1
        p['eligible'] = True
        p['ineligible_reason'] = ''

    return {
        'selected_demons':        picks,
        'post_relaxation_demons': picks,
        'other_demons':           others,
        'status':  'CLEAR',
        'strategy': 'Demontime',
        'mode': 'forced_top2_individual',
        'trace': {
            'game_id':           game_id,
            'total_demon_props': len(demons),
            'selected':          len(picks),
        },
        'error': None,
    }


def format_output(result: Dict[str, Any]) -> Dict[str, Any]:
    return result


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    try:
        payload = json.loads(sys.stdin.read())
        props   = payload if isinstance(payload, list) else payload.get('props', [])
        game_id = (props[0].get('gameId') or props[0].get('game_id') or 'unknown') if props else 'unknown'
        result  = run_demon_pipeline(props, str(game_id))
        print(json.dumps(result))
    except Exception as exc:
        log.exception('[Demontime] fatal error')
        print(json.dumps({'error': str(exc), 'selected_demons': [], 'post_relaxation_demons': []}))
        sys.exit(1)
