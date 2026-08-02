"""
leg_selector.py — The System (non-demon prop selection)

One pipeline. Two exit paths.
  Core : score every PP prop (both More and Less), trap filters, fragility
  Path A — LOCKED  : fat misprice with lockable hedge/arb → LOCKED FIRE  (stub in v1)
  Path B — SYSTEM FIRE : package avg P clears break-even + EV > floor
  Path C — NO-GO   : neither clears

Non-negotiable rules:
  assert side in ("more", "less")
  assert decision in ("LOCKED", "SYSTEM_FIRE", "NO_GO")
  if SYSTEM_FIRE: assert avg_p >= p_be and package_ev >= min_package_ev
  demons: side == "more" and p_true >= demon_min_p  (handled in demon_pipeline.py)
"""

from __future__ import annotations

# PropContext helpers (re-used from gotit_engine)
try:
    import sys, os
    sys.path.insert(0, os.path.dirname(__file__))
    from gotit_engine import PropContext, prop_context_from_dict, score_prop_context as _engine_score_ctx
    _HAVE_ENGINE_CTX = True
except Exception:
    _HAVE_ENGINE_CTX = False

import json
import logging
import math
import sys
from collections import Counter
from dataclasses import dataclass, field
from itertools import combinations, product
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Math helpers (Section 3)
# ─────────────────────────────────────────────────────────────────────────────

def _phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def p_more(mu: float, sigma: float, L: float) -> float:
    if sigma <= 1e-9:
        return 1.0 if mu > L else (0.5 if mu == L else 0.0)
    return _phi((mu - L) / sigma)


def p_less(mu: float, sigma: float, L: float) -> float:
    return 1.0 - p_more(mu, sigma, L)


def implied_prob(american: float) -> float:
    a = float(american)
    if a >= 0:
        return 100.0 / (a + 100.0)
    return abs(a) / (abs(a) + 100.0)


def no_vig_pair(imp_over: float, imp_under: float) -> Tuple[float, float]:
    s = imp_over + imp_under
    if s <= 0:
        return 0.5, 0.5
    return imp_over / s, imp_under / s


def p_true_blend(p_model: float, p_sharp: Optional[float], mode: str = "min") -> float:
    if p_sharp is None:
        return _clamp(p_model, 0.01, 0.99)
    if mode == "min":
        return _clamp(min(p_model, p_sharp), 0.01, 0.99)
    return _clamp(0.5 * p_model + 0.5 * p_sharp, 0.01, 0.99)


def count(pt: float, p_be: float) -> float:
    return pt - p_be

# ─────────────────────────────────────────────────────────────────────────────
# Canonical sigma_ratios — single source of truth for all CDF estimates.
# sigma = line * ratio (floored at 0.5).
# Hitters and pitchers use the same math — no position bias.
# Calibrated to approximate real hit-rate dispersion at typical PP line values.
# ─────────────────────────────────────────────────────────────────────────────
SIGMA_RATIOS: Dict[str, float] = {
    # ── MLB Hitter ───────────────────────────────────────────────────────────
    'Total Bases':          0.38,   # line ~2.5; σ≈0.95 — moderate dispersion
    'Hits':                 0.42,   # line ~1.0; σ≈0.42 — corrected down from 0.55
    'Hits+Runs+RBIs':       0.40,   # line ~2.5; σ≈1.0
    'Hitter Fantasy Score': 0.36,   # line ~7-9; σ≈2.7 — tighter than generic
    # ── MLB Pitcher ──────────────────────────────────────────────────────────
    'Pitcher Strikeouts':   0.32,   # line ~4-7; σ≈1.6-2.2
    'Pitches Thrown':       0.14,   # line ~70-95; σ≈10-13 — tight
    'Pitching Outs':        0.28,   # line ~12-18; σ≈3.4-5.0
    'Hits Allowed':         0.42,
    'Earned Runs Allowed':  0.70,
    'Walks Allowed':        0.75,
    'Pitcher Fantasy Score':0.34,
    # ── MMA / UFC ────────────────────────────────────────────────────────────
    'Significant Strikes':       0.22,
    'Round 1 Significant Strikes': 0.28,
    'R1 Significant Strikes':    0.28,
    'Total Strikes':             0.25,
    'Takedowns':                 0.55,
    'Fight Time (Mins)':         0.30,
    'Fight Time':                0.30,
    'Knockdowns':                0.90,
    'Submission Attempts':       0.85,
    # ── NBA ─────────────────────────────────────────────────────────────────
    'Points':                    0.26,
    'Rebounds':                  0.40,
    'Assists':                   0.45,
    'Points+Rebounds+Assists':   0.28,
    'Fantasy Score':             0.40,
    # ── NFL ──────────────────────────────────────────────────────────────────
    'Rushing Attempts':          0.35,
    'Receiving Yards':           0.45,
    'Passing Yards':             0.22,
    'Rushing Yards':             0.40,
}


def _sigma_for(stat_type: str, line: float) -> float:
    """Return calibrated sigma for a stat/line pair. Never below 0.5."""
    ratio = SIGMA_RATIOS.get(stat_type, 0.40)
    return max(0.5, line * ratio)


# ─────────────────────────────────────────────────────────────────────────────
# Payout tables (PrizePicks standard)
# ─────────────────────────────────────────────────────────────────────────────

PAYOUTS: Dict[str, Dict[int, float]] = {
    "5_flex": {5: 10.0, 4: 2.0, 3: 0.4, 2: 0.0, 1: 0.0, 0: 0.0},
    "6_flex": {6: 25.0, 5: 2.0, 4: 0.4, 3: 0.0, 2: 0.0, 1: 0.0, 0: 0.0},
    "2_power": {2: 3.0, 1: 0.0, 0: 0.0},
    "3_power": {3: 5.0, 2: 0.0, 1: 0.0, 0: 0.0},
    "4_power": {4: 10.0, 3: 0.0, 2: 0.0, 1: 0.0, 0: 0.0},
}


# Multiplier tables (maximize multiplier subject to EV >= play_ev_min)
MULTIPLIERS: Dict[str, float] = {
    "2_power": 3.0,
    "3_power": 5.0,
    "4_power": 10.0,
    "5_flex":  10.0,   # all-hit tier
    "6_flex":  25.0,   # all-hit tier
}


def flex_ev(probs: List[float], payout_by_hits: Dict[int, float], stake: float = 1.0) -> float:
    """Full enumeration over 2^n hit patterns (n <= 6)."""
    n = len(probs)
    ev = 0.0
    for mask in product([0, 1], repeat=n):
        hits = sum(mask)
        p = 1.0
        for i, h in enumerate(mask):
            p *= probs[i] if h else (1.0 - probs[i])
        mult = payout_by_hits.get(hits, 0.0)
        ev += p * (mult * stake)
    return ev - stake  # net EV


def power_ev(probs: List[float], multiplier: float, stake: float = 1.0) -> float:
    p_all = 1.0
    for p in probs:
        p_all *= p
    return p_all * multiplier * stake - stake


# ─────────────────────────────────────────────────────────────────────────────
# Config defaults (Section 6)
# ─────────────────────────────────────────────────────────────────────────────

DEFAULT_CFG: Dict[str, Any] = {
    "p_be": {
        "5_flex":  0.543,
        "6_flex":  0.542,
        "2_power": 0.577,
        "3_power": 0.550,
        "4_power": 0.560,
    },
    "p_true_mode":       "min",
    "absolute_floor_p":  0.50,   # floor lowered — 0.52 kills too many props when sharp data sparse
    "demon_min_p":       0.50,
    "ban_goblins":       False,   # goblins allowed in The System
    "fragility_kill":    0.65,
    "require_role":      False,   # relax v1 — no role feed yet
    "min_package_ev":    0.02,
    "min_lock_roi":      0.005,
    "max_same_game_legs": 6,  # per-game: all 6 legs can be from the same game
    "unit_pct_bankroll": 0.01,
    "lock_unit_pct":     0.02,
    "preferred_slips":   ["5_flex", "6_flex"],
    "fat_count":         0.03,
    "combo_head":        12,      # top-N by count to enumerate combos from
    # PLAY vs LEAN thresholds (run_system_for_game spec)
    "play_ev_min":       0.04,    # EV >= this → PLAY status
    "lean_ev_min":       0.00,    # EV >= this → LEAN (else NO_GO)
    "stake_play":        0.02,    # 2% of bankroll
    "stake_lean":        0.01,    # 1% of bankroll
    # p_true blend weights
    "w_market":          0.50,    # sharpFairLine weight
    "w_model":           0.30,    # mu/sigma model weight
    "w_hitrate":         0.20,    # historical hit rate weight
    "p_haircut":         0.005,   # small haircut on blended p_true
    # Sample-size / games-played gates
    "min_games_played":      5,    # kill if n_games < this AND no p_mkt AND no non-prior model
    "min_games_lean_only":  10,    # cap to LEAN tier if n_games < this (soft gate)
    "min_hit_rate_sample":   5,    # hit_rate only counts as signal if n >= this
    "sample_shrink_n":      10,    # denominator for sample_factor = clamp(n/10, 0, 1)
    "min_role_score_callup": 0.7,  # role_score must be >= this for call-ups
    # 6-factor confidence thresholds
    "conf_sharp_margin":       0.04,  # p_mkt must be >= 0.5 + this to fire sharp_agree
    "conf_model_room":         0.04,  # sigma-normalized gap for model_clear (z-score)
    "conf_stable_sample_factor": 0.8, # sample_factor >= this for stable_sample
    "conf_stable_n_games":    10,     # n_games >= this for stable_sample
}


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ScoredLeg:
    prop_id:     str
    player_id:   str
    player_name: str
    stat_type:   str
    game_id:     str
    side:        str            # "more" | "less"
    line:        float
    p_model:     float
    p_sharp:     float
    p_true:      float
    p_be_5flex:  float
    count:       float
    sharp_gap:   float
    fragility:   float
    trap_flags:  List[str]
    eligible:    bool
    kill_reasons: List[str]
    # raw prop fields for frontend
    is_demon:      bool  = False
    is_goblin:     bool  = False
    sport:         str   = ''
    team:          str   = ''
    # sample-quality fields
    sample_factor: float = 1.0   # clamp(n_games/10, 0, 1)
    low_confidence: bool = False  # sample_factor < 0.5
    lean_only:     bool  = False  # soft cap: n_games < min_games_lean_only
    n_games:       int   = 0
    # 6-factor confidence score
    conf_score:    float = 0.0   # 0.0–1.0 aggregate (more alignment = higher)
    conf_tier:     str   = ''    # "STRONG" | "MODERATE" | "WEAK" | "NOISE"
    conf_factors:  List[str] = None  # which factors fired
    conf_missing:  List[str] = None  # which factors were absent


# ─────────────────────────────────────────────────────────────────────────────
# Sharp quote helpers
# ─────────────────────────────────────────────────────────────────────────────

def _sharp_probs(sharps: List[Dict], line: float) -> Tuple[Optional[float], Optional[float]]:
    """
    Find the closest sharp quote to PP line, return no-vig (over, under).
    sharps: list of {book, side, line, american_odds, ts}
    """
    if not sharps:
        return None, None

    # Match exact line first, else nearest
    def dist(q):
        return abs(q.get('line', line) - line)

    best = min(sharps, key=dist)
    imp_over  = implied_prob(best.get('american_odds', -110)) if best.get('side') == 'more' else None
    imp_under = implied_prob(best.get('american_odds', -110)) if best.get('side') == 'less' else None

    # Try to get both sides
    over_q  = next((q for q in sharps if q.get('side') == 'more'), None)
    under_q = next((q for q in sharps if q.get('side') == 'less'), None)

    if over_q and under_q:
        io = implied_prob(over_q['american_odds'])
        iu = implied_prob(under_q['american_odds'])
        nv_over, nv_under = no_vig_pair(io, iu)
        return nv_over, nv_under

    if over_q:
        io = implied_prob(over_q['american_odds'])
        return io, 1.0 - io
    if under_q:
        iu = implied_prob(under_q['american_odds'])
        return 1.0 - iu, iu

    return None, None


def _sharp_gap(sharps: List[Dict], line: float, side: str) -> float:
    """
    Sharp line vs PP line gap.
    Positive = sharp consensus is further in the favored direction.
    """
    if not sharps:
        return 0.0
    sharp_lines = [q.get('line', line) for q in sharps if q.get('line') is not None]
    if not sharp_lines:
        return 0.0
    avg_sharp = sum(sharp_lines) / len(sharp_lines)
    # For more: sharp > PP line = bad (tougher to clear); sharp < PP line = good
    if side == 'more':
        return line - avg_sharp   # positive = PP line below sharp = easier to hit
    else:
        return avg_sharp - line   # positive = PP line above sharp = easier to hit under


# ─────────────────────────────────────────────────────────────────────────────
# Fragility + trap detection (Section 6)
# ─────────────────────────────────────────────────────────────────────────────

def _fragility_score(ctx: Dict[str, Any]) -> float:
    score = 0.0
    score += 0.25 * min(ctx.get('minutes_risk', 0), 2) / 2
    score += 0.20 * min(ctx.get('blowout_risk', 0), 2) / 2
    score += 0.20 * min(ctx.get('weather_risk', 0), 2) / 2
    score += 0.15 * min(ctx.get('platoon_risk', 0), 2) / 2
    if not ctx.get('role_confirmed', True):
        score += 0.25
    if ctx.get('news_kill', False):
        score = 1.0
    return _clamp(score, 0.0, 1.0)


def _detect_traps(row: Dict, side: str, ctx: Dict, cfg: Dict) -> List[str]:
    flags = []
    featured_ids = cfg.get('ui_featured_ids', set())
    if row.get('prizepicks_id') in featured_ids or row.get('id') in featured_ids:
        flags.append('featured_tab')
    if side == 'more' and cfg.get('public_over_bias_active', False):
        flags.append('public_over_lean')
    if row.get('isDemon') or row.get('is_demon'):
        flags.append('demon')

    # MMA trap: Round 1 Significant Strikes — line set very low (< 15).
    # These props almost always go over — picking less is a trap.
    stat_type = str(row.get('statType') or row.get('stat_type') or '')
    line = float(row.get('lineScore') or row.get('line') or 0)
    if stat_type in ('Round 1 Significant Strikes', 'R1 Significant Strikes',
                     'Significant Strikes', 'Significant Strikes Landed'):
        if side == 'less' and line < 20.0:
            flags.append('mma_sig_strikes_less_trap')

    return flags


# ─────────────────────────────────────────────────────────────────────────────
# Score one prop → two ScoredLegs (Section 5)
# ─────────────────────────────────────────────────────────────────────────────

def _sample_fields(row: Dict) -> Tuple[int, int]:
    """
    Extract (n_games, n_hit_rate_sample) from prop row.
    Looks for gamesPlayed / games_played / nGames / sample_n etc.
    Returns (0, 0) if not present.
    """
    n_games = int(
        row.get('gamesPlayed') or row.get('games_played') or
        row.get('nGames') or row.get('n_games') or
        row.get('sampleN') or row.get('sample_n') or 0
    )
    n_hr = int(
        row.get('hitRateSample') or row.get('hit_rate_sample') or
        row.get('nHitRate') or row.get('n_hit_rate') or
        n_games  # fall back to n_games if specific sample not given
    )
    return n_games, n_hr


def _has_real_signal(row: Dict, sharps: List[Dict], model: Optional[Dict],
                     cfg: Optional[Dict] = None) -> bool:
    """
    True only when at least one real external signal exists.
    mu = line * 1.05 or line * 1.03 are NOT real signals.
    hit_rate only counts if n_hit_rate >= min_hit_rate_sample.
    """
    min_hr_n = (cfg or DEFAULT_CFG).get('min_hit_rate_sample', 5)

    # p_mkt via sharpFairLine or live sharps quotes
    p_mkt_present = bool(
        (row.get('sharpFairLine') or row.get('sharp_fair_line')) or sharps
    )
    if p_mkt_present:
        return True

    # Non-prior model (mu/sigma injected externally, not derived from line)
    if model:
        return True

    # Real data fields on the prop itself
    mu_raw    = float(row.get('mu', 0) or 0)
    sigma_raw = float(row.get('sigma', 0) or 0)
    avg_stat  = row.get('avgStat') or row.get('avg_stat')
    sharp_gap = abs(float(row.get('sharpGap',  row.get('sharp_gap',  0)) or 0))
    line_move = abs(float(row.get('lineMove',  row.get('line_move',  0)) or 0))
    shade     = row.get('ppShadeSignal') or row.get('pp_shade_signal') or ''
    line_moves = int(row.get('lineMoveCount', row.get('line_move_count', 0)) or 0)

    # hit_rate only counts if sample is large enough
    _, n_hr = _sample_fields(row)
    hit_rate_raw = row.get('hitRate') or row.get('hit_rate')
    hit_rate_valid = (hit_rate_raw is not None) and (n_hr >= min_hr_n)

    return (
        mu_raw > 0
        or sigma_raw > 0
        or hit_rate_valid
        or avg_stat is not None
        or sharp_gap > 0.01
        or line_move > 0.01
        or shade not in ('', 'no_data', None)
        or line_moves > 0
    )


def _estimate_mu_sigma(row: Dict, sharps: List[Dict]) -> Tuple[float, float]:
    """
    Estimate mu/sigma when no model projection is available.
    ONLY use sharp lines as signal — never invent mu from line * constant.
    If no real signal: return mu = L (50/50 baseline, no edge).
    """
    L = float(row.get('lineScore', row.get('line', 0)) or 0)
    stat_type = str(row.get('statType', row.get('stat_type', '')) or '')

    sigma = _sigma_for(stat_type, L)

    # Sharp-informed mu — ONLY real sharp lines, never L * constant
    sharp_lines = [q.get('line', None) for q in sharps if q.get('line') is not None]
    if sharp_lines:
        sharp_mu = sum(sharp_lines) / len(sharp_lines)
        return sharp_mu, sigma

    # No real signal — return exact line (50/50 baseline, p_more = 0.50)
    return L, sigma


# ─────────────────────────────────────────────────────────────────────────────
# 6-factor confidence scorer
# More alignment → higher confidence; never certainty.
# Factors (each 0 or 1, weighted):
#   1. sharp_agree   — de-vig fair P clearly on your side (sharp_margin >= thresh)
#   2. role_locked   — starter / role confirmed, not capped
#   3. model_clear   — mu clears line with room (not kissing it)
#   4. matchup_boost — context pushes stat (park, pace, pitcher, total)
#   5. stable_sample — sample_factor >= 0.8 and n_games >= 10
#   6. no_kill_shots — no injury/weather/lineup/bullpen flags
#
# Tiers: STRONG ≥ 0.80 | MODERATE ≥ 0.55 | WEAK ≥ 0.30 | NOISE < 0.30
# ─────────────────────────────────────────────────────────────────────────────

CONF_WEIGHTS = {
    'sharp_agree':   0.28,   # biggest signal
    'model_clear':   0.22,
    'stable_sample': 0.18,
    'role_locked':   0.14,
    'matchup_boost': 0.12,
    'no_kill_shots': 0.06,
}

CONF_TIER_THRESHOLDS = [
    (0.80, 'STRONG'),
    (0.55, 'MODERATE'),
    (0.30, 'WEAK'),
    (0.00, 'NOISE'),
]


def _conf_score(
    row:    Dict,
    side:   str,
    model:  Optional[Dict],
    sharps: List[Dict],
    ctx:    Dict,
    pt:     float,        # blended p_true after haircut
    sample_factor: float,
    n_games: int,
    frag:   float,
    traps:  List[str],
    kills:  List[str],
    cfg:    Dict,
) -> Tuple[float, str, List[str], List[str]]:
    """
    Compute 6-factor confidence score.
    Returns (conf_score 0-1, tier str, fired_factors, missing_factors).
    """
    fired:   List[str] = []
    missing: List[str] = []

    L = float(row.get('lineScore', row.get('line', 0)) or 0)

    # ── Factor 1: sharp_agree ─────────────────────────────────────────────
    sharp_margin_thresh = float(cfg.get('conf_sharp_margin', 0.04))
    p_mkt_val = None
    sharp_fair = row.get('sharpFairLine') or row.get('sharp_fair_line')
    if sharp_fair is not None:
        # Estimate p_mkt from sharpFairLine using same CDF approach as fair_p_market
        try:
            sf = float(sharp_fair)
            stat_type = str(row.get('statType', row.get('stat_type', '')) or '')
            sigma = _sigma_for(stat_type, L)
            p_more_val = _phi((sf - L) / sigma) if sigma > 0 else 0.5
            p_mkt_val = p_more_val if side == 'more' else 1.0 - p_more_val
        except (ValueError, TypeError):
            p_mkt_val = None

    if p_mkt_val is not None and (p_mkt_val - 0.5) >= sharp_margin_thresh:
        fired.append('sharp_agree')
    else:
        missing.append('sharp_agree')

    # ── Factor 2: role_locked ─────────────────────────────────────────────
    role_score = float(ctx.get('role_score', 0.0) or 0.0)
    role_confirmed = ctx.get('role_confirmed', None)
    is_callup = bool(ctx.get('is_callup') or ctx.get('callup'))
    # Confirmed if role_confirmed explicitly True, or role_score high and not callup
    if (role_confirmed is True) or (role_score >= 0.75 and not is_callup):
        fired.append('role_locked')
    elif role_confirmed is False or is_callup:
        missing.append('role_locked')
    else:
        # role_score not provided → neutral, small partial credit
        # give partial by not adding to missing; just skip
        pass

    # ── Factor 3: model_clear ─────────────────────────────────────────────
    model_room_thresh = float(cfg.get('conf_model_room', 0.04))
    if model:
        mu    = float(model.get('mu', L) or L)
        sigma = float(model.get('sigma', 1.0) or 1.0) or 1.0
        gap   = (mu - L) / sigma if side == 'more' else (L - mu) / sigma
        if gap >= model_room_thresh:
            fired.append('model_clear')
        else:
            missing.append('model_clear')
    else:
        # No model — check if sharp fair line has clear room vs PP line
        if sharp_fair is not None:
            sf = float(sharp_fair)
            gap_units = (sf - L) if side == 'more' else (L - sf)
            if gap_units >= 0.25:
                fired.append('model_clear')
            else:
                missing.append('model_clear')
        else:
            missing.append('model_clear')

    # ── Factor 4: matchup_boost ───────────────────────────────────────────
    # Driven by context flags: park_factor, pace_factor, pitcher_hand_adv, game_total_boost
    matchup_keys = ('park_boost', 'pace_boost', 'pitcher_adv', 'game_total_boost',
                    'matchup_score', 'opp_rank_boost')
    matchup_score = max(
        float(ctx.get(k, 0) or 0) for k in matchup_keys
    )
    shade = str(row.get('ppShadeSignal') or row.get('pp_shade_signal') or '')
    # PP shade also counts as matchup signal
    if shade in ('strong', 'moderate', 'bullish'):
        matchup_score = max(matchup_score, 0.6)

    if matchup_score >= 0.5:
        fired.append('matchup_boost')
    else:
        missing.append('matchup_boost')

    # ── Factor 5: stable_sample ───────────────────────────────────────────
    stable_sf_thresh = float(cfg.get('conf_stable_sample_factor', 0.8))
    stable_n_thresh  = int(cfg.get('conf_stable_n_games', 10))
    if sample_factor >= stable_sf_thresh and n_games >= stable_n_thresh:
        fired.append('stable_sample')
    else:
        missing.append('stable_sample')

    # ── Factor 6: no_kill_shots ───────────────────────────────────────────
    kill_flags = {'news_kill', 'fragility', 'weather_risk', 'lineup_downgrade',
                  'bullpen_day', 'injury_risk', 'callup_role_unconfirmed'}
    ctx_kills = {k for k in kill_flags if ctx.get(k)}
    trap_kills = {k for k in traps if k in kill_flags}
    kill_kill  = {k for k in kills if k in kill_flags}
    has_kill   = bool(ctx_kills | trap_kills | kill_kill) or frag >= 0.35

    if not has_kill:
        fired.append('no_kill_shots')
    else:
        missing.append('no_kill_shots')

    # ── Aggregate ─────────────────────────────────────────────────────────
    raw_score = sum(CONF_WEIGHTS.get(f, 0) for f in fired)
    # Bonus: all 6 fire → small boost (reflects true alignment)
    if len(fired) == 6:
        raw_score = min(1.0, raw_score + 0.02)

    score = _clamp(raw_score, 0.0, 1.0)

    tier = 'NOISE'
    for thresh, label in CONF_TIER_THRESHOLDS:
        if score >= thresh:
            tier = label
            break

    return score, tier, fired, missing


def score_prop(row: Dict, model: Optional[Dict], sharps: List[Dict],
               ctx: Dict, cfg: Dict) -> List[ScoredLeg]:
    """
    Score a single prop row for both More and Less.
    Returns list of 1–2 ScoredLeg objects.
    """
    L         = float(row.get('lineScore', row.get('line', 0)) or 0)
    is_demon  = bool(row.get('isDemon') or row.get('is_demon'))
    is_goblin = bool(row.get('isGoblin') or row.get('is_goblin'))
    prop_id   = str(row.get('id') or row.get('prizepicks_id') or '')
    player_id = str(row.get('playerId') or row.get('player_id') or row.get('playerName') or '')
    player_name = str(row.get('playerName') or row.get('player_name') or '')
    stat_type = str(row.get('statType') or row.get('stat_type') or '')
    game_id   = str(row.get('gameId') or row.get('game_id') or '')
    sport     = str(row.get('sport') or '')
    team      = str(row.get('teamAbbr') or row.get('team') or '')

    has_signal = _has_real_signal(row, sharps, model, cfg)

    # ── Sample-size fields ──────────────────────────────────────────────────
    n_games, n_hr = _sample_fields(row)
    # Also pull n_games from the injected model dict (projNGames stamped by mlb_projections.py)
    if n_games == 0 and model:
        n_games = int(model.get('n', 0) or 0)
    min_games        = cfg.get('min_games_played', 5)
    min_games_lean   = cfg.get('min_games_lean_only', 10)
    min_hr_n         = cfg.get('min_hit_rate_sample', 5)
    shrink_n         = cfg.get('sample_shrink_n', 10)
    min_role_callup  = cfg.get('min_role_score_callup', 0.7)

    # sample_factor: shrinks p_true back to 50/50 when sample is thin
    sample_factor = _clamp(n_games / shrink_n if n_games > 0 else 0.0, 0.0, 1.0)
    # If n_games is unknown (0) but we have p_mkt → trust the market, don't shrink
    p_mkt_present = bool(row.get('sharpFairLine') or row.get('sharp_fair_line') or sharps)
    if n_games == 0 and p_mkt_present:
        sample_factor = 1.0

    # Call-up / role flag
    is_callup = bool(ctx.get('is_callup') or ctx.get('callup'))
    role_score = float(ctx.get('role_score', 1.0) or 1.0)

    # Hard kill: too few games AND no market AND no model → not slipable
    no_market_no_model = not p_mkt_present and not model
    sample_kill = (
        n_games > 0
        and n_games < min_games
        and no_market_no_model
    )
    # Role kill for call-ups with unconfirmed role
    role_kill = is_callup and role_score < min_role_callup

    if model:
        mu    = float(model.get('mu', L) or L)
        sigma = float(model.get('sigma', 1.0) or 1.0)
        if sigma <= 1e-9:
            sigma = 1.0
    else:
        mu, sigma = _estimate_mu_sigma(row, sharps)

    # No real signal: lock to 50/50 — do not invent edge
    if not has_signal:
        mu = L

    p_m_model = p_more(mu, sigma, L)
    p_l_model = p_less(mu, sigma, L)

    p_m_sharp_raw, p_l_sharp_raw = _sharp_probs(sharps, L)

    p_be_map = cfg.get('p_be', DEFAULT_CFG['p_be'])
    p_be_5f  = p_be_map.get('5_flex', 0.543)
    frag     = _fragility_score(ctx)
    abs_floor = cfg.get('absolute_floor_p', 0.52)
    frag_kill = cfg.get('fragility_kill', 0.65)

    legs: List[ScoredLeg] = []

    for side, pm, ps_raw in [('more', p_m_model, p_m_sharp_raw),
                              ('less', p_l_model, p_l_sharp_raw)]:
        # Demons are more-only
        if is_demon and side == 'less':
            continue

        pt_raw = p_true_blend(pm, ps_raw, cfg.get('p_true_mode', 'min'))

        # ── Sample shrinkage: p_true_adj = 0.5 + (p_true - 0.5) * sample_factor ──
        pt = 0.5 + (pt_raw - 0.5) * sample_factor

        ps = ps_raw if ps_raw is not None else pm
        gap = _sharp_gap(sharps, L, side)
        traps = _detect_traps(row, side, ctx, cfg)

        # Low sample_factor → low confidence flag
        low_confidence = sample_factor < 0.5

        kills: List[str] = []
        if ctx.get('news_kill', False):
            kills.append('news_kill')
        if frag >= frag_kill:
            kills.append('fragility')
        if is_demon and side != 'more':
            kills.append('demon_side')
        if is_demon and pt < cfg.get('demon_min_p', 0.50):
            kills.append('demon_p_floor')
        if is_goblin and cfg.get('ban_goblins', False):
            kills.append('goblin_ban')
        if pt < abs_floor:
            kills.append('below_floor')
        if 'mma_sig_strikes_less_trap' in traps:
            kills.append('mma_sig_strikes_less_trap')
        if not has_signal:
            kills.append('no_real_signal')
        if sample_kill:
            kills.append('thin_sample')       # hard kill: < min_games, no market/model
        if role_kill:
            kills.append('callup_role_unconfirmed')

        eligible = len(kills) == 0
        # Soft gate: n_games < min_games_lean_only → cap at LEAN (flag only, not kill)
        lean_only = n_games > 0 and n_games < min_games_lean and not sample_kill

        legs.append(ScoredLeg(
            prop_id      = prop_id,
            player_id    = player_id,
            player_name  = player_name,
            stat_type    = stat_type,
            game_id      = game_id,
            side         = side,
            line         = L,
            p_model      = pm,
            p_sharp      = ps,
            p_true       = pt,
            p_be_5flex   = p_be_5f,
            count        = count(pt, p_be_5f),
            sharp_gap    = gap,
            fragility    = frag,
            trap_flags   = traps,
            eligible       = eligible,
            kill_reasons   = kills,
            is_demon       = is_demon,
            is_goblin      = is_goblin,
            sport          = sport,
            team           = team,
            sample_factor  = sample_factor,
            low_confidence = low_confidence,
            lean_only      = lean_only,
            n_games        = n_games,
        ))
        # Back-patch conf_score onto the leg we just appended
        _cs, _ct, _cf, _cm = _conf_score(
            row=row, side=side, model=model, sharps=sharps, ctx=ctx,
            pt=pt, sample_factor=sample_factor, n_games=n_games,
            frag=frag, traps=traps, kills=kills, cfg=cfg,
        )
        legs[-1].conf_score   = _cs
        legs[-1].conf_tier    = _ct
        legs[-1].conf_factors = _cf
        legs[-1].conf_missing = _cm

    return legs


# ─────────────────────────────────────────────────────────────────────────────
# Path A — LOCKED scan (Section 7, v1 stub)
# ─────────────────────────────────────────────────────────────────────────────

def lock_scan(scored_legs: List[ScoredLeg], cfg: Dict) -> Optional[Dict]:
    """
    v1 stub — no multi-book odds feed yet.
    Always returns None; Path B runs full System.
    Wire real arb/middle logic when book odds feed is available.
    """
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Path B — Package builder (Section 8)
# ─────────────────────────────────────────────────────────────────────────────

def _dedup_pool(eligible: List[ScoredLeg]) -> List[ScoredLeg]:
    """One prop_id → best side by p_true. One player_id → best prop by count."""
    # Step 1: best side per prop
    best_by_prop: Dict[str, ScoredLeg] = {}
    for leg in eligible:
        prev = best_by_prop.get(leg.prop_id)
        if prev is None or leg.p_true > prev.p_true:
            best_by_prop[leg.prop_id] = leg

    # Step 2: best prop per player
    best_by_player: Dict[str, ScoredLeg] = {}
    for leg in best_by_prop.values():
        prev = best_by_player.get(leg.player_id)
        if prev is None or leg.count > prev.count:
            best_by_player[leg.player_id] = leg

    return list(best_by_player.values())


def _select_combos(pool: List[ScoredLeg], n: int, max_same_game: int):
    """
    v1: take top combo_head by count, enumerate C(head, n), filter same-game cap.
    Yields lists of ScoredLeg.
    """
    head = pool[:12]
    for combo in combinations(head, n):
        game_counts = Counter(leg.game_id for leg in combo)
        if max(game_counts.values(), default=0) > max_same_game:
            continue
        yield list(combo)


def build_packages(eligible: List[ScoredLeg], cfg: Dict) -> Dict:
    """Path B — find best flex package that clears avg_p >= p_be and EV > min_ev."""
    pool = sorted(_dedup_pool(eligible), key=lambda x: x.count, reverse=True)
    p_be_map      = cfg.get('p_be', DEFAULT_CFG['p_be'])
    min_ev        = cfg.get('min_package_ev', 0.02)
    max_same_game = cfg.get('max_same_game_legs', 2)
    preferred     = cfg.get('preferred_slips', ['5_flex', '6_flex'])

    best_decision: Optional[Dict] = None

    for slip in preferred:
        n      = int(slip.split('_')[0])
        p_be   = p_be_map.get(slip, 0.543)
        payouts = PAYOUTS.get(slip, {})

        if len(pool) < n:
            continue

        for combo in _select_combos(pool, n, max_same_game):
            probs = [c.p_true for c in combo]
            avg_p = sum(probs) / n
            if avg_p < p_be:
                continue
            ev = flex_ev(probs, payouts)
            if ev < min_ev:
                continue
            cand = {
                'path':       'SYSTEM_FIRE',
                'slip_type':  slip,
                'legs':       combo,
                'avg_p':      avg_p,
                'p_be':       p_be,
                'package_ev': ev,
            }
            if best_decision is None or cand['package_ev'] > best_decision['package_ev']:
                best_decision = cand

    if best_decision:
        return best_decision

    # Report best avg_p seen for diagnostics
    best_avg = 0.0
    if pool:
        for slip in preferred:
            n = int(slip.split('_')[0])
            if len(pool) >= n:
                top = pool[:n]
                avg = sum(l.p_true for l in top) / n
                best_avg = max(best_avg, avg)

    return {
        'path':        'NO_GO',
        'reason':      'no_package_clears_gate',
        'best_avg_p':  round(best_avg, 4),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Master entrypoint (Section 9)
# ─────────────────────────────────────────────────────────────────────────────

# ─────────────────────────────────────────────────────────────────────────────
# fair_p_market — extract fair P from sharpFairLine (MoneyLine DK/FD)
# ─────────────────────────────────────────────────────────────────────────────

def fair_p_market(row: Dict, side: str) -> Optional[float]:
    """
    Use sharpFairLine from MoneyLine (DK/FD) as the market fair probability.
    Converts fair line → P(More) via CDF estimate.
    Returns None if no sharp data.
    """
    sharp_fair = row.get('sharpFairLine') or row.get('sharp_fair_line')
    if sharp_fair is None:
        return None
    try:
        sf = float(sharp_fair)
        L  = float(row.get('lineScore', row.get('line', 0)) or 0)
        stat_type = str(row.get('statType', row.get('stat_type', '')) or '')
        sigma = _sigma_for(stat_type, L)
        p_more_val = _phi((sf - L) / sigma) if sigma > 0 else (1.0 if sf > L else 0.0)
        return p_more_val if side == 'more' else 1.0 - p_more_val
    except (ValueError, TypeError):
        return None


def _blend_p_true(p_mkt: Optional[float], p_mod: float, p_hr: Optional[float], cfg: Dict) -> float:
    """
    Weighted blend: market 50%, model 30%, hit_rate 20%.
    Falls back gracefully when signals are missing.
    """
    w_m = cfg.get('w_market', 0.50)
    w_p = cfg.get('w_model', 0.30)
    w_h = cfg.get('w_hitrate', 0.20)
    haircut = cfg.get('p_haircut', 0.005)

    total_w = 0.0
    total_p = 0.0

    if p_mkt is not None:
        total_p += w_m * p_mkt
        total_w += w_m
    if p_mod is not None:
        total_p += w_p * p_mod
        total_w += w_p
    if p_hr is not None:
        total_p += w_h * float(p_hr)
        total_w += w_h

    if total_w <= 0:
        return 0.50

    blended = total_p / total_w
    return _clamp(blended - haircut, 0.01, 0.99)


# ─────────────────────────────────────────────────────────────────────────────
# run_system_for_game — per-game System run (spec §run_system_for_game)
# ─────────────────────────────────────────────────────────────────────────────

def run_system_for_game(
    game_id: str,
    tiles:   List[Dict],
    models:  Dict,
    sharps:  Dict,
    context: Dict,
    cfg:     Dict,
) -> Dict:
    """
    Per-game version of The System per spec:
      - fair_p_market (weakness attack via sharpFairLine)
      - blend(p_mkt, p_mod, p_hr) - p_haircut
      - no_real_signal → kill
      - build best package across n_legs_options × slip_types
      - compute_ev → PLAY if ev >= play_ev_min, else LEAN, else NO_GO
    """
    # Filter to this game, non-demons only
    game_tiles = [t for t in tiles if
                  (t.get('gameId') or t.get('game_id') or '') == game_id
                  and not (t.get('isDemon') or t.get('is_demon'))]

    scored: List[ScoredLeg] = []
    for row in game_tiles:
        player_id = str(row.get('playerId') or row.get('player_id') or row.get('playerName') or '')
        stat_type = str(row.get('statType') or row.get('stat_type') or '')
        m   = models.get((player_id, stat_type))
        sh  = sharps.get(player_id, [])
        ctx = context.get(player_id, {})

        # Inject real projection mu/sigma from prop row (stamped by mlb_projections.py)
        # This is the primary model signal when no external model dict is provided.
        if m is None:
            proj_mu    = row.get('projMu')    or row.get('proj_mu')
            proj_sigma = row.get('projSigma') or row.get('proj_sigma')
            proj_n     = row.get('projNGames')or row.get('proj_n_games')
            if proj_mu is not None:
                m = {
                    'mu':     float(proj_mu),
                    'sigma':  float(proj_sigma) if proj_sigma else float(proj_mu) * 0.45,
                    'n':      int(proj_n) if proj_n else 0,
                    'source': row.get('projSource') or 'mlb_projections',
                }

        # Enrich ctx with PropContext 6-factor scores from the prop row
        if _HAVE_ENGINE_CTX:
            try:
                _pctx   = prop_context_from_dict({**ctx, **row})
                _scores = _engine_score_ctx(_pctx)
                ctx = {**ctx, **_scores}
            except Exception:
                pass  # fall back to raw ctx if engine unavailable

        # Score both sides
        legs = score_prop(row, m, sh, ctx, cfg)

        # Apply spec blend: replace p_true with fair blend
        for leg in legs:
            p_mkt = fair_p_market(row, leg.side)
            p_hr_raw = row.get('hitRate') or row.get('hit_rate')
            p_hr = float(p_hr_raw) if p_hr_raw is not None else None
            if _has_real_signal(row, sh, m):
                blended = _blend_p_true(p_mkt, leg.p_model, p_hr, cfg)
                leg = ScoredLeg(
                    **{**leg.__dict__,
                       'p_true': blended,
                       'count': count(blended, leg.p_be_5flex)}
                )
                # Re-check floor with blended p
                if blended < cfg.get('absolute_floor_p', 0.50):
                    leg.kill_reasons.append('below_floor_blend')
                    leg.eligible = False
            scored.append(leg)

    eligible = [s for s in scored if s.eligible]

    if len(eligible) < 2:
        return {
            'path': 'NO_GO', 'decision': 'NO_GO', 'status': 'NO_GO',
            'reason': 'lean_inventory_short', 'best_avg_p': 0.0,
            'game_id': game_id, 'legs': [],
        }

    # Build best package across all slip types
    play_ev_min = cfg.get('play_ev_min', 0.04)
    lean_ev_min = cfg.get('lean_ev_min', 0.00)

    # Score all candidate packages across all slip types
    # Goal: maximize multiplier subject to EV >= play_ev_min
    # Among feasible (EV >= play_ev_min): pick highest multiplier
    # Else: highest EV (LEAN)
    # Optional ceiling: second slip at 0.25u if EV>0 but high variance

    all_slips = ['2_power', '3_power', '4_power', '5_flex', '6_flex']
    p_be_map  = cfg.get('p_be', DEFAULT_CFG['p_be'])
    max_same  = cfg.get('max_same_game_legs', 6)

    candidates = []
    pool = sorted(_dedup_pool(eligible), key=lambda x: x.count, reverse=True)

    for slip in all_slips:
        n       = int(slip.split('_')[0])
        p_be    = p_be_map.get(slip, 0.543)
        payouts = PAYOUTS.get(slip, {})
        mult    = MULTIPLIERS.get(slip, 1.0)
        if len(pool) < n:
            continue
        for combo in _select_combos(pool, n, max_same):
            probs = [c.p_true for c in combo]
            avg_p = sum(probs) / n
            if avg_p < p_be * 0.90:   # pre-filter obvious losers
                continue
            ev = flex_ev(probs, payouts) if 'flex' in slip else power_ev(probs, mult)
            # Variance proxy: std dev of probs
            mean_p = avg_p
            variance = sum((p - mean_p) ** 2 for p in probs) / n
            candidates.append({
                'path':       'SYSTEM_FIRE',
                'slip_type':  slip,
                'legs':       combo,
                'avg_p':      avg_p,
                'p_be':       p_be,
                'package_ev': ev,
                'multiplier': mult,
                'variance':   variance,
                'game_id':    game_id,
            })

    if not candidates:
        return {
            'path': 'NO_GO', 'decision': 'NO_GO', 'status': 'NO_GO',
            'reason': 'no_package_clears_gate', 'best_avg_p': 0.0,
            'game_id': game_id, 'legs': [],
        }

    # Feasible = EV >= play_ev_min (subject to constraint)
    feasible = [c for c in candidates if c['package_ev'] >= play_ev_min]

    ceiling_slip = None

    if feasible:
        # Primary: among feasible, maximize multiplier (then EV as tiebreak)
        chosen = max(feasible, key=lambda c: (c['multiplier'], c['package_ev']))
        status = 'PLAY'
        stake_key = 'stake_play'

        # Optional ceiling slip: highest EV feasible slip if it differs from chosen
        # and has high variance → offer as 0.25u side bet
        by_ev = max(feasible, key=lambda c: c['package_ev'])
        if (by_ev['slip_type'] != chosen['slip_type']
                and by_ev['package_ev'] > chosen['package_ev']
                and by_ev['variance'] > 0.04):
            ceiling_slip = {**by_ev, 'stake_pct': 0.0025, 'status': 'CEILING'}

    else:
        # No feasible — fall back to best EV (LEAN)
        best = max(candidates, key=lambda c: c['package_ev'])
        if best['package_ev'] >= lean_ev_min:
            chosen = best
            status = 'LEAN'
            stake_key = 'stake_lean'
        else:
            return {
                'path': 'NO_GO', 'decision': 'NO_GO', 'status': 'NO_GO',
                'reason': 'ev_below_lean_floor',
                'best_ev': round(best['package_ev'], 4),
                'best_avg_p': round(best['avg_p'], 4),
                'game_id': game_id, 'legs': [],
            }

    # Soft gate: if ANY leg in chosen is lean_only → cap to LEAN
    chosen_legs = chosen.get('legs', [])
    if status == 'PLAY' and any(
        getattr(leg, 'lean_only', False) for leg in chosen_legs
    ):
        status    = 'LEAN'
        stake_key = 'stake_lean'
        # also cap ceiling slip (remove it — LEAN doesn't offer ceiling)
        ceiling_slip = None

    chosen['path']     = 'SYSTEM_FIRE'
    chosen['decision'] = 'SYSTEM_FIRE'
    chosen['status']   = status
    chosen['stake_pct'] = cfg.get(stake_key, 0.01)
    if ceiling_slip:
        chosen['ceiling_slip'] = ceiling_slip
    return chosen


def run_the_system(
    board:   List[Dict],
    models:  Dict[Tuple[str, str], Dict],   # (player_id, stat_type) → {mu, sigma}
    sharps:  Dict[str, List[Dict]],          # player_id → [SharpQuote]
    context: Dict[str, Dict],               # player_id → ContextFlags
    cfg:     Optional[Dict] = None,
) -> Dict:
    """
    Run The System over a slate board.

    Returns a SystemDecision dict:
      { path: "LOCKED" | "SYSTEM_FIRE" | "NO_GO", ... }
    """
    cfg = {**DEFAULT_CFG, **(cfg or {})}

    scored: List[ScoredLeg] = []
    for row in board:
        player_id = str(row.get('playerId') or row.get('player_id') or row.get('playerName') or '')
        stat_type = str(row.get('statType') or row.get('stat_type') or '')
        m   = models.get((player_id, stat_type))
        sh  = sharps.get(player_id, [])
        ctx = dict(context.get(player_id, {}))
        # Inject real projection mu/sigma from prop row
        if m is None:
            proj_mu    = row.get('projMu')    or row.get('proj_mu')
            proj_sigma = row.get('projSigma') or row.get('proj_sigma')
            proj_n     = row.get('projNGames')or row.get('proj_n_games')
            if proj_mu is not None:
                m = {
                    'mu':     float(proj_mu),
                    'sigma':  float(proj_sigma) if proj_sigma else float(proj_mu) * 0.45,
                    'n':      int(proj_n) if proj_n else 0,
                    'source': row.get('projSource') or 'mlb_projections',
                }
        # Enrich ctx with PropContext 6-factor scores from the prop row
        if _HAVE_ENGINE_CTX:
            try:
                _pctx   = prop_context_from_dict({**ctx, **row})
                _scores = _engine_score_ctx(_pctx)
                ctx = {**ctx, **_scores}
            except Exception:
                pass
        scored.extend(score_prop(row, m, sh, ctx, cfg))

    # Validate sides
    for leg in scored:
        assert leg.side in ('more', 'less'), f'invalid side {leg.side}'

    # Path A
    locked = lock_scan(scored, cfg)
    if locked:
        assert locked.get('path') == 'LOCKED'
        return locked

    eligible = [s for s in scored if s.eligible]
    if len(eligible) < 2:
        return {'path': 'NO_GO', 'reason': 'insufficient_eligible_legs', 'best_avg_p': 0.0}

    # Path B
    decision = build_packages(eligible, cfg)

    # Validate
    assert decision.get('path') in ('LOCKED', 'SYSTEM_FIRE', 'NO_GO'), \
        f"bad decision path: {decision.get('path')}"
    if decision.get('path') == 'SYSTEM_FIRE':
        assert decision['avg_p'] >= decision['p_be'], \
            f"avg_p {decision['avg_p']} < p_be {decision['p_be']}"
        assert decision['package_ev'] >= cfg['min_package_ev'], \
            f"ev {decision['package_ev']} < min {cfg['min_package_ev']}"

    return decision


# ─────────────────────────────────────────────────────────────────────────────
# Format output for API / frontend (Section 10)
# ─────────────────────────────────────────────────────────────────────────────

def _leg_to_dict(leg: ScoredLeg) -> Dict:
    return {
        'prop_id':     leg.prop_id,
        'player':      leg.player_name,
        'player_id':   leg.player_id,
        'stat':        leg.stat_type,
        'side':        leg.side,
        'line':        leg.line,
        'p_true':      round(leg.p_true, 4),
        'p_model':     round(leg.p_model, 4),
        'p_sharp':     round(leg.p_sharp, 4),
        'count':       round(leg.count, 4),
        'sharp_gap':   round(leg.sharp_gap, 3),
        'fragility':   round(leg.fragility, 3),
        'traps':       leg.trap_flags,
        'kill_reasons': leg.kill_reasons,
        'eligible':        leg.eligible,
        'game_id':         leg.game_id,
        'is_demon':        leg.is_demon,
        'is_goblin':       leg.is_goblin,
        'sample_factor':   round(getattr(leg, 'sample_factor', 1.0), 3),
        'low_confidence':  getattr(leg, 'low_confidence', False),
        'lean_only':       getattr(leg, 'lean_only', False),
        'n_games':         getattr(leg, 'n_games', 0),
        # 6-factor confidence
        'conf_score':      round(getattr(leg, 'conf_score', 0.0), 3),
        'conf_tier':       getattr(leg, 'conf_tier', 'NOISE'),
        'conf_factors':    getattr(leg, 'conf_factors', []),
        'conf_missing':    getattr(leg, 'conf_missing', []),
    }


def format_system_output(decision: Dict, slate_id: str = '') -> Dict:
    """
    Convert internal SystemDecision to the API response shape (Section 10).
    """
    path = decision.get('path', 'NO_GO')
    out: Dict[str, Any] = {
        'system_version': '1.0',
        'slate_id':       slate_id,
        'decision':       path,
        'path':           path,
        'no_go_reason':   None,
        'lock':           None,
    }

    if path == 'SYSTEM_FIRE':
        legs = decision.get('legs', [])
        out.update({
            'slip_type':   decision.get('slip_type', '5_flex'),
            'avg_p':       round(decision.get('avg_p', 0.0), 4),
            'p_be':        round(decision.get('p_be', 0.543), 4),
            'package_ev':  round(decision.get('package_ev', 0.0), 4),
            'unit_stake_pct': DEFAULT_CFG['unit_pct_bankroll'],
            'legs':        [_leg_to_dict(l) for l in legs],
            'rejected_top': [],
        })
    elif path == 'LOCKED':
        out.update({
            'guaranteed_roi': decision.get('guaranteed_roi', 0.0),
            'stakes':         decision.get('stakes', {}),
            'hedges':         decision.get('hedges', []),
            'legs':           [_leg_to_dict(l) for l in decision.get('legs', [])],
        })
    else:  # NO_GO
        out['no_go_reason'] = decision.get('reason', 'unknown')
        out['best_avg_p']   = decision.get('best_avg_p', 0.0)
        out['legs']         = []

    return out


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point (called from optimize.py via subprocess / direct import)
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    try:
        payload  = json.loads(sys.stdin.read())
        board    = payload.get('props', [])
        slate_id = payload.get('slate_id', '')
        cfg      = payload.get('cfg', {})

        # No model/sharp/context feed in v1 — pass empty dicts, system estimates from line
        decision = run_the_system(board, {}, {}, {}, cfg)
        output   = format_system_output(decision, slate_id)
        print(json.dumps(output))
    except Exception as exc:
        log.exception('leg_selector fatal error')
        print(json.dumps({'path': 'NO_GO', 'decision': 'NO_GO',
                          'no_go_reason': str(exc), 'legs': []}))
        sys.exit(1)
