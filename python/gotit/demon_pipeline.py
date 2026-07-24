"""
demon_pipeline.py — Demontime

Analyzes every demon prop in a game.
Produces exactly 2 demons per game — top 2 by P_hit.

P_hit = Phi((mu - line) / sigma)   # More-only (over), standard normal CDF
tau   = 0.50                        # per-demon floor; relax to 0.45 if needed

Rules:
  - Demons are over only
  - Separate pipeline from standard/goblin legs entirely
  - Always return top 2 by P_hit — no joint optimization, no pairing logic
  - If fewer than 2 props exist for a game → return however many exist (min 0)
"""

from __future__ import annotations

import json
import logging
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

TAU = 0.50   # per-demon P_hit floor (relaxed to 0.45 if < 2 survive strict)


# ─────────────────────────────────────────────────────────────────────────────
# Math
# ─────────────────────────────────────────────────────────────────────────────

def _phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _p_hit(mu: float, sigma: float, line: float) -> float:
    """P(stat > line) — More only."""
    if sigma <= 1e-9:
        return 1.0 if mu > line else 0.0
    return _phi((mu - line) / sigma)


def _estimate_sigma(line: float, stat_type: str) -> float:
    ratios = {
        'Total Bases': 0.38, 'Hits': 0.55, 'Hits+Runs+RBIs': 0.40,
        'Pitcher Strikeouts': 0.32, 'Pitches Thrown': 0.14,
        'Pitching Outs': 0.28, 'Hits Allowed': 0.42,
        'Earned Runs Allowed': 0.70, 'Walks Allowed': 0.75,
        'Significant Strikes': 0.22, 'Hitter Fantasy Score': 0.40,
        'Points': 0.26, 'Rebounds': 0.40, 'Assists': 0.45,
        'Points+Rebounds+Assists': 0.28, 'Takedowns': 0.55,
        'Fight Time': 0.30, 'Rushing Attempts': 0.35,
    }
    return max(0.5, line * ratios.get(stat_type, 0.40))


def _estimate_mu(line: float, sharp_gap: float) -> float:
    if sharp_gap != 0.0:
        return line + sharp_gap
    return line * 1.05  # slight positive edge assumed on PP demon lines


# ─────────────────────────────────────────────────────────────────────────────
# Score one demon prop
# ─────────────────────────────────────────────────────────────────────────────

def _score(raw: Dict[str, Any]) -> Dict[str, Any]:
    line      = float(raw.get('lineScore', raw.get('line', 0)) or 0)
    stat_type = str(raw.get('statType', raw.get('stat_type', '')) or '')
    sharp_gap = float(raw.get('sharpGap', raw.get('sharp_gap', 0)) or 0)

    mu    = float(raw.get('mu', 0) or 0) or _estimate_mu(line, sharp_gap)
    sigma = float(raw.get('sigma', 0) or 0) or _estimate_sigma(line, stat_type)
    z     = (mu - line) / sigma if sigma > 0 else 0.0
    p     = _phi(z)

    # Map p_hit → propScore and confidenceLevel so frontend renders correctly
    confidence_level = max(1, min(5, round(p * 5)))   # 0.50→2.5→3, 0.70→3.5→4

    return {
        **raw,
        'p_hit':           round(p, 4),
        'propScore':       round(p, 4),       # frontend uses propScore for bar
        'confidenceLevel': confidence_level,  # frontend uses this for dots
        'mu':              round(mu, 3),
        'sigma':           round(sigma, 3),
        'z_score':         round(z, 3),
        'direction':       'over',
        'isDemon':         True,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

def run_demon_pipeline(props: List[Dict[str, Any]], game_id: str) -> Dict[str, Any]:
    """
    Run Demontime for a single game.
    Returns top 2 demon props by P_hit.
    """
    # Filter to demons, over only
    demons = [p for p in props
              if p.get('isDemon') or p.get('is_demon')]
    demons = [p for p in demons
              if str(p.get('direction', 'over')).lower() != 'under']

    if not demons:
        return {
            'selected_demons': [],
            'post_relaxation_demons': [],
            'status': 'NO-GO',
            'trace': {'game_id': game_id, 'reason': 'no demon props'},
            'error': None,
        }

    # Score all
    scored = sorted([_score(d) for d in demons], key=lambda x: x['p_hit'], reverse=True)

    # Apply tau floor — top 2 that pass
    passed = [d for d in scored if d['p_hit'] >= TAU]

    # Relax tau if fewer than 2 pass
    if len(passed) < 2:
        relaxed_tau = TAU - 0.05
        passed = [d for d in scored if d['p_hit'] >= relaxed_tau]

    # Take top 2
    selected = passed[:2] if len(passed) >= 2 else passed

    # If still 0, take absolute top 2 regardless of floor
    if not selected and scored:
        selected = scored[:2]

    return {
        'selected_demons':        selected,
        'post_relaxation_demons': selected,
        'status':  'CLEAR' if selected else 'NO-GO',
        'trace': {
            'game_id':    game_id,
            'total_scored': len(scored),
            'passed_tau':   len(passed),
            'selected':     len(selected),
        },
        'error': None,
    }


def format_output(result: Dict[str, Any]) -> Dict[str, Any]:
    return result


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────────────────

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
