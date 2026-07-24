#!/usr/bin/env python3
"""
GOTit Demon Pipeline — Guilty Until Proven Innocent, Always Return 2
=====================================================================

Priority bucket system:
  Bucket 1 (PREFERRED) — Total Bases, Walks Allowed
  Bucket 2 (SECONDARY) — Pitcher Strikeouts, Pitches Thrown, Pitching Outs,
                          Hits Allowed, Significant Strikes, Earned Runs Allowed,
                          Pitcher Fantasy Score, Rushing Attempts, Points+Rebounds+Assists
  Bucket 3 (JUNK — conditional) — Hitter Strikeouts, Singles, Hits, Runs, Walks,
                          Hitter Fantasy Score, RBIs, Home Runs, Stolen Bases, Doubles

Algorithm:
  1. Normalize all demons
  2. Try to fill max_demons slots from Bucket 1 first (strictest thresholds)
  3. If slots remain, try Bucket 2 (same thresholds, relax if needed)
  4. If still short, try Bucket 3 ONLY if p_win >= JUNK_PWIN_FLOOR (raised floor)
     and market explicitly supports it (sharp line or lean_over shade)
  5. Hard gates NEVER relax: injury, overpriced vs sharp, near-certain, line floor,
     script conflict
  6. Sort by survival score (40/25/20/15), correlation check, take top 2

Survival score:
  40% market agreement
  25% role stability
  20% matchup / script support
  15% normal-volume path to hit
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Bucket definitions
# ─────────────────────────────────────────────────────────────────────────────

# Bucket 1 — preferred: component stats, directly modellable
BUCKET_1: Set[str] = {
    'Total Bases',      # MLB — plate appearances + contact quality
    'Walks Allowed',    # MLB pitcher — directly maps to control + matchup
}

# Bucket 2 — secondary: stable-count pitching/sport volume stats
BUCKET_2: Set[str] = {
    'Pitcher Strikeouts',    # MLB — stable when role truly locked
    'Pitches Thrown',        # MLB — most stable pitcher stat
    'Pitching Outs',         # MLB — innings pitched proxy
    'Hits Allowed',          # MLB — starter quality + matchup
    'Significant Strikes',   # MMA — fight volume
    'Earned Runs Allowed',   # MLB — starter ERA proxy
    'Pitcher Fantasy Score', # MLB — composite, only if projection strong
    'Takedowns',             # MMA — fighter style stat
    'Fight Time',            # MMA — stable when both fighters tend to go deep
    # NFL/NBA equivalents
    'Rushing Attempts',
    'Points+Rebounds+Assists',
    'Points',
    'Rebounds',
    'Assists',
}

# Bucket 3 — junk: high-variance, heavily tuned, avoid unless forced
# Only used if JUNK conditions met (raised p_win floor + market support)
BUCKET_3: Set[str] = {
    'Hitter Strikeouts',
    'Singles',
    'Hits',
    'Runs',
    'Walks',
    'Hitter Fantasy Score',
    'RBIs',
    'Home Runs',
    'Stolen Bases',
    'Doubles',
    'Hits+Runs+RBIs',
}

# Hard-excluded forever — no bucket will ever touch these
HARD_EXCLUDED: Set[str] = {
    'Plate Appearances',
    'Pitcher Strikeouts (Combo)',
    '1st Inning Walks Allowed',
    'Triples',
}

# ─────────────────────────────────────────────────────────────────────────────
# Line floors — HARD GATE, never relaxed
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
    'Rushing Attempts':      10.0,
    'Points':                10.0,
    'Rebounds':              4.0,
    'Assists':               3.0,
    'Points+Rebounds+Assists': 15.0,
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
    'Rushing Attempts':      0.30,
    'Points':                0.40,
    'Rebounds':              0.55,
    'Assists':               0.60,
    'Points+Rebounds+Assists': 0.40,
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
    'Rushing Attempts':      1.20,
    'Points':                1.30,
    'Rebounds':              1.35,
    'Assists':               1.35,
    'Points+Rebounds+Assists': 1.30,
    '_default':              1.40,
}

# ───────���─────────────────────────────────────────────────────────────────────
# Thresholds
# ─────────────────────────────────────────────────────────────────────────────

# Soft tier ladder — (prob_floor, market_tolerance, role_floor, label)
SOFT_TIERS: List[Tuple[float, float, float, str]] = [
    (0.62, 0.50, 0.65, 'strict'),
    (0.59, 0.75, 0.60, 'relaxed_1'),
    (0.56, 1.00, 0.55, 'relaxed_2'),
    (0.53, 1.50, 0.50, 'relaxed_3'),
]

# Junk bucket (Bucket 3) gets a raised floor that never relaxes below this
JUNK_PWIN_FLOOR  = 0.65   # raised — junk needs stronger model confidence
JUNK_MARKET_ONLY = True   # junk always requires sharp line or lean_over shade

# Near-certain ceiling — HARD
PROB_CEILING = 0.92

# Locked-role stat types — role gate auto-passes when no role data
LOCKED_ROLE_STATS: Set[str] = {
    # MLB pitching — starter role implied
    'Pitcher Strikeouts', 'Pitches Thrown', 'Pitching Outs',
    'Hits Allowed', 'Earned Runs Allowed', 'Pitcher Fantasy Score',
    'Significant Strikes', 'Walks Allowed',
    # MLB hitting — lineup spot required
    'Total Bases', 'Hitter Fantasy Score',
    # Combat sports
    'Fight Time', 'Takedowns',
    # NFL/NBA
    'Rushing Attempts', 'Points', 'Rebounds', 'Assists',
    'Points+Rebounds+Assists',
}

SCORE_FLOOR = 0.001


# ──────────────��──────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _bucket_of(stat_type: str) -> Optional[int]:
    """Return 1, 2, 3, or None (hard-excluded)."""
    if stat_type in HARD_EXCLUDED:
        return None
    if stat_type in BUCKET_1:
        return 1
    if stat_type in BUCKET_2:
        return 2
    if stat_type in BUCKET_3:
        return 3
    # Unknown stat — treat as Bucket 2 conservatively
    return 2


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


def _normalize(d: dict, sharp_map: Dict[str, dict]) -> Optional['DemonRecord']:
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

    sharp_entry = sharp_map.get(prop_id, {})
    sharp_line  = sharp_entry.get('fair_line') or d.get('sharpFairLine') or d.get('sharp_fair_line')

    return DemonRecord(
        prop_id=prop_id,
        game_id=game_id,
        player_id=player_id,
        player_name=player_name,
        stat_type=stat_type,
        bucket=_bucket_of(stat_type),
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
# Data structure
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DemonRecord:
    prop_id:         str
    game_id:         str
    player_id:       str
    player_name:     str
    stat_type:       str
    bucket:          Optional[int]   # 1=preferred, 2=secondary, 3=junk, None=hard-excluded
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

    eligible_demon:  bool          = False
    demon_score:     float         = 0.0
    gates_passed:    List[str]     = field(default_factory=list)
    gates_failed:    List[str]     = field(default_factory=list)
    reject_reason:   Optional[str] = None
    tier_used:       str           = 'strict'


# ─────────────────────────────────────────────────────────────────────────────
# Hard gates — never relax
# ─────────────────────────────────────────────────────────────────────────────

def _hard_gate_excluded(rec: DemonRecord) -> Optional[str]:
    if rec.bucket is None:
        return 'hard_excluded_stat'
    return None


def _hard_gate_line_floor(rec: DemonRecord) -> Optional[str]:
    floor = DEMON_LINE_FLOOR.get(rec.stat_type, DEMON_LINE_FLOOR['_default'])
    if rec.pp_line < floor:
        return 'line_below_floor_' + str(floor)
    return None


def _hard_gate_injury(rec: DemonRecord) -> Optional[str]:
    return 'injury_flag' if rec.injury_flag else None


def _hard_gate_overpriced(rec: DemonRecord, market_tolerance: float) -> Optional[str]:
    if rec.sharp_line is not None and rec.pp_line > rec.sharp_line + market_tolerance:
        return 'overpriced_vs_sharp_pp=' + str(rec.pp_line) + '_sharp=' + str(rec.sharp_line)
    return None


def _hard_gate_near_certain(rec: DemonRecord) -> Optional[str]:
    p = _estimate_p_win(rec.pp_line, rec.stat_type)
    rec.proj_hit_prob = round(p, 4)
    if p >= PROB_CEILING:
        return 'near_certain_house_trap_p=' + str(round(p, 3))
    return None


def _hard_gate_script_conflict(rec: DemonRecord) -> Optional[str]:
    return 'script_conflicts' if rec.script_flag == 'conflicts' else None


# ─────────────────────────────────────────────────────────────────────────────
# Soft gates
# ─────────────────────────────────────────────────────────────────────────────

def _soft_gate_probability(rec: DemonRecord, prob_floor: float) -> Optional[str]:
    p = rec.proj_hit_prob if rec.proj_hit_prob is not None else _estimate_p_win(rec.pp_line, rec.stat_type)
    rec.proj_hit_prob = round(p, 4)
    if p < prob_floor:
        return 'p_win=' + str(round(p, 3)) + '_below_floor=' + str(prob_floor)
    return None


def _soft_gate_role(rec: DemonRecord, role_floor: float) -> Optional[str]:
    if rec.recent_role is not None:
        if rec.recent_role < role_floor:
            return 'role=' + str(round(rec.recent_role, 2)) + '_below_floor=' + str(role_floor)
        return None
    if rec.stat_type in LOCKED_ROLE_STATS:
        return None
    return 'no_role_data_non_locked_stat'


def _soft_gate_matchup(rec: DemonRecord) -> Optional[str]:
    return 'matchup_unfavorable' if rec.matchup_flag == 'unfavorable' else None


# Junk-bucket additional gate: must have market support
def _junk_gate_market_support(rec: DemonRecord) -> Optional[str]:
    if rec.sharp_line is not None:
        # Sharp data present — must not be overpriced (already checked) — pass
        return None
    if rec.shade_signal == 'lean_over':
        return None
    return 'junk_bucket_no_market_support_shade=' + rec.shade_signal


# ─────────────────────────────────────────────────────────────────────────────
# Survival score (40/25/20/15)
# ─────────────────────────────────────────────────────────────────────────────

def _survival_score(rec: DemonRecord) -> float:
    # 40%: market agreement
    market = 0.0
    if rec.sharp_line is not None:
        gap = rec.sharp_line - rec.pp_line
        if gap >= 0.5:
            market = 1.0
        elif gap >= 0.0:
            market = 0.5 + gap
        else:
            market = max(0.0, 0.5 + gap * 0.5)
    elif rec.shade_signal == 'lean_over':
        market = 0.65
    elif rec.shade_signal == 'neutral':
        market = 0.40
    else:
        market = 0.20
    if rec.line_move >= 1 and rec.first_seen_line is not None and rec.pp_line >= rec.first_seen_line:
        market = min(1.0, market + 0.05 * min(rec.line_move, 3))

    # 25%: role stability
    if rec.recent_role is not None:
        role = min(1.0, rec.recent_role)
    elif rec.stat_type in LOCKED_ROLE_STATS:
        role = 0.75
    else:
        role = 0.40

    # 20%: matchup / script
    if rec.matchup_flag == 'favorable':
        matchup = 0.90
    elif rec.matchup_flag == 'unfavorable':
        matchup = 0.10
    else:
        matchup = 0.50
    if rec.script_flag == 'fits':
        matchup = min(1.0, matchup + 0.15)
    elif rec.script_flag == 'conflicts':
        matchup = max(0.0, matchup - 0.30)

    # 15%: normal-volume path
    floor = DEMON_LINE_FLOOR.get(rec.stat_type, DEMON_LINE_FLOOR['_default'])
    ratio = rec.pp_line / max(floor, 1.0)
    if 1.0 <= ratio <= 2.0:
        path = 0.80
    elif ratio < 1.0:
        path = 0.20
    else:
        path = max(0.30, 0.80 - (ratio - 2.0) * 0.20)
    p = rec.proj_hit_prob or _estimate_p_win(rec.pp_line, rec.stat_type)
    path = min(1.0, path + (p - 0.55) * 0.30)

    # Bucket bonus — preferred stats score slightly higher at same thresholds
    bucket_bonus = {1: 0.05, 2: 0.0, 3: -0.05}.get(rec.bucket or 2, 0.0)

    score = 0.40 * market + 0.25 * role + 0.20 * matchup + 0.15 * path + bucket_bonus
    return round(min(1.0, max(0.0, score)), 4)


# ──────────────────────────────��──────────────────────────────────────────────
# Correlation check
# ─────────────────────────────────────────────────────────────────────────────

def _scripts_conflict(a: DemonRecord, b: DemonRecord) -> bool:
    conflict_pairs = [('blowout_pitcher', 'blowout_pitcher'), ('overtime', 'overtime')]
    sf_a = a.script_flag or ''
    sf_b = b.script_flag or ''
    for ca, cb in conflict_pairs:
        if ca in sf_a and cb in sf_b:
            return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Core per-tier runner
# ─────────────────────────────────────────────────────────────────────────────

def _run_tier(
    candidates: List[DemonRecord],
    prob_floor: float,
    market_tolerance: float,
    role_floor: float,
    tier_label: str,
    bucket_filter: Optional[Set[int]] = None,
    is_junk_pass: bool = False,
) -> List[DemonRecord]:
    survivors = []
    for rec in candidates:
        if bucket_filter is not None and rec.bucket not in bucket_filter:
            continue

        rec.gates_passed = []
        rec.gates_failed = []
        rec.reject_reason = None

        failed = False

        # Hard gates
        for gate_name, gate_fn in [
            ('excluded_stat',    lambda r: _hard_gate_excluded(r)),
            ('line_floor',       lambda r: _hard_gate_line_floor(r)),
            ('injury',           lambda r: _hard_gate_injury(r)),
            ('overpriced_sharp', lambda r: _hard_gate_overpriced(r, market_tolerance)),
            ('near_certain',     lambda r: _hard_gate_near_certain(r)),
            ('script_conflict',  lambda r: _hard_gate_script_conflict(r)),
        ]:
            reason = gate_fn(rec)
            if reason:
                rec.gates_failed.append(gate_name)
                rec.reject_reason = reason
                failed = True
                log.info("[demon_tier=%s|b%s] HARD_FAIL %s %s %.1f %s",
                         tier_label, rec.bucket, rec.player_name, rec.stat_type, rec.pp_line, reason)
                break

        if failed:
            continue

        # Junk-bucket extra gates (Bucket 3 only)
        if is_junk_pass:
            eff_floor = max(prob_floor, JUNK_PWIN_FLOOR)
            reason = _soft_gate_probability(rec, eff_floor)
            if reason:
                rec.gates_failed.append('junk_probability')
                rec.reject_reason = 'junk_' + reason
                failed = True
                log.info("[demon_tier=%s|b3] JUNK_PWIN_FAIL %s %s %.1f %s",
                         tier_label, rec.player_name, rec.stat_type, rec.pp_line, reason)
            if not failed:
                reason = _junk_gate_market_support(rec)
                if reason:
                    rec.gates_failed.append('junk_market')
                    rec.reject_reason = reason
                    failed = True
                    log.info("[demon_tier=%s|b3] JUNK_MARKET_FAIL %s %s %.1f %s",
                             tier_label, rec.player_name, rec.stat_type, rec.pp_line, reason)
            if failed:
                continue

        # Soft gates
        for gate_name, gate_fn in [
            ('probability', lambda r: _soft_gate_probability(r, prob_floor)),
            ('role',        lambda r: _soft_gate_role(r, role_floor)),
            ('matchup',     lambda r: _soft_gate_matchup(r)),
        ]:
            reason = gate_fn(rec)
            if reason:
                rec.gates_failed.append(gate_name)
                rec.reject_reason = reason
                failed = True
                log.info("[demon_tier=%s|b%s] SOFT_FAIL %s %s %.1f %s",
                         tier_label, rec.bucket, rec.player_name, rec.stat_type, rec.pp_line, reason)
                break
            rec.gates_passed.append(gate_name)

        if failed:
            continue

        rec.demon_score    = _survival_score(rec)
        rec.eligible_demon = rec.demon_score >= SCORE_FLOOR
        rec.tier_used      = tier_label

        if rec.eligible_demon:
            log.info("[demon_tier=%s|b%s] PASS %s %s %.1f score=%.4f p_win=%.3f",
                     tier_label, rec.bucket, rec.player_name, rec.stat_type, rec.pp_line,
                     rec.demon_score, rec.proj_hit_prob or 0)
            survivors.append(rec)

    return survivors


# ─────────────────────────────────────────────────────────────────────────────
# Distinct-player picker with correlation check
# ─────────────────────────────────────────────────────────────────────────────

def _pick_top(
    survivors: List[DemonRecord],
    already_selected: List[DemonRecord],
    needed: int,
) -> List[DemonRecord]:
    seen = {r.player_name for r in already_selected}
    picked = []
    for rec in sorted(survivors, key=lambda r: r.demon_score, reverse=True):
        if rec.player_name in seen:
            continue
        skip = False
        for sel in already_selected + picked:
            if _scripts_conflict(rec, sel):
                log.info("[demon_pipeline] CORRELATION_SKIP %s conflicts with %s",
                         rec.player_name, sel.player_name)
                skip = True
                break
        if skip:
            continue
        seen.add(rec.player_name)
        picked.append(rec)
        if len(picked) >= needed:
            break
    return picked


# ──────��──────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def run_demon_pipeline(
    raw_props: List[dict],
    sharp_map: Optional[Dict[str, dict]] = None,
    max_demons: int = 2,
) -> List[dict]:
    """
    Full demon pipeline. Always returns max_demons distinct-player demons
    (fewer only if the input genuinely has fewer unique demon players).

    Search order:
      1. Bucket 1 (Total Bases, Walks Allowed) — all soft tiers
      2. Bucket 2 (Pitcher Ks, Pitches Thrown, etc.) — all soft tiers
      3. Bucket 3 (junk) — strict only, raised p_win floor, market required
    """
    sharp_map = sharp_map or {}

    candidates: List[DemonRecord] = []
    for d in raw_props:
        rec = _normalize(d, sharp_map)
        if rec is None:
            continue
        candidates.append(rec)

    log.info("[demon_pipeline] %d demon candidates (b1=%d b2=%d b3=%d excluded=%d)",
             len(candidates),
             sum(1 for r in candidates if r.bucket == 1),
             sum(1 for r in candidates if r.bucket == 2),
             sum(1 for r in candidates if r.bucket == 3),
             sum(1 for r in candidates if r.bucket is None))

    if not candidates:
        return []

    selected: List[DemonRecord] = []

    # ── Pass 1: Bucket 1 — preferred stats ───────────────────────────────────
    for prob_floor, market_tol, role_floor, label in SOFT_TIERS:
        if len(selected) >= max_demons:
            break
        survivors = _run_tier(
            candidates, prob_floor, market_tol, role_floor, label,
            bucket_filter={1},
        )
        new_picks = _pick_top(survivors, selected, max_demons - len(selected))
        if new_picks:
            log.info("[demon_pipeline] bucket=1 tier=%s added %d picks", label, len(new_picks))
            selected.extend(new_picks)
        if len(selected) >= max_demons:
            break

    # ── Pass 2: Bucket 2 — secondary stats ───────────────────────────────────
    if len(selected) < max_demons:
        for prob_floor, market_tol, role_floor, label in SOFT_TIERS:
            if len(selected) >= max_demons:
                break
            survivors = _run_tier(
                candidates, prob_floor, market_tol, role_floor, label,
                bucket_filter={2},
            )
            new_picks = _pick_top(survivors, selected, max_demons - len(selected))
            if new_picks:
                log.info("[demon_pipeline] bucket=2 tier=%s added %d picks", label, len(new_picks))
                selected.extend(new_picks)
            if len(selected) >= max_demons:
                break

    # ── Pass 3: Bucket 3 — junk, strict only, raised floor ───────────────────
    if len(selected) < max_demons:
        log.info("[demon_pipeline] falling into bucket=3 junk pass — need %d more",
                 max_demons - len(selected))
        for prob_floor, market_tol, role_floor, label in SOFT_TIERS:
            if len(selected) >= max_demons:
                break
            survivors = _run_tier(
                candidates, prob_floor, market_tol, role_floor, label,
                bucket_filter={3},
                is_junk_pass=True,
            )
            new_picks = _pick_top(survivors, selected, max_demons - len(selected))
            if new_picks:
                log.info("[demon_pipeline] bucket=3 tier=%s added %d picks", label, len(new_picks))
                selected.extend(new_picks)
            if len(selected) >= max_demons:
                break

    log.info("[demon_pipeline] final=%d/%d demons selected", len(selected), max_demons)
    if len(selected) < max_demons:
        log.warning("[demon_pipeline] WARNING: only %d/%d demons — input had %d candidates",
                    len(selected), max_demons, len(candidates))

    result = []
    for rec in selected:
        result.append({
            **rec.raw,
            'isDemon': True,
            'demonScore': {
                'composite':    rec.demon_score,
                'p_win':        rec.proj_hit_prob,
                'line':         rec.pp_line,
                'stat':         rec.stat_type,
                'bucket':       rec.bucket,
                'tier':         rec.tier_used,
                'gates_passed': rec.gates_passed,
                'gates_failed': rec.gates_failed,
            },
        })

    return result
