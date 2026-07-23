#!/usr/bin/env python3
"""
GOTit Demon Pipeline — Guilty Until Proven Innocent, Always Return 2
=====================================================================

Every Demon starts as a house-favored trap.
Survival filter runs at strict thresholds first.
If fewer than 2 survive, soft thresholds relax in steps until 2 remain.
Hard gates (injury, role-unstable, overpriced vs sharp) NEVER relax.

Survival score formula (after all gates pass):
  40% market agreement
  25% role stability
  20% matchup / script support
  15% normal-volume path to hit

Then:
  1. Sort descending by survival score
  2. Correlation check — reject conflicting script pairs
  3. Take top 2 distinct-player demons

Returned count is always 2 if at least 2 demons exist anywhere in the pool.
If truly fewer than 2 demons are present in the input, return however many exist.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Excluded stats — HARD GATE, never relaxed
# Structurally fragile, single-event, or purely luck-driven
# ─────────────────────────────────────────────────────────────────────────────
DEMON_EXCLUDED_STATS: set = {
    'Plate Appearances',
    'Pitcher Strikeouts (Combo)',
    '1st Inning Walks Allowed',
    'Triples',
    'RBIs',
    'Singles',
    'Hits+Runs+RBIs',
    'Hits',
    'Hitter Strikeouts',
    'Doubles',
    'Walks',
    'Home Runs',
    'Stolen Bases',
    'Runs',
}

# ─────────────────────────────────────────────────────────────────────────────
# Line floors — HARD GATE, never relaxed
# Lines below floor require an outlier event to hit
# ─────────────────────────────────────────────────────────────────────────────
DEMON_LINE_FLOOR: Dict[str, float] = {
    'Total Bases':           2.5,
    'Hitter Fantasy Score':  25.0,
    'Pitcher Strikeouts':    3.5,
    'Pitching Outs':         9.5,
    'Pitches Thrown':        70.0,
    'Pitcher Fantasy Score': 25.0,
    'Earned Runs Allowed':   0.5,
    'Hits Allowed':          2.5,
    'Significant Strikes':   25.0,
    'Takedowns':             1.5,
    'Fight Time':            8.0,
    '_default':              1.5,
}

# Stat CV for log-normal p_win estimation
STAT_CV: Dict[str, float] = {
    'Pitcher Strikeouts':    0.35,
    'Pitches Thrown':        0.18,
    'Pitcher Fantasy Score': 0.55,
    'Total Bases':           0.85,
    'Hitter Fantasy Score':  0.75,
    'Earned Runs Allowed':   0.90,
    'Hits Allowed':          0.65,
    'Significant Strikes':   1.20,
    'Takedowns':             1.10,
    'Fight Time':            0.50,
    '_default':              0.70,
}

DEMON_RATIO: Dict[str, float] = {
    'Pitcher Strikeouts':    1.35,
    'Pitches Thrown':        1.15,
    'Pitcher Fantasy Score': 1.30,
    'Earned Runs Allowed':   1.80,
    'Hitter Fantasy Score':  1.45,
    'Total Bases':           1.45,
    'Hits Allowed':          1.40,
    'Significant Strikes':   1.40,
    'Takedowns':             1.50,
    'Fight Time':            1.20,
    '_default':              1.40,
}

# ─────────────────────────────────────────────────────────────────────────────
# Soft thresholds — can relax in steps to reach 2 survivors
# ─────────────────────────────────────────────────────────────────────────────
# Each tier is (prob_floor, market_tolerance, role_floor, label)
# Tier 0 = strictest, Tier N = most relaxed soft floor
SOFT_TIERS: List[Tuple[float, float, float, str]] = [
    (0.62, 0.50, 0.65, 'strict'),
    (0.59, 0.75, 0.60, 'relaxed_1'),
    (0.56, 1.00, 0.55, 'relaxed_2'),
    (0.53, 1.50, 0.50, 'relaxed_3'),  # floor: never below 0.53 (breakeven)
]

# Near-certain ceiling — HARD, never relaxed
PROB_CEILING = 0.92

# Locked-role stat types (role_pass auto-passes with no role data)
LOCKED_ROLE_STATS = {
    # Pitching stats — starter role locked by definition
    'Pitcher Strikeouts', 'Pitches Thrown', 'Pitching Outs',
    'Hits Allowed', 'Earned Runs Allowed', 'Pitcher Fantasy Score',
    'Significant Strikes', 'Walks Allowed',
    # Hitting stats — requires a starting lineup spot (locked role)
    'Total Bases', 'Hitter Fantasy Score',
    # Combat sports — fighter always participates
    'Fight Time', 'Takedowns',
}

SCORE_FLOOR = 0.001


# ─────────────────────────────────────────────────────────────────────────────
# Data structure
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DemonRecord:
    prop_id:         str
    game_id:         str
    player_id:       str
    player_name:     str
    stat_type:       str
    pp_line:         float
    sharp_line:      Optional[float] = None
    shade_signal:    str             = 'no_data'
    line_move:       int             = 0
    first_seen_line: Optional[float] = None
    proj_hit_prob:   Optional[float] = None
    recent_role:     Optional[float] = None
    matchup_flag:    Optional[str]   = None
    injury_flag:     bool            = False
    script_flag:     Optional[str]   = None
    raw:             dict            = field(default_factory=dict)

    # Pipeline outputs
    eligible_demon:  bool          = False
    demon_score:     float         = 0.0
    gates_passed:    List[str]     = field(default_factory=list)
    gates_failed:    List[str]     = field(default_factory=list)
    reject_reason:   Optional[str] = None
    tier_used:       str           = 'strict'


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _estimate_p_win(line: float, stat_type: str) -> float:
    if line <= 0:
        return 0.0
    cv        = STAT_CV.get(stat_type, STAT_CV['_default'])
    ratio     = DEMON_RATIO.get(stat_type, DEMON_RATIO['_default'])
    true_mean = line * ratio
    sigma     = cv * true_mean
    if sigma <= 0:
        return 0.999
    mu_ln    = math.log(true_mean) - 0.5 * math.log(1 + (sigma / true_mean) ** 2)
    sigma_ln = math.sqrt(math.log(1 + (sigma / true_mean) ** 2))
    z        = (math.log(max(line, 0.001)) - mu_ln) / sigma_ln
    return float(max(0.0, min(0.999, 0.5 * math.erfc(z / math.sqrt(2)))))


def _normalize(d: dict, sharp_map: Optional[Dict[str, dict]]) -> Optional[DemonRecord]:
    if not d.get('isDemon'):
        return None
    import hashlib
    prop_id = str(
        d.get('propId') or d.get('id') or d.get('sourcePropId') or
        hashlib.md5(f"{d.get('playerName','')}{d.get('statType','')}{d.get('lineScore','')}".encode()).hexdigest()[:12]
    )
    game_id     = str(d.get('gameId') or d.get('game_id') or '')
    player_id   = str(d.get('playerId') or d.get('player_id') or
                      hashlib.md5((d.get('playerName') or '').lower().encode()).hexdigest()[:16])
    player_name = str(d.get('playerName') or '')
    stat_type   = str(d.get('statType') or '')
    try:
        pp_line = float(d.get('lineScore') or 0)
    except (TypeError, ValueError):
        pp_line = 0.0

    sharp_entry = (sharp_map or {}).get(prop_id, {})
    sharp_line  = sharp_entry.get('fair_line') or d.get('sharpFairLine') or d.get('sharp_fair_line')

    return DemonRecord(
        prop_id=prop_id,
        game_id=game_id,
        player_id=player_id,
        player_name=player_name,
        stat_type=stat_type,
        pp_line=pp_line,
        sharp_line=float(sharp_line) if sharp_line is not None else None,
        shade_signal=str(d.get('ppShadeSignal') or d.get('pp_shade_signal') or 'no_data'),
        line_move=int(d.get('lineMoveCount') or d.get('line_move_count') or 0),
        first_seen_line=(
            float(d.get('firstSeenLine') or d.get('first_seen_line'))
            if (d.get('firstSeenLine') or d.get('first_seen_line')) else None
        ),
        proj_hit_prob=(
            float(d.get('pWin') or d.get('p_win'))
            if (d.get('pWin') or d.get('p_win')) else None
        ),
        recent_role=(
            float(d.get('roleCertainty') or d.get('role_certainty'))
            if (d.get('roleCertainty') or d.get('role_certainty')) else None
        ),
        matchup_flag=d.get('matchupFlag') or d.get('matchup_flag'),
        injury_flag=bool(d.get('injuryFlag') or d.get('injury_flag')),
        script_flag=d.get('scriptFlag') or d.get('script_flag'),
        raw=d,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Hard gates — never relax
# ─────────────────────────────────────────────────────────────────────────────

def _hard_gate_excluded_stat(rec: DemonRecord) -> Optional[str]:
    """Returns reject reason string if hard-blocked, else None."""
    if rec.stat_type in DEMON_EXCLUDED_STATS:
        return 'excluded_stat'
    return None


def _hard_gate_line_floor(rec: DemonRecord) -> Optional[str]:
    floor = DEMON_LINE_FLOOR.get(rec.stat_type, DEMON_LINE_FLOOR['_default'])
    if rec.pp_line < floor:
        return f'line_below_floor_{floor}'
    return None


def _hard_gate_injury(rec: DemonRecord) -> Optional[str]:
    if rec.injury_flag:
        return 'injury_flag'
    return None


def _hard_gate_overpriced(rec: DemonRecord, market_tolerance: float) -> Optional[str]:
    """PP line must not be more than tolerance ABOVE sharp line."""
    if rec.sharp_line is not None:
        if rec.pp_line > rec.sharp_line + market_tolerance:
            return f'overpriced_vs_sharp_pp={rec.pp_line}_sharp={rec.sharp_line}'
    return None


def _hard_gate_near_certain(rec: DemonRecord) -> Optional[str]:
    p = _estimate_p_win(rec.pp_line, rec.stat_type)
    rec.proj_hit_prob = round(p, 4)
    if p >= PROB_CEILING:
        return f'near_certain_house_trap_p={p:.3f}'
    return None


def _hard_gate_script_conflict(rec: DemonRecord) -> Optional[str]:
    if rec.script_flag == 'conflicts':
        return 'script_conflicts'
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Soft gates — relax across tiers
# ─────────────────────────────────────────────────────────────────────────────

def _soft_gate_probability(rec: DemonRecord, prob_floor: float) -> Optional[str]:
    p = rec.proj_hit_prob if rec.proj_hit_prob is not None else _estimate_p_win(rec.pp_line, rec.stat_type)
    rec.proj_hit_prob = round(p, 4)
    if p < prob_floor:
        return f'p_win={p:.3f}_below_floor={prob_floor}'
    return None


def _soft_gate_role(rec: DemonRecord, role_floor: float) -> Optional[str]:
    if rec.recent_role is not None:
        if rec.recent_role < role_floor:
            return f'role={rec.recent_role:.2f}_below_floor={role_floor}'
        return None
    # No role data — locked-role stats pass automatically
    if rec.stat_type in LOCKED_ROLE_STATS:
        return None
    return 'no_role_data_non_locked_stat'


def _soft_gate_matchup(rec: DemonRecord) -> Optional[str]:
    """Matchup is soft — only fail on explicit 'unfavorable'."""
    if rec.matchup_flag == 'unfavorable':
        return 'matchup_unfavorable'
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Survival score
# ─────────────────────────────────────────────────────────────────────────────

def _survival_score(rec: DemonRecord) -> float:
    """
    40% market agreement
    25% role stability
    20% matchup / script support
    15% normal-volume path to hit
    """
    # ── 40%: market agreement ─────────────────────────────────────────────────
    market = 0.0
    if rec.sharp_line is not None:
        gap = rec.sharp_line - rec.pp_line  # positive = PP line below sharp = easier over
        if gap >= 0.5:
            market = 1.0
        elif gap >= 0.0:
            market = 0.5 + gap  # 0.5–1.0 proportional
        else:
            market = max(0.0, 0.5 + gap * 0.5)  # slight gap above sharp, penalise
    elif rec.shade_signal == 'lean_over':
        market = 0.65
    elif rec.shade_signal == 'neutral':
        market = 0.40
    else:
        market = 0.20  # lean_under or no_data

    # Line movement confirmation (small bonus)
    if rec.line_move >= 1 and rec.first_seen_line is not None and rec.pp_line >= rec.first_seen_line:
        market = min(1.0, market + 0.05 * min(rec.line_move, 3))

    # ── 25%: role stability ───────────────────────────────────────────────────
    if rec.recent_role is not None:
        role = min(1.0, rec.recent_role)
    elif rec.stat_type in LOCKED_ROLE_STATS:
        role = 0.75  # implied locked
    else:
        role = 0.40  # unknown

    # ── 20%: matchup / script ─────────────────────────────────────────────────
    if rec.matchup_flag == 'favorable':
        matchup = 0.90
    elif rec.matchup_flag == 'unfavorable':
        matchup = 0.10  # gate already blocks this, but score it low anyway
    else:
        matchup = 0.50  # neutral / no data

    if rec.script_flag == 'fits':
        matchup = min(1.0, matchup + 0.15)
    elif rec.script_flag == 'conflicts':
        matchup = max(0.0, matchup - 0.30)

    # ── 15%: normal-volume path ───────────────────────────────────────────────
    floor = DEMON_LINE_FLOOR.get(rec.stat_type, DEMON_LINE_FLOOR['_default'])
    difficulty_ratio = rec.pp_line / max(floor, 1.0)
    # Sweet spot: 1.0–2.0x the floor = normal volume, not ceiling-chasing
    if 1.0 <= difficulty_ratio <= 2.0:
        path = 0.80
    elif difficulty_ratio < 1.0:
        path = 0.20  # blocked by gate anyway
    else:
        path = max(0.30, 0.80 - (difficulty_ratio - 2.0) * 0.20)  # gets harder above 2x

    p = rec.proj_hit_prob or _estimate_p_win(rec.pp_line, rec.stat_type)
    path = min(1.0, path + (p - 0.55) * 0.30)

    score = 0.40 * market + 0.25 * role + 0.20 * matchup + 0.15 * path
    return round(min(1.0, max(0.0, score)), 4)


# ─────────────────────────────────────────────────────────────────────────────
# Correlation check
# ─────────────────────────────────────────────────────────────────────────────

def _scripts_conflict(a: DemonRecord, b: DemonRecord) -> bool:
    """
    Two demons conflict if they need incompatible game scripts to hit.
    Conservative rule: same player is already blocked by distinct-player check.
    Script conflict: both need extreme volume events that can't co-exist.
    """
    # Both need 'blowout only' scripts — only one side can blow out
    conflict_pairs = [
        ('blowout_pitcher', 'blowout_pitcher'),
        ('overtime', 'overtime'),
    ]
    sf_a = a.script_flag or ''
    sf_b = b.script_flag or ''
    for ca, cb in conflict_pairs:
        if ca in sf_a and cb in sf_b:
            return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Core pipeline per tier
# ─────────────────────────────────────────────────────────────────────────────

def _run_tier(
    candidates: List[DemonRecord],
    prob_floor: float,
    market_tolerance: float,
    role_floor: float,
    tier_label: str,
) -> List[DemonRecord]:
    """
    Run all gates at this tier's thresholds.
    Returns list of survivors (all gates passed).
    """
    survivors = []
    for rec in candidates:
        # Clone lists so relaxing tiers don't accumulate old failures
        rec.gates_passed = []
        rec.gates_failed = []
        rec.reject_reason = None

        failed = False

        # ── Hard gates first (never relax) ────────────────────────────────────
        for gate_name, gate_fn in [
            ('excluded_stat',     lambda r: _hard_gate_excluded_stat(r)),
            ('line_floor',        lambda r: _hard_gate_line_floor(r)),
            ('injury',            lambda r: _hard_gate_injury(r)),
            ('overpriced_sharp',  lambda r: _hard_gate_overpriced(r, market_tolerance)),
            ('near_certain',      lambda r: _hard_gate_near_certain(r)),
            ('script_conflict',   lambda r: _hard_gate_script_conflict(r)),
        ]:
            reason = gate_fn(rec)
            if reason:
                rec.gates_failed.append(gate_name)
                rec.reject_reason = reason
                failed = True
                log.info("[demon_tier=%s] HARD_FAIL %s %s %.1f gate=%s reason=%s",
                         tier_label, rec.player_name, rec.stat_type, rec.pp_line,
                         gate_name, reason)
                break

        if failed:
            continue

        # ── Soft gates (relax across tiers) ──────────────────────────────────
        for gate_name, gate_fn in [
            ('probability',  lambda r: _soft_gate_probability(r, prob_floor)),
            ('role',         lambda r: _soft_gate_role(r, role_floor)),
            ('matchup',      lambda r: _soft_gate_matchup(r)),
        ]:
            reason = gate_fn(rec)
            if reason:
                rec.gates_failed.append(gate_name)
                rec.reject_reason = reason
                failed = True
                log.info("[demon_tier=%s] SOFT_FAIL %s %s %.1f gate=%s reason=%s",
                         tier_label, rec.player_name, rec.stat_type, rec.pp_line,
                         gate_name, reason)
                break
            rec.gates_passed.append(gate_name)

        if failed:
            continue

        # ── Score ─────────────────────────────────────────────────────────────
        rec.demon_score   = _survival_score(rec)
        rec.eligible_demon = rec.demon_score >= SCORE_FLOOR
        rec.tier_used     = tier_label

        if rec.eligible_demon:
            log.info("[demon_tier=%s] PASS %s %s %.1f score=%.4f p_win=%.3f",
                     tier_label, rec.player_name, rec.stat_type, rec.pp_line,
                     rec.demon_score, rec.proj_hit_prob or 0)
            survivors.append(rec)

    return survivors


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def run_demon_pipeline(
    raw_props: List[dict],
    sharp_map: Optional[Dict[str, dict]] = None,
    max_demons: int = 2,
) -> List[dict]:
    """
    Full demon pipeline. Always returns max_demons distinct-player demons
    (or fewer only if the input has fewer unique demon players available).

    Algorithm:
      1. Normalize all demons
      2. Try SOFT_TIERS[0] (strictest) — if >= max_demons survive, done
      3. Relax soft thresholds one tier at a time until >= max_demons survive
      4. Hard gates (injury, overpriced, near-certain, excluded_stat, line_floor,
         script_conflict) NEVER relax
      5. Sort by survival score descending
      6. Correlation check — skip conflicting script pairs
      7. Take top max_demons distinct-player demons
    """
    sharp_map = sharp_map or {}

    # Normalize
    candidates: List[DemonRecord] = []
    for d in raw_props:
        rec = _normalize(d, sharp_map)
        if rec is None:
            continue
        candidates.append(rec)

    log.info("[demon_pipeline] %d demon candidates in input", len(candidates))

    if not candidates:
        log.info("[demon_pipeline] no demons in input — returning []")
        return []

    # Run tiers, stop as soon as we have enough survivors
    survivors: List[DemonRecord] = []
    tier_used = SOFT_TIERS[0][3]

    for prob_floor, market_tol, role_floor, label in SOFT_TIERS:
        log.info("[demon_pipeline] trying tier=%s prob_floor=%.2f market_tol=%.2f role_floor=%.2f",
                 label, prob_floor, market_tol, role_floor)
        survivors = _run_tier(
            [r for r in candidates],  # fresh copy each tier
            prob_floor=prob_floor,
            market_tolerance=market_tol,
            role_floor=role_floor,
            tier_label=label,
        )
        tier_used = label
        if len(survivors) >= max_demons:
            log.info("[demon_pipeline] tier=%s produced %d survivors — stopping relaxation",
                     label, len(survivors))
            break
        log.info("[demon_pipeline] tier=%s produced %d survivors — relaxing", label, len(survivors))

    # Sort by survival score descending
    survivors.sort(key=lambda r: r.demon_score, reverse=True)

    # Distinct-player selection with correlation check
    seen_players: set = set()
    top: List[DemonRecord] = []

    for rec in survivors:
        if rec.player_name in seen_players:
            continue
        # Correlation check against already-selected demons
        skip = False
        for selected in top:
            if _scripts_conflict(rec, selected):
                log.info("[demon_pipeline] CORRELATION_SKIP %s — script conflicts with %s",
                         rec.player_name, selected.player_name)
                skip = True
                break
        if skip:
            continue
        seen_players.add(rec.player_name)
        top.append(rec)
        if len(top) >= max_demons:
            break

    log.info("[demon_pipeline] final=%d tier=%s demons selected", len(top), tier_used)
    if len(top) < max_demons:
        log.warning("[demon_pipeline] WARNING: only %d/%d demons available after all tiers — "
                    "input had %d demon candidates",
                    len(top), max_demons, len(candidates))

    # Build output
    result = []
    for rec in top:
        out = {
            **rec.raw,
            'isDemon': True,
            'demonScore': {
                'composite':    rec.demon_score,
                'p_win':        rec.proj_hit_prob,
                'line':         rec.pp_line,
                'stat':         rec.stat_type,
                'tier':         rec.tier_used,
                'gates_passed': rec.gates_passed,
                'gates_failed': rec.gates_failed,
            },
        }
        result.append(out)

    return result
