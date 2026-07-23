#!/usr/bin/env python3
"""
GOTit Non-Demon Pipeline
========================
Decides which non-demon legs reach MILP — and why.

Pipeline steps (exact order):
  1.  ingest_props()
  2.  normalize_prop_record()
  3.  apply_hard_blocks()
  4.  evaluate_edge_gate()
  5.  evaluate_stability_gate()
  6.  score_nondemon_prop()
  7.  drop_if_score_zero_or_ineligible()
  8.  build_nondemon_candidate_pool()
  9.  create_milp_variables_from_pool_only()
  10. solve_milp_for_nondemons()
  11. emit_debug_table()

MILP never sees: demons, 0-score props, missing-data props, failed-gate props.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# ── OR-Tools ──────────────────────────────────────────────────────────────────
try:
    from ortools.linear_solver import pywraplp
    _ORTOOLS_OK = True
except ImportError:
    _ORTOOLS_OK = False
    log.error("OR-Tools not available — MILP will not run")


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

# Hard-blocked stat types — never eligible regardless of line
BLOCKED_STATS: set = {
    'Home Runs', 'Doubles', 'RBIs', 'Singles', 'Walks',
    'Triples', 'Stolen Bases', 'Hitter Strikeouts',
    'Plate Appearances', 'Pitcher Fantasy Score', 'Runs',
}

# Minimum line floors for non-blocked stats
LINE_FLOORS: Dict[str, float] = {
    'Total Bases':          2.5,
    'Hits+Runs+RBIs':       2.5,
    'Pitcher Strikeouts':   3.5,
    'Pitches Thrown':       70.0,
    'Hitter Fantasy Score': 7.0,
    'Hits Allowed':         2.5,
    'Significant Strikes':  25.0,
    'Takedowns':            1.5,
    'Fight Time':           8.0,
    'Hits':                 1.0,
    'Earned Runs Allowed':  1.0,
    'Pitching Outs':        10.0,
    'Walks Allowed':        1.0,
    '_default':             1.0,
}

# Stats whose UNDER side is also blocked
UNDER_BLOCKED_STATS: set = {
    'Plate Appearances', 'Singles', 'Runs', 'RBIs', 'Walks',
    'Hitter Strikeouts', 'Home Runs', 'Triples', 'Stolen Bases',
}

# Edge gate thresholds
SHARP_GAP_MIN        = 0.35   # sharp fair line vs pp line gap (stat units)
HIGH_WIN_PCT_FLOOR   = 0.53   # p_win threshold for strong signal
STRONG_ROLE_FLOOR    = 0.72   # role certainty signal
SCRIPT_FIT_FLOOR     = 0.58   # game script signal
LINE_MOVED_MIN       = 1      # confirmed line moves

# Score floor — below this even gated props are excluded
SCORE_FLOOR          = 0.01
STANDARD_PWIN_FLOOR  = 0.52   # absolute p_win minimum for standards

# Required fields for a non-demon record
REQUIRED_FIELDS = {
    'prop_id', 'game_id', 'player_id', 'stat_type',
    'side', 'pp_line', 'is_demon',
}


# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class NonDemonRecord:
    """Normalized record for one non-demon prop side."""

    # Identity
    prop_id:          str
    game_id:          str
    player_id:        str
    player_name:      str
    stat_type:        str
    side:             str        # 'over' | 'under'
    pp_line:          float
    floor_value:      float

    # Market signals
    book_consensus_line:  Optional[float] = None
    book_consensus_price: Optional[float] = None
    line_move:            int   = 0      # times PP moved this line
    first_seen_line:      Optional[float] = None
    shaded_side:          Optional[str]  = None   # 'over' | 'under' | None

    # Model signals
    proj_mean:        Optional[float] = None
    proj_hit_prob:    Optional[float] = None   # computed p_win
    recent_role:      Optional[float] = None   # 0-1 role certainty
    matchup_flag:     Optional[str]   = None   # 'favorable' | 'unfavorable' | None
    injury_flag:      bool = False
    script_flag:      Optional[str]   = None   # 'fits' | 'conflicts' | None
    is_more_only:     bool = False
    is_goblin:        bool = False
    is_demon:         bool = False

    # Pipeline outputs — filled by each step
    ineligible_reason:      Optional[str] = None
    eligible_nondemon:      bool          = True
    nondemon_score:         float         = 0.0
    strong_reasons_hit:     List[str]     = field(default_factory=list)
    stability_reasons_hit:  List[str]     = field(default_factory=list)
    reject_reasons:         List[str]     = field(default_factory=list)
    milp_candidate:         bool          = False
    selected:               bool          = False


# ─────────────────────────────────────────────────────────────────────────────
# Step 1 — ingest_props
# ─────────────────────────────────────────────────────────────────────────────

def ingest_props(raw_props: List[dict]) -> List[dict]:
    """
    Accept raw props from Express/Supabase.
    Drops nulls and logs count.
    """
    valid = [p for p in raw_props if isinstance(p, dict)]
    log.info("[ingest] %d props received, %d valid dicts", len(raw_props), len(valid))
    return valid


# ─────────────────────────────────────────────────────────────────────────────
# Step 2 — normalize_prop_record
# ─────────────────────────────────────────────────────────────────────────────

def normalize_prop_record(
    d: dict,
    sharp_map: Optional[Dict[str, dict]] = None,
) -> NonDemonRecord:
    """
    Convert raw prop dict → NonDemonRecord.
    Sets ineligible_reason = 'missing_data' if required fields are absent.
    """
    sharp_map = sharp_map or {}

    prop_id    = str(d.get('propId') or d.get('id') or d.get('sourcePropId') or '')
    game_id    = str(d.get('gameId') or d.get('game_id') or '')
    player_id  = str(d.get('playerId') or d.get('player_id') or '')
    player_name= str(d.get('playerName') or d.get('player_name') or '')
    stat_type  = str(d.get('statType') or d.get('stat_type') or '')
    side       = str(d.get('direction') or 'over').lower()
    is_demon   = bool(d.get('isDemon') or d.get('is_demon'))
    is_goblin  = bool(d.get('isGoblin') or d.get('is_goblin'))

    try:
        pp_line = float(d.get('lineScore') or d.get('line_score') or 0)
    except (TypeError, ValueError):
        pp_line = 0.0

    floor_value = LINE_FLOORS.get(stat_type, LINE_FLOORS['_default'])

    # Market / shade signals
    shade_signal     = d.get('ppShadeSignal') or d.get('pp_shade_signal') or 'no_data'
    shaded_side      = 'over'  if shade_signal == 'lean_over'  else \
                       'under' if shade_signal == 'lean_under' else None
    sharp_entry      = sharp_map.get(prop_id, {})
    book_line        = sharp_entry.get('fair_line') or d.get('sharpFairLine') or d.get('sharp_fair_line')
    book_price       = sharp_entry.get('over_juice') or d.get('sharpOverJuice')
    first_seen_line  = d.get('firstSeenLine') or d.get('first_seen_line')
    line_move        = int(d.get('lineMoveCount') or d.get('line_move_count') or 0)
    proj_hit_prob    = d.get('pWin') or d.get('p_win')

    rec = NonDemonRecord(
        prop_id=prop_id,
        game_id=game_id,
        player_id=player_id,
        player_name=player_name,
        stat_type=stat_type,
        side=side,
        pp_line=pp_line,
        floor_value=floor_value,
        book_consensus_line=float(book_line) if book_line is not None else None,
        book_consensus_price=float(book_price) if book_price is not None else None,
        line_move=line_move,
        first_seen_line=float(first_seen_line) if first_seen_line is not None else None,
        shaded_side=shaded_side,
        proj_hit_prob=float(proj_hit_prob) if proj_hit_prob is not None else None,
        recent_role=d.get('roleCertainty') or d.get('role_certainty'),
        matchup_flag=d.get('matchupFlag') or d.get('matchup_flag'),
        injury_flag=bool(d.get('injuryFlag') or d.get('injury_flag')),
        script_flag=d.get('scriptFlag') or d.get('script_flag'),
        is_more_only=bool(d.get('isMoreOnly') or d.get('is_more_only')),
        is_goblin=is_goblin,
        is_demon=is_demon,
    )

    # Validate required fields
    missing = []
    if not rec.prop_id:   missing.append('prop_id')
    if not rec.game_id:   missing.append('game_id')
    if not rec.stat_type: missing.append('stat_type')
    if rec.pp_line <= 0:  missing.append('pp_line')

    if missing:
        rec.ineligible_reason = 'missing_data'
        rec.eligible_nondemon = False
        rec.reject_reasons.append(f"missing_data:{','.join(missing)}")
        log.debug("[normalize] %s %s INELIGIBLE missing=%s", player_name, stat_type, missing)

    return rec


# ─────────────────────────────────────────────────────────────────────────────
# Step 3 — apply_hard_blocks
# ─────────────────────────────────────────────────────────────────────────────

def apply_hard_blocks(rec: NonDemonRecord) -> NonDemonRecord:
    """
    Hard business rules that immediately disqualify a prop.
    Any failure → eligible_nondemon = False.
    """
    if not rec.eligible_nondemon:
        return rec  # already blocked

    def _block(reason: str) -> NonDemonRecord:
        rec.eligible_nondemon = False
        rec.ineligible_reason = reason
        rec.reject_reasons.append(reason)
        log.debug("[hard_block] %s %s %.1f %s → %s",
                  rec.player_name, rec.stat_type, rec.pp_line, rec.side, reason)
        return rec

    # Demons never enter non-demon pool
    if rec.is_demon:
        return _block('is_demon')

    # Blocked stat types
    if rec.stat_type in BLOCKED_STATS:
        return _block('blocked_stat')

    # Under side blocked for certain stats
    if rec.side == 'under' and rec.stat_type in UNDER_BLOCKED_STATS:
        return _block('under_blocked_stat')

    # Below line floor
    if rec.pp_line < rec.floor_value and not rec.is_goblin:
        return _block(f'below_floor:{rec.floor_value}')

    # Injury uncertainty
    if rec.injury_flag:
        return _block('injury_flag')

    # Script conflict
    if rec.script_flag == 'conflicts':
        return _block('script_conflict')

    return rec


# ─────────────────────────────────────────────────────────────────────────────
# Step 4 — evaluate_edge_gate
# ─────────────────────────────────────────────────────────────────────────────

def evaluate_edge_gate(rec: NonDemonRecord) -> NonDemonRecord:
    """
    Require at least one strong edge signal.
    Populates rec.strong_reasons_hit.
    """
    if not rec.eligible_nondemon:
        return rec

    reasons: List[str] = []

    # 1. Sharp market gap
    if rec.book_consensus_line is not None:
        gap = rec.book_consensus_line - rec.pp_line
        # For over: positive gap means book is higher = PP line is low = edge over
        # For under: negative gap means book is lower = PP line is high = edge under
        if rec.side == 'over'  and gap >= SHARP_GAP_MIN:
            reasons.append('sharp_gap')
        elif rec.side == 'under' and gap <= -SHARP_GAP_MIN:
            reasons.append('sharp_gap')

    # 2. PP shade confirmed in our direction
    if rec.shaded_side == rec.side:
        reasons.append('shade_confirmed')

    # 3. Line moved in our direction
    if rec.line_move >= LINE_MOVED_MIN and rec.first_seen_line is not None:
        moved_up   = rec.pp_line > rec.first_seen_line
        move_fits  = (moved_up and rec.side == 'over') or \
                     (not moved_up and rec.side == 'under')
        if move_fits:
            reasons.append('line_moved')

    # 4. High win probability
    if rec.proj_hit_prob is not None and rec.proj_hit_prob >= HIGH_WIN_PCT_FLOOR:
        reasons.append('high_win_pct')

    # 5. Favorable matchup
    if rec.matchup_flag == 'favorable':
        reasons.append('favorable_matchup')

    rec.strong_reasons_hit = reasons

    if not reasons:
        rec.eligible_nondemon = False
        rec.reject_reasons.append('no_edge_signal')
        log.debug("[edge_gate] FAIL %s %s %.1f %s — no strong edge",
                  rec.player_name, rec.stat_type, rec.pp_line, rec.side)

    return rec


# ─────────────────────────────────────────────────────────────────────────────
# Step 5 — evaluate_stability_gate
# ─────────────────────────────────────────────────────────────────────────────

def evaluate_stability_gate(rec: NonDemonRecord) -> NonDemonRecord:
    """
    Require at least one stability signal.
    Populates rec.stability_reasons_hit.
    """
    if not rec.eligible_nondemon:
        return rec

    reasons: List[str] = []

    # 1. Secure role / high role certainty
    if rec.recent_role is not None and rec.recent_role >= STRONG_ROLE_FLOOR:
        reasons.append('strong_role')

    # 2. Script fits the stat type
    if rec.script_flag == 'fits':
        reasons.append('script_fit')

    # 3. Goblin props get stability pass — discount line implies PP is confident
    if rec.is_goblin:
        reasons.append('goblin_discount_line')

    # 4. More-only prop — PP flagged this as a guaranteed direction
    if rec.is_more_only:
        reasons.append('more_only_designation')

    # 5. Sharp gap alone is a strong stability proxy (sharp books have role data)
    if 'sharp_gap' in rec.strong_reasons_hit:
        reasons.append('sharp_implied_stability')

    # 6. Shade confirmed = PP market team has vetted this line
    if 'shade_confirmed' in rec.strong_reasons_hit:
        reasons.append('shade_implied_stability')

    rec.stability_reasons_hit = reasons

    if not reasons:
        rec.eligible_nondemon = False
        rec.reject_reasons.append('no_stability_signal')
        log.debug("[stability_gate] FAIL %s %s %.1f %s — no stability",
                  rec.player_name, rec.stat_type, rec.pp_line, rec.side)

    return rec


# ─────────────────────────────────────────────────────────────────────────────
# Step 6 — score_nondemon_prop
# ─────────────────────────────────────────────────────────────────────────────

def score_nondemon_prop(rec: NonDemonRecord) -> NonDemonRecord:
    """
    Deterministic additive score. Only runs if both gates passed.
    Starts from a neutral baseline and shifts based on signals.
    """
    if not rec.eligible_nondemon:
        rec.nondemon_score = 0.0
        return rec

    # ── Baseline ─────────────────────────────────────────────────────────────
    # More-only or PP-shaded props start with a non-neutral baseline
    if rec.is_more_only or rec.shaded_side == rec.side:
        score = 0.58
    elif rec.shaded_side is not None and rec.shaded_side != rec.side:
        score = 0.44  # shaded against us
    else:
        score = 0.50  # true neutral

    # ── Additive adjustments ─────────────────────────────────────────────────
    # Strong reasons add
    if 'sharp_gap' in rec.strong_reasons_hit:
        gap = abs((rec.book_consensus_line or rec.pp_line) - rec.pp_line)
        score += min(0.08, gap * 0.05)   # cap at +0.08

    if 'shade_confirmed' in rec.strong_reasons_hit:
        score += 0.04

    if 'line_moved' in rec.strong_reasons_hit:
        score += 0.03 * min(rec.line_move, 3)   # up to +0.09 for 3 moves

    if 'high_win_pct' in rec.strong_reasons_hit and rec.proj_hit_prob:
        score += (rec.proj_hit_prob - HIGH_WIN_PCT_FLOOR) * 0.50  # proportional

    if 'favorable_matchup' in rec.strong_reasons_hit:
        score += 0.03

    # Stability adds
    if 'strong_role' in rec.stability_reasons_hit and rec.recent_role:
        score += (rec.recent_role - STRONG_ROLE_FLOOR) * 0.20

    if 'script_fit' in rec.stability_reasons_hit:
        score += 0.02

    if 'goblin_discount_line' in rec.stability_reasons_hit:
        score += 0.03

    # ── Subtractions ─────────────────────────────────────────────────────────
    # Opposing shade (bad sign)
    if rec.shaded_side and rec.shaded_side != rec.side:
        score -= 0.05

    # Thin p_win
    if rec.proj_hit_prob is not None and rec.proj_hit_prob < STANDARD_PWIN_FLOOR:
        score -= (STANDARD_PWIN_FLOOR - rec.proj_hit_prob) * 0.80

    # ── Clamp ────────────────────────────────────────────────────────────────
    score = max(0.0, min(1.0, score))

    # ── Final eligibility check ───────────────────────────────────────────────
    if score < SCORE_FLOOR:
        rec.eligible_nondemon = False
        rec.reject_reasons.append(f'score_below_floor:{score:.4f}')

    rec.nondemon_score = round(score, 4)
    return rec


# ─────────────────────────────────────────────────────────────────────────────
# Step 7 — drop_if_score_zero_or_ineligible
# ─────────────────────────────────────────────────────────────────────────────

def drop_if_score_zero_or_ineligible(records: List[NonDemonRecord]) -> List[NonDemonRecord]:
    """
    Hard filter — only eligible records with score > 0 survive.
    All others are logged and dropped before MILP sees anything.
    """
    kept, dropped = [], []
    for rec in records:
        if rec.eligible_nondemon and rec.nondemon_score > 0.0:
            kept.append(rec)
        else:
            dropped.append(rec)
            log.debug("[drop] %s %s %.1f %s score=%.4f eligible=%s reasons=%s",
                      rec.player_name, rec.stat_type, rec.pp_line, rec.side,
                      rec.nondemon_score, rec.eligible_nondemon, rec.reject_reasons)

    log.info("[drop] kept=%d dropped=%d", len(kept), len(dropped))
    return kept


# ─────────────────────────────────────────────────────────────────────────────
# Step 8 — build_nondemon_candidate_pool
# ─────────────────────────────────────────────────────────────────────────────

def build_nondemon_candidate_pool(
    records: List[NonDemonRecord],
) -> Dict[str, List[NonDemonRecord]]:
    """
    Group surviving records by game_id.
    Marks each record as milp_candidate = True.
    Only games with ≥ n_legs candidates are included.
    """
    by_game: Dict[str, List[NonDemonRecord]] = {}
    for rec in records:
        rec.milp_candidate = True
        by_game.setdefault(rec.game_id, []).append(rec)

    log.info("[pool] %d games with candidates", len(by_game))
    for gid, recs in by_game.items():
        log.info("  game %s: %d candidates", gid, len(recs))
    return by_game


# ─────────────────────────────────────────────────────────────────────────────
# Step 9 — create_milp_variables_from_pool_only
# ─────────────────────────────────────────────────────────────────────────────

def create_milp_variables_from_pool_only(
    solver: "pywraplp.Solver",
    pool: List[NonDemonRecord],
) -> Dict[str, "pywraplp.Variable"]:
    """
    Create one binary variable per eligible record.
    No 0-score, no demon, no failed-gate record gets a variable.
    """
    x: Dict[str, "pywraplp.Variable"] = {}
    for rec in pool:
        if not rec.milp_candidate:
            continue  # guard — should never happen after step 8
        if rec.nondemon_score <= 0.0:
            log.warning("[milp_vars] BUG: 0-score record reached variable creation: %s", rec.prop_id)
            continue
        x[rec.prop_id] = solver.BoolVar(f"x_{rec.prop_id}")

    log.info("[milp_vars] created %d variables", len(x))
    return x


# ─────────────────────────────────────────────────────────────────────────────
# Step 10 — solve_milp_for_nondemons
# ─────────────────────────────────────────────────────────────────────────────

def solve_milp_for_nondemons(
    pool: List[NonDemonRecord],
    n_legs: int,
    time_limit_sec: float = 10.0,
) -> Optional[List[NonDemonRecord]]:
    """
    MILP over the pre-approved candidate pool.
    Constraints:
      - Exactly n_legs selected
      - Max 1 prop per player-stat-side combination
      - Maximize total nondemon_score
    Returns selected records or None if infeasible.
    """
    if not _ORTOOLS_OK:
        log.error("[milp] OR-Tools unavailable")
        return None

    if len(pool) < n_legs:
        log.info("[milp] pool size %d < n_legs %d — infeasible", len(pool), n_legs)
        return None

    solver = pywraplp.Solver.CreateSolver('SCIP')
    if not solver:
        log.error("[milp] SCIP solver unavailable")
        return None
    solver.SetTimeLimit(int(time_limit_sec * 1000))

    # Step 9 — variables
    x = create_milp_variables_from_pool_only(solver, pool)
    if len(x) < n_legs:
        log.info("[milp] only %d variables, need %d — infeasible", len(x), n_legs)
        return None

    # Objective — maximize total score
    solver.Maximize(
        solver.Sum([rec.nondemon_score * x[rec.prop_id]
                    for rec in pool if rec.prop_id in x])
    )

    # Constraint 1 — exact count
    solver.Add(solver.Sum(list(x.values())) == n_legs)

    # Constraint 2 — max 1 per player-stat-side
    player_stat_side: Dict[str, List] = {}
    for rec in pool:
        if rec.prop_id not in x:
            continue
        key = f"{rec.player_id}::{rec.stat_type}::{rec.side}"
        player_stat_side.setdefault(key, []).append(x[rec.prop_id])
    for vars_ in player_stat_side.values():
        solver.Add(solver.Sum(vars_) <= 1)

    # Constraint 3 — ≥ 2 distinct stat categories
    stat_vars: Dict[str, List] = {}
    for rec in pool:
        if rec.prop_id not in x:
            continue
        stat_vars.setdefault(rec.stat_type, []).append(x[rec.prop_id])
    z_stat: Dict[str, "pywraplp.Variable"] = {}
    for stat, vars_ in stat_vars.items():
        z = solver.BoolVar(f"z_stat_{stat}")
        solver.Add(solver.Sum(vars_) >= z)
        solver.Add(solver.Sum(vars_) <= n_legs * z)
        z_stat[stat] = z
    solver.Add(solver.Sum(list(z_stat.values())) >= 2)

    # Solve
    status = solver.Solve()
    if status not in (pywraplp.Solver.OPTIMAL, pywraplp.Solver.FEASIBLE):
        log.info("[milp] infeasible or no solution")
        return None

    selected = []
    for rec in pool:
        if rec.prop_id in x and x[rec.prop_id].solution_value() > 0.5:
            rec.selected = True
            selected.append(rec)

    if len(selected) != n_legs:
        log.warning("[milp] selected %d != n_legs %d", len(selected), n_legs)
        return None

    log.info("[milp] selected %d legs: %s",
             len(selected),
             [(r.player_name, r.stat_type, r.side, r.nondemon_score) for r in selected])
    return selected


# ─────────────────────────────────────────────────────────────────────────────
# Step 11 — emit_debug_table
# ─────────────────────────────────────────────────────────────────────────────

def emit_debug_table(records: List[NonDemonRecord]) -> None:
    """
    Log one debug row per prop with full pipeline trace.
    """
    log.info("[debug_table] %d records", len(records))
    log.info(
        "%-40s %-6s %-8s %-6s %-5s %-30s %-30s %-30s %-8s %-8s",
        "prop_key", "elig", "score", "milp", "sel",
        "strong_reasons", "stability_reasons", "reject_reasons",
        "p_win", "line"
    )
    for rec in records:
        key = f"{rec.player_name}:{rec.stat_type}:{rec.side}"
        log.info(
            "%-40s %-6s %-8.4f %-6s %-5s %-30s %-30s %-30s %-8s %-8.1f",
            key[:40],
            str(rec.eligible_nondemon),
            rec.nondemon_score,
            str(rec.milp_candidate),
            str(rec.selected),
            str(rec.strong_reasons_hit)[:30],
            str(rec.stability_reasons_hit)[:30],
            str(rec.reject_reasons)[:30],
            f"{rec.proj_hit_prob:.3f}" if rec.proj_hit_prob else "None",
            rec.pp_line,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point — run_nondemon_pipeline
# ─────────────────────────────────────────────────────────────────────────────

def run_nondemon_pipeline(
    raw_props: List[dict],
    n_legs: int,
    sharp_map: Optional[Dict[str, dict]] = None,
) -> Tuple[Optional[List[NonDemonRecord]], List[NonDemonRecord]]:
    """
    Full pipeline. Returns (selected_legs, all_records) for debug visibility.
    selected_legs is None if no feasible solution found.
    """
    sharp_map = sharp_map or {}

    # Step 1 — ingest
    valid_dicts = ingest_props(raw_props)

    # Step 2 — normalize
    records: List[NonDemonRecord] = [
        normalize_prop_record(d, sharp_map) for d in valid_dicts
    ]

    # Step 3 — hard blocks
    records = [apply_hard_blocks(r) for r in records]

    # Step 4 — edge gate
    records = [evaluate_edge_gate(r) for r in records]

    # Step 5 — stability gate
    records = [evaluate_stability_gate(r) for r in records]

    # Step 6 — score
    records = [score_nondemon_prop(r) for r in records]

    # Step 11 — debug (log all before dropping)
    if log.isEnabledFor(logging.DEBUG):
        emit_debug_table(records)

    # Step 7 — drop ineligible / 0-score
    eligible = drop_if_score_zero_or_ineligible(records)

    if not eligible:
        log.info("[pipeline] no eligible non-demon props — no slip")
        return None, records

    # Step 8 — pool by game
    by_game = build_nondemon_candidate_pool(eligible)

    # Step 10 — MILP per game, return best game's result
    best_selected: Optional[List[NonDemonRecord]] = None
    best_score = -1.0

    for game_id, pool in by_game.items():
        selected = solve_milp_for_nondemons(pool, n_legs)
        if selected:
            total = sum(r.nondemon_score for r in selected)
            if total > best_score:
                best_score   = total
                best_selected = selected
            log.info("[pipeline] game %s: %d legs selected, total_score=%.4f",
                     game_id, len(selected), total)

    return best_selected, records
