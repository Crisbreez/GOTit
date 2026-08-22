"""
script_tag.py — GOTit Script Tag Signal

Computes script_tag for each prop:
  SUPPORT  — multiple signals agree (sharp + history/sample) → boost blend
  WEAK     — only one weak signal present → slight boost
  PASS     — no real edge, but no hard contradictions → 50/50 baseline
  BLIND    — signal conflict or thin sample AND no sharp → penalise / skip

Also computes matchup_tag (PLUS / NEUTRAL / MINUS) from park factor + game total.
"""
from __future__ import annotations
from typing import Any, Dict, Optional


# ── Script tag ────────────────────────────────────────────────────────────────

def compute_script_tag(row: Dict[str, Any]) -> str:
    """
    Returns: 'SUPPORT' | 'WEAK' | 'PASS' | 'BLIND'

    Signals considered:
      has_sharp    — sharpFairLine present
      has_model    — projMu present and non-zero
      has_history  — hitRate present AND hitRateSample >= 5
      has_form     — form_trend != 0 (learning loop momentum)
      lineup_ok    — lineup_ok field truthy
    """
    has_sharp   = bool(row.get('sharpFairLine') or row.get('sharp_fair_line'))
    proj_mu     = row.get('projMu') or row.get('proj_mu')
    has_model   = proj_mu is not None and float(proj_mu) > 0
    hit_rate    = row.get('hitRate') or row.get('hit_rate')
    hr_sample   = int(row.get('hitRateSample') or row.get('hit_rate_sample') or 0)
    has_history = hit_rate is not None and hr_sample >= 5
    form_trend  = int(row.get('form_trend') or 0)
    has_form    = form_trend != 0
    lineup_ok   = bool(row.get('lineup_ok') or row.get('lineupOk'))

    signal_count = sum([has_sharp, has_model, has_history])

    # BLIND: no signals at all, or lineup unconfirmed with no model
    if signal_count == 0:
        return 'BLIND'

    # SUPPORT: 2+ signals agree AND (lineup confirmed OR has sharp)
    if signal_count >= 2 and (lineup_ok or has_sharp):
        return 'SUPPORT'

    # SUPPORT via strong sharp + model agreement alone
    if has_sharp and has_model:
        return 'SUPPORT'

    # SUPPORT via history backing model
    if has_model and has_history:
        return 'SUPPORT'

    # WEAK: exactly one signal present
    if signal_count == 1:
        return 'WEAK'

    # Fallback
    return 'PASS'


def script_tag_boost(tag: str) -> float:
    """
    Additive p_true boost for SUPPORT/WEAK, penalty for BLIND.
    Applied after the existing blend — keeps haircut logic intact.
    """
    return {
        'SUPPORT': +0.025,
        'WEAK':    +0.008,
        'PASS':    0.000,
        'BLIND':   -0.040,
    }.get(tag, 0.0)


# ── Matchup tag ───────────────────────────────────────────────────────────────

# Simple park factor table (HR-adjusted; hitter-friendly > 1.0)
_PARK_FACTORS: Dict[str, float] = {
    # Hitter-friendly
    'COL': 1.30, 'CIN': 1.12, 'PHI': 1.10, 'TEX': 1.08,
    'BOS': 1.06, 'NYY': 1.05, 'HOU': 1.04, 'MIL': 1.03,
    # Neutral
    'CHC': 1.01, 'ATL': 1.00, 'LAD': 1.00, 'MIN': 0.99,
    'SEA': 0.98, 'STL': 0.98, 'ARI': 0.97, 'TOR': 0.97,
    'NYM': 0.97, 'DET': 0.96, 'CLE': 0.96,
    # Pitcher-friendly
    'OAK': 0.95, 'TB':  0.94, 'SF':  0.93, 'MIA': 0.92,
    'SD':  0.91, 'CWS': 0.90,
}

def compute_matchup_tag(row: Dict[str, Any]) -> str:
    """
    Returns: 'PLUS' | 'NEUTRAL' | 'MINUS'

    For hitter props  → PLUS when park factor > 1.05
    For pitcher props → PLUS when park factor < 0.95
    """
    team_abbr = str(row.get('homeTeam') or row.get('home_team') or row.get('teamAbbr') or '').upper()
    stat_type = str(row.get('statType') or row.get('stat_type') or '').lower()

    pf = _PARK_FACTORS.get(team_abbr, 1.00)

    is_pitcher_prop = any(k in stat_type for k in [
        'strikeout', 'pitching', 'pitcher', 'pitches', 'walks allowed',
        'hits allowed', 'earned run', 'outs'
    ])

    if is_pitcher_prop:
        if pf < 0.95:
            return 'PLUS'
        elif pf > 1.05:
            return 'MINUS'
        else:
            return 'NEUTRAL'
    else:
        if pf > 1.05:
            return 'PLUS'
        elif pf < 0.95:
            return 'MINUS'
        else:
            return 'NEUTRAL'


def matchup_boost(tag: str) -> float:
    return {
        'PLUS':    +0.015,
        'NEUTRAL':  0.000,
        'MINUS':   -0.015,
    }.get(tag, 0.0)
