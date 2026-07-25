"""
demon_pipeline.py — Demontime

Demontime: per game, score every PP demon individually on adjusted solo More
hit prob; always take top 2; never joint-pair; never fake mu=line*1.05 as
real signal.

Mode: forced_top2_individual
  - Always emit exactly 2 picks per game (if ≥2 demon props exist)
  - Score alone, sort by p_adj DESC
  - No tau gate to DROP from pool — tau only sets a below_confidence_floor flag
  - No signal → BLIND → p_raw = 0.50 * 0.88 — still eligible for forced fill
"""

from __future__ import annotations

import json
import logging
import math
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

TAU = 0.52   # below_confidence_floor warning threshold — does NOT drop from pool

# MLB Demontime allowlist — ONLY these stats qualify for MLB demons
# Singles allowed only at line 0.5
MLB_DEMON_ALLOWLIST = {
    'Total Bases',
    'Hits+Runs+RBIs',
    'Hitter Fantasy Score',
    'Singles',
}
MLB_SINGLES_MAX_LINE = 0.5

# Non-MLB: keep old blocklist approach (no allowlist restriction)
DEMON_STAT_BLOCKLIST = {
    'Doubles', 'Triples', 'Home Runs', 'RBIs', 'Walks',
    'Stolen Bases', 'Hitter Strikeouts', 'Plate Appearances',
}

UNCERTAINTY_HAIRCUT_BLIND    = 0.88
UNCERTAINTY_HAIRCUT_DEGRADED = 0.92
TRASH_STAT_PENALTY           = 0.70
ZERO_STRING_PENALTY          = 0.05


# ─────────────────────────────────────────────────────────────────────────────
# Math
# ─────────────────────────────────────────────────────────────────────────────

def _phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _p_more(mu: float, sigma: float, line: float) -> float:
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
        'Hitter Fantasy Score': 0.40,
        'Points': 0.26, 'Rebounds': 0.40, 'Assists': 0.45,
        'Points+Rebounds+Assists': 0.28,
        'Rushing Attempts': 0.35, 'Receiving Yards': 0.40, 'Passing Yards': 0.22,
        'Takedowns': 0.55, 'Takedowns Landed': 0.55,
        'Fight Time': 0.30, 'Fight Time (Mins)': 0.30,
        'Significant Strikes': 0.22, 'Significant Strikes Landed': 0.22,
        'Round 1 Significant Strikes': 0.28, 'R1 Significant Strikes': 0.28,
        'Total Strikes': 0.25, 'Total Strikes Landed': 0.25,
        'Knockdowns': 0.90, 'Submission Attempts': 0.85,
        'Ground Control Time': 0.40,
    }
    return max(0.5, line * ratios.get(stat_type, 0.40))


# ─────────────────────────────────────────────────────────────────────────────
# Signal detection
# ─────────────────────────────────────────────────────────────────────────────

def _data_quality(raw: Dict[str, Any]) -> str:
    """
    REAL   — has sharp line, model mu/sigma, or hit rate / avg stat
    BLIND  — no external signal whatsoever
    """
    sharp_fair  = raw.get('sharpFairLine') or raw.get('sharp_fair_line')
    sharp_gap   = abs(float(raw.get('sharpGap',   raw.get('sharp_gap',   0)) or 0))
    mu_raw      = float(raw.get('mu',   0) or 0)
    sigma_raw   = float(raw.get('sigma', 0) or 0)
    hit_rate    = raw.get('hitRate')  or raw.get('hit_rate')
    avg_stat    = raw.get('avgStat')  or raw.get('avg_stat')
    shade       = raw.get('ppShadeSignal') or raw.get('pp_shade_signal') or ''
    line_move   = abs(float(raw.get('lineMove',       raw.get('line_move',       0)) or 0))
    line_moves  = int(raw.get('lineMoveCount', raw.get('line_move_count', 0)) or 0)

    has_real = (
        sharp_fair is not None
        or sharp_gap > 0.01
        or mu_raw > 0
        or sigma_raw > 0
        or hit_rate is not None
        or avg_stat is not None
        or shade not in ('', 'no_data', None)
        or line_move > 0.01
        or line_moves > 0
    )
    return 'REAL' if has_real else 'BLIND'


# ─────────────────────────────────────────────────────────────────────────────
# Blank / zero-string detection
# ─────────────────────────────────────────────────────────────────────────────

def _blank_history_flags(raw: Dict[str, Any]) -> Tuple[bool, bool, str]:
    """
    Returns (zero_string_fail, crush, reason).
    zero_string_fail  → ZERO_STRING_PENALTY (0.05x)
    crush             → smaller penalty (0.10x) for hit_rate=0 / avg=0 / dnp
    """
    recent = raw.get('recentStats') or raw.get('recent_stats') or []
    if isinstance(recent, list) and len(recent) >= 3:
        zeros = sum(1 for v in recent if v == 0 or v is None)
        if zeros / len(recent) >= 0.60:
            return True, True, f'zero_heavy:{zeros}/{len(recent)}'

    hit_rate = raw.get('hitRate') or raw.get('hit_rate')
    if hit_rate is not None:
        try:
            if float(hit_rate) == 0.0:
                return False, True, 'hit_rate=0'
        except (ValueError, TypeError):
            pass

    avg = raw.get('avgStat') or raw.get('avg_stat')
    if avg is not None:
        try:
            if float(avg) == 0.0:
                return False, True, 'avg_stat=0'
        except (ValueError, TypeError):
            pass

    dnp = raw.get('dnpProb') or raw.get('dnp_prob')
    if dnp is not None:
        try:
            if float(dnp) >= 0.25:
                return False, True, f'dnp_prob={float(dnp):.2f}'
        except (ValueError, TypeError):
            pass

    return False, False, ''


# ─────────────────────────────────────────────────────────────────────────────
# Solo hit probability
# ─────────────────────────────────────────────────────────────────────────────

def _solo_hit_prob_more(raw: Dict[str, Any]) -> Tuple[float, str]:
    """
    Returns (p_raw, data_quality).
    Never uses mu = line * 1.05 as REAL signal.
    """
    quality = _data_quality(raw)
    line      = float(raw.get('lineScore', raw.get('line', 0)) or 0)
    stat_type = str(raw.get('statType', raw.get('stat_type', '')) or '')

    mu_raw    = float(raw.get('mu',    0) or 0)
    sigma_raw = float(raw.get('sigma', 0) or 0)

    # Sharp fair line from MoneyLine/SGO
    sharp_fair     = raw.get('sharpFairLine') or raw.get('sharp_fair_line')
    sharp_p_more   = None
    if sharp_fair is not None:
        try:
            sf = float(sharp_fair)
            sigma_est = _estimate_sigma(line, stat_type)
            sharp_p_more = _p_more(sf, sigma_est, line)
        except (ValueError, TypeError):
            pass

    if quality == 'REAL':
        if mu_raw > 0 and sigma_raw > 0:
            p_model = _p_more(mu_raw, sigma_raw, line)
            if sharp_p_more is not None:
                p_raw = min(p_model, sharp_p_more)   # conservative
            else:
                p_raw = p_model
        elif sharp_p_more is not None:
            p_raw = sharp_p_more
        else:
            # Has other real signal (shade, line move) but no numeric anchor
            # Use hit_rate or avg_stat if available
            hit_rate = raw.get('hitRate') or raw.get('hit_rate')
            if hit_rate is not None:
                try:
                    p_raw = float(hit_rate)
                except (ValueError, TypeError):
                    p_raw = 0.50
            else:
                p_raw = 0.50
        return max(0.01, min(0.99, p_raw)), 'REAL'

    # No real signal — ineligible
    return None, 'BLIND'


# ─────────────────────────────────────────────────────────────────────────────
# Score one demon prop → p_adj
# ─────────────────────────────────────────────────────────────────────────────

def _score(raw: Dict[str, Any]) -> Dict[str, Any]:
    stat_type = str(raw.get('statType', raw.get('stat_type', '')) or '')
    line      = float(raw.get('lineScore', raw.get('line', 0)) or 0)

    p_raw, quality = _solo_hit_prob_more(raw)

    # No signal → ineligible, never enters sort
    if p_raw is None:
        return {
            **raw,
            'p_raw': None, 'p_adj': None, 'p_hit': 0.0, 'propScore': 0.0,
            'confidenceLevel': 0, 'data_quality': 'BLIND', 'fragility': 1.0,
            'below_confidence_floor': True, 'flags': ['insufficient_projection_data'],
            'direction': 'over', 'isDemon': True,
            'eligible': False,
            'ineligible_reason': 'insufficient_projection_data',
            'blank_history': False, 'blank_reason': '',
        }

    zero_string, crush, blank_reason = _blank_history_flags(raw)
    league = str(raw.get('league', raw.get('sport', '')) or '').upper()
    # MLB stat fingerprints — used when league field missing
    _MLB_STATS = {
        'Total Bases','Hits+Runs+RBIs','Hitter Fantasy Score','Singles','Hits',
        'Home Runs','RBIs','Pitcher Strikeouts','Pitches Thrown','Pitching Outs',
        'Earned Runs Allowed','Hits Allowed','Walks Allowed','Stolen Bases',
        'Hitter Strikeouts','Plate Appearances','Doubles','Triples','Runs',
        'Hitter Fantasy Score',
    }
    is_mlb = league == 'MLB' or (not league and stat_type in _MLB_STATS)

    # MLB allowlist enforcement
    mlb_blocked = False
    if is_mlb:
        if stat_type not in MLB_DEMON_ALLOWLIST:
            mlb_blocked = True
        elif stat_type == 'Singles' and line > MLB_SINGLES_MAX_LINE:
            mlb_blocked = True

    is_trash_stat = (not is_mlb and stat_type in DEMON_STAT_BLOCKLIST) or mlb_blocked

    p_adj = p_raw

    # Adjustments
    if is_trash_stat:
        p_adj *= TRASH_STAT_PENALTY       # 0.70x — stays in pool, ranks last
    if zero_string:
        p_adj *= ZERO_STRING_PENALTY      # 0.05x — effectively last resort
    elif crush:
        p_adj *= 0.10                     # heavy crush — near-zero but not gone

    # Clamp
    p_adj = max(0.01, min(0.99, p_adj))

    fragility = round(1.0 - p_adj, 4)
    below_floor = p_adj < TAU

    flags = []
    if is_trash_stat:
        flags.append('trash_stat')
    if zero_string:
        flags.append('zero_string')
    elif crush:
        flags.append('crushed')
    if below_floor:
        flags.append('below_confidence_floor')
    if quality == 'BLIND':
        flags.append('low_data')

    return {
        **raw,
        'p_raw':                round(p_raw, 4),
        'p_adj':                round(p_adj, 4),
        'p_hit':                round(p_adj, 4),   # alias for compat
        'propScore':            round(p_adj, 4),
        'confidenceLevel':      max(1, min(5, round(p_adj * 5))),
        'data_quality':         quality,
        'fragility':            fragility,
        'below_confidence_floor': below_floor,
        'flags':                flags,
        'direction':            'over',
        'isDemon':              True,
        # eligibility — always True (forced mode — pool never drained)
        'eligible':             True,
        'ineligible_reason':    '',
        'blank_history':        crush or zero_string,
        'blank_reason':         blank_reason,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

def run_demon_pipeline(props: List[Dict[str, Any]], game_id: str) -> Dict[str, Any]:
    """
    Run Demontime for a single game.
    Always returns top 2 demon props (forced). Never empty if ≥2 demons exist.
    """
    # Filter to demons, over only (synthetic unders excluded)
    demons = [p for p in props if p.get('isDemon') or p.get('is_demon')]
    demons = [p for p in demons if str(p.get('direction', 'over')).lower() != 'under']
    demons = [p for p in demons if not p.get('isSynthetic')]

    if not demons:
        return {
            'selected_demons': [],
            'other_demons': [],
            'status': 'NO-GO',
            'strategy': 'Demontime',
            'mode': 'forced_top2_individual',
            'trace': {'game_id': game_id, 'reason': 'no demon props in game'},
            'error': None,
        }

    # Score ALL demons
    all_scored = [_score(d) for d in demons]

    # Only eligible props enter the sort — no signal = ineligible
    eligible   = [d for d in all_scored if d.get('eligible', False) and d.get('p_adj') is not None]
    ineligible = [d for d in all_scored if not d.get('eligible', False)]

    # Sort eligible: p_adj DESC, then p_raw DESC, then lower fragility
    eligible.sort(key=lambda x: (-x['p_adj'], -(x['p_raw'] or 0), x['fragility']))

    # Top-2 from eligible only
    picks  = eligible[:2]
    others = eligible[2:] + ineligible  # ineligible listed but not pickable

    # Annotate picks with rank
    for i, p in enumerate(picks):
        p['rank'] = i + 1

    real_count  = sum(1 for d in all_scored if d.get('data_quality') == 'REAL')
    blind_count = sum(1 for d in all_scored if d.get('data_quality') == 'BLIND')

    return {
        'selected_demons':        picks,
        'post_relaxation_demons': picks,   # compat alias
        'other_demons':           others,
        'status':  'CLEAR' if picks else 'NO-GO',
        'strategy': 'Demontime',
        'mode': 'forced_top2_individual',
        'notes': 'forced top2 by solo p_adj; not joint pair',
        'trace': {
            'game_id':           game_id,
            'total_demon_props': len(demons),
            'real_signal':       real_count,
            'blind':             blind_count,
            'eligible':          len(eligible),
            'ineligible':        len(ineligible),
            'selected':          len(picks),
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
