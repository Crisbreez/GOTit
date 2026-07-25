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
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

TAU = 0.50   # per-demon P_hit floor (relaxed to 0.45 if < 2 survive strict)

# Stats barred from Demontime entirely regardless of data quality
DEMON_STAT_BLOCKLIST = {
    'Singles', 'Doubles', 'Triples', 'Home Runs', 'RBIs', 'Walks',
    'Stolen Bases', 'Hitter Strikeouts', 'Plate Appearances',
}


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
        # MLB
        'Total Bases': 0.38, 'Hits': 0.55, 'Hits+Runs+RBIs': 0.40,
        'Pitcher Strikeouts': 0.32, 'Pitches Thrown': 0.14,
        'Pitching Outs': 0.28, 'Hits Allowed': 0.42,
        'Earned Runs Allowed': 0.70, 'Walks Allowed': 0.75,
        'Significant Strikes': 0.22,
        'Round 1 Significant Strikes': 0.28,
        'R1 Significant Strikes': 0.28, 'Hitter Fantasy Score': 0.40,
        # NBA/NFL
        'Points': 0.26, 'Rebounds': 0.40, 'Assists': 0.45,
        'Points+Rebounds+Assists': 0.28, 'Rushing Attempts': 0.35,
        'Receiving Yards': 0.40, 'Passing Yards': 0.22,
        # MMA/UFC
        'Takedowns': 0.55,
        'Takedowns Landed': 0.55,
        'Fight Time': 0.30,
        'Fight Time (Mins)': 0.30,
        'Significant Strikes': 0.22,
        'Significant Strikes Landed': 0.22,
        'Total Strikes': 0.25,
        'Total Strikes Landed': 0.25,
        'Knockdowns': 0.90,           # high variance — low line, big sigma
        'Submission Attempts': 0.85,
        'Ground Control Time': 0.40,
    }
    return max(0.5, line * ratios.get(stat_type, 0.40))


def _estimate_mu(line: float, sharp_gap: float) -> float:
    if sharp_gap != 0.0:
        return line + sharp_gap
    return line * 1.05  # slight positive edge assumed on PP demon lines


# ─────────────────────────────────────────────────────────────────────────────
# Blank / zero-string detection
# ─────────────────────────────────────────────────────────────────────────────

def _is_blank_history(raw: Dict[str, Any]) -> Tuple[bool, str]:
    """
    Detect players with zero-heavy or blank recent stat histories.
    A demon whose last N games are all 0 or missing is a trap — reject it.

    Checks:
      1. recentStats / recent_stats array — if ≥ 60% of last 5+ entries are 0 → REJECT
      2. hitRate / hit_rate is 0 or None — REJECT
      3. avgStat / avg_stat == 0 — REJECT
      4. dnpProb / dnp_prob >= 0.25 — REJECT (high did-not-play risk)
    """
    # Check recent stats array
    recent = raw.get('recentStats') or raw.get('recent_stats') or []
    if isinstance(recent, list) and len(recent) >= 3:
        zeros = sum(1 for v in recent if v == 0 or v is None)
        if zeros / len(recent) >= 0.60:
            return True, f'zero_heavy_history: {zeros}/{len(recent)} zeros in recent stats'

    # Check hit rate
    hit_rate = raw.get('hitRate') or raw.get('hit_rate')
    if hit_rate is not None:
        try:
            if float(hit_rate) == 0.0:
                return True, 'hit_rate=0'
        except (ValueError, TypeError):
            pass

    # Check average stat
    avg = raw.get('avgStat') or raw.get('avg_stat')
    if avg is not None:
        try:
            if float(avg) == 0.0:
                return True, 'avg_stat=0'
        except (ValueError, TypeError):
            pass

    # Check DNP probability
    dnp = raw.get('dnpProb') or raw.get('dnp_prob')
    if dnp is not None:
        try:
            if float(dnp) >= 0.25:
                return True, f'dnp_prob={float(dnp):.2f} >= 0.25'
        except (ValueError, TypeError):
            pass

    return False, ''


# ─────────────────────────────────────────────────────────────────────────────
# Score one demon prop
# ─────────────────────────────────────────────────────────────────────────────

def _has_sharp_data(raw: Dict[str, Any]) -> bool:
    """True if prop has real sharp/model data beyond line-derived estimates."""
    sharp_gap    = float(raw.get('sharpGap',    raw.get('sharp_gap',    0)) or 0)
    mu_raw       = float(raw.get('mu',          0) or 0)
    sigma_raw    = float(raw.get('sigma',       0) or 0)
    hit_rate     = raw.get('hitRate')     or raw.get('hit_rate')
    avg_stat     = raw.get('avgStat')     or raw.get('avg_stat')
    shade        = raw.get('ppShadeSignal') or raw.get('pp_shade_signal') or ''
    line_move    = abs(float(raw.get('lineMove', raw.get('line_move', 0)) or 0))
    line_moves   = int(raw.get('lineMoveCount', raw.get('line_move_count', 0)) or 0)
    sharp_fair   = raw.get('sharpFairLine') or raw.get('sharp_fair_line')
    return (
        abs(sharp_gap) > 0.01
        or mu_raw > 0
        or sigma_raw > 0
        or hit_rate is not None
        or avg_stat is not None
        or shade not in ('', 'no_data', None)
        or line_move > 0.01
        or line_moves > 0
        or sharp_fair is not None
    )


def _score(raw: Dict[str, Any]) -> Dict[str, Any]:
    line      = float(raw.get('lineScore', raw.get('line', 0)) or 0)
    stat_type = str(raw.get('statType', raw.get('stat_type', '')) or '')
    sharp_gap = float(raw.get('sharpGap', raw.get('sharp_gap', 0)) or 0)

    has_sharp            = _has_sharp_data(raw)
    is_blank, blank_rsn  = _is_blank_history(raw)

    def _ineligible(reason: str) -> Dict[str, Any]:
        return {
            **raw,
            'p_hit': 0.0, 'propScore': 0.0, 'confidenceLevel': 0,
            'mu': 0.0, 'sigma': 0.0, 'z_score': 0.0,
            'direction': 'over', 'isDemon': True,
            'eligible': False,
            'ineligible_reason': reason,
            'blank_history': is_blank, 'blank_reason': blank_rsn,
        }

    # Gate 1: blocklisted stat
    if stat_type in DEMON_STAT_BLOCKLIST:
        return _ineligible(f'stat_blocklisted:{stat_type}')

    # Gate 2: no real signal = ineligible, period
    if not has_sharp:
        return _ineligible('insufficient_projection_data')

    # Gate 3: blank/zero history
    if is_blank:
        return _ineligible(f'blank_history:{blank_rsn}')

    # Score
    mu    = float(raw.get('mu', 0) or 0) or _estimate_mu(line, sharp_gap)
    sigma = float(raw.get('sigma', 0) or 0) or _estimate_sigma(line, stat_type)
    z     = (mu - line) / sigma if sigma > 0 else 0.0
    p     = _phi(z)

    confidence_level = max(1, min(5, round(p * 5))) if p > 0 else 0

    return {
        **raw,
        'p_hit':           round(p, 4),
        'propScore':       round(p, 4),
        'confidenceLevel': confidence_level,
        'mu':              round(mu, 3),
        'sigma':           round(sigma, 3),
        'z_score':         round(z, 3),
        'direction':       'over',
        'isDemon':         True,
        'eligible':        True,
        'ineligible_reason': '',
        'blank_history':   False,
        'blank_reason':    '',
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

    # Score all then filter ineligible (blocklisted, no projection data, blank history)
    all_scored = [_score(d) for d in demons]
    scored = sorted(
        [d for d in all_scored if d.get('eligible', True) and d['p_hit'] > 0],
        key=lambda x: x['p_hit'], reverse=True
    )
    ineligible_count = len(all_scored) - len(scored)

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
            'game_id':           game_id,
            'total_demon_props': len(demons),
            'ineligible':        ineligible_count,
            'eligible':          len(scored),
            'passed_tau':        len(passed),
            'selected':          len(selected),
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
