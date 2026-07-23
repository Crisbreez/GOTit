#!/usr/bin/env python3
"""
GOTit Demon Pipeline — Guilty Until Proven Innocent
====================================================

Every Demon is treated as a house-favored trap first.
It must survive ALL 5 gates before it can be selected.
If any one gate fails, the Demon is rejected and the reason is logged.

Gate order (exact):
  1. market_pass    — PP Demon line ≤ sharp consensus line (or close)
  2. probability_pass — estimated hit prob >= 0.62
  3. role_pass      — player role is locked and stable
  4. matchup_pass   — opponent + game environment supports the stat
  5. normal_path_pass — can hit through ordinary volume, not outlier script

A Demon that clears all 5 becomes eligible_demon = True and enters MILP.
MILP only sees eligible Demons.

Scoring (after gates pass):
  1. Compare PP line to sharp books (market agreement signal)
  2. Estimate real hit probability
  3. Confirm role stability
  4. Confirm matchup + script fit
  5. Confirm normal-volume path
  Final score = weighted composite of all 5 signals
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

# Stats that are structurally fragile, single-event, or high-variance
# These can never be trusted as Demon picks regardless of line
DEMON_EXCLUDED_STATS: set = {
    'Plate Appearances',          # near-certain for any starter
    'Pitcher Strikeouts (Combo)', # multi-player, poorly defined
    '1st Inning Walks Allowed',   # tiny sample, extreme variance
    'Triples',                    # rarest hit, pure lottery
    'RBIs',                       # teammate-dependent, not player skill
    'Singles',                    # low line, near-coinflip on bad days
    'Hits+Runs+RBIs',             # composite — too many failure paths
    'Hits',                       # 1.5 line is too volatile
    'Hitter Strikeouts',          # pitcher-dependent, not player skill
    'Doubles',                    # single-event, insufficient edge
    'Walks',                      # pitcher-dependent, extreme variance
    'Home Runs',                  # 0.5 demon lines are coinflips
    'Stolen Bases',               # situational, manager/matchup dependent
    'Runs',                       # teammate-dependent coinflip
}

# Minimum line floors — lines below this are structurally too easy or too volatile
DEMON_LINE_FLOOR: Dict[str, float] = {
    'Total Bases':          2.5,
    'Hitter Fantasy Score': 25.0,  # only Ohtani/Judge tier qualifies
    'Pitcher Strikeouts':   3.5,
    'Pitching Outs':        9.5,
    'Pitches Thrown':       70.0,
    'Pitcher Fantasy Score':25.0,
    'Earned Runs Allowed':  0.5,
    'Hits Allowed':         2.5,
    'Significant Strikes':  25.0,
    'Takedowns':            1.5,
    'Fight Time':           8.0,
    '_default':             1.5,
}

# CV table for log-normal p_win estimation
STAT_CV: Dict[str, float] = {
    'Pitcher Strikeouts':   0.35,
    'Pitches Thrown':       0.18,
    'Pitcher Fantasy Score':0.55,
    'Total Bases':          0.85,
    'Hitter Fantasy Score': 0.75,
    'Earned Runs Allowed':  0.90,
    'Hits Allowed':         0.65,
    'Significant Strikes':  1.20,
    'Takedowns':            1.10,
    'Fight Time':           0.50,
    '_default':             0.70,
}

# How far below true mean PP sets the demon line (demon_ratio > 1 = easier over)
DEMON_RATIO: Dict[str, float] = {
    'Pitcher Strikeouts':   1.35,
    'Pitches Thrown':       1.15,
    'Pitcher Fantasy Score':1.30,
    'Earned Runs Allowed':  1.80,
    'Hitter Fantasy Score': 1.45,
    'Total Bases':          1.45,
    'Hits Allowed':         1.40,
    'Significant Strikes':  1.40,
    'Takedowns':            1.50,
    'Fight Time':           1.20,
    '_default':             1.40,
}

# Gate thresholds
PROB_FLOOR           = 0.62   # Gate 2: strict minimum hit probability
PROB_CEILING         = 0.92   # Reject near-certain overs (house trap)
MARKET_TOLERANCE     = 0.50   # Gate 1: PP line can be at most 0.5 below sharp line
ROLE_FLOOR           = 0.65   # Gate 3: role certainty minimum for Demons
SCORE_FLOOR          = 0.01   # Final: below this the Demon is dropped


# ─────────────────────────────────────────────────────────────────────────────
# Data structure
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DemonRecord:
    """Normalized record for one Demon candidate."""

    # Identity
    prop_id:      str
    game_id:      str
    player_id:    str
    player_name:  str
    stat_type:    str
    pp_line:      float

    # Market signals
    sharp_line:       Optional[float] = None   # sharp consensus fair line
    shade_signal:     str             = 'no_data'  # lean_over | lean_under | neutral
    line_move:        int             = 0
    first_seen_line:  Optional[float] = None

    # Model signals
    proj_hit_prob:    Optional[float] = None
    recent_role:      Optional[float] = None
    matchup_flag:     Optional[str]   = None
    injury_flag:      bool            = False
    script_flag:      Optional[str]   = None

    # Raw prop dict (for output)
    raw:          dict = field(default_factory=dict)

    # Pipeline outputs
    eligible_demon:     bool          = False
    demon_score:        float         = 0.0
    gates_passed:       List[str]     = field(default_factory=list)
    gates_failed:       List[str]     = field(default_factory=list)
    reject_reason:      Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _estimate_p_win(line: float, stat_type: str) -> float:
    """Log-normal p(over demon line) using ratio-based true mean."""
    if line <= 0:
        return 0.0
    cv    = STAT_CV.get(stat_type, STAT_CV['_default'])
    ratio = DEMON_RATIO.get(stat_type, DEMON_RATIO['_default'])
    true_mean = line * ratio
    sigma     = cv * true_mean
    if sigma <= 0:
        return 0.999
    mu_ln    = math.log(true_mean) - 0.5 * math.log(1 + (sigma / true_mean) ** 2)
    sigma_ln = math.sqrt(math.log(1 + (sigma / true_mean) ** 2))
    z        = (math.log(max(line, 0.001)) - mu_ln) / sigma_ln
    return float(max(0.0, min(0.999, 0.5 * math.erfc(z / math.sqrt(2)))))


def _normalize(d: dict, sharp_map: Optional[Dict[str, dict]] = None) -> Optional[DemonRecord]:
    """Convert raw prop dict → DemonRecord. Returns None if not a demon."""
    if not d.get('isDemon'):
        return None

    import hashlib
    prop_id    = str(d.get('propId') or d.get('id') or d.get('sourcePropId') or
                     hashlib.md5(f"{d.get('playerName','')}{d.get('statType','')}{d.get('lineScore','')}".encode()).hexdigest()[:12])
    game_id    = str(d.get('gameId') or d.get('game_id') or '')
    player_id  = str(d.get('playerId') or d.get('player_id') or
                     hashlib.md5((d.get('playerName') or '').lower().encode()).hexdigest()[:16])
    player_name = str(d.get('playerName') or '')
    stat_type   = str(d.get('statType') or '')
    try:
        pp_line = float(d.get('lineScore') or 0)
    except (TypeError, ValueError):
        pp_line = 0.0

    sharp_entry = (sharp_map or {}).get(prop_id, {})
    sharp_line  = sharp_entry.get('fair_line') or d.get('sharpFairLine') or d.get('sharp_fair_line')
    shade_raw   = d.get('ppShadeSignal') or d.get('pp_shade_signal') or 'no_data'

    return DemonRecord(
        prop_id=prop_id,
        game_id=game_id,
        player_id=player_id,
        player_name=player_name,
        stat_type=stat_type,
        pp_line=pp_line,
        sharp_line=float(sharp_line) if sharp_line is not None else None,
        shade_signal=shade_raw,
        line_move=int(d.get('lineMoveCount') or d.get('line_move_count') or 0),
        first_seen_line=float(d.get('firstSeenLine') or d.get('first_seen_line')) if (d.get('firstSeenLine') or d.get('first_seen_line')) else None,
        proj_hit_prob=float(d.get('pWin') or d.get('p_win')) if (d.get('pWin') or d.get('p_win')) else None,
        recent_role=float(d.get('roleCertainty') or d.get('role_certainty')) if (d.get('roleCertainty') or d.get('role_certainty')) else None,
        matchup_flag=d.get('matchupFlag') or d.get('matchup_flag'),
        injury_flag=bool(d.get('injuryFlag') or d.get('injury_flag')),
        script_flag=d.get('scriptFlag') or d.get('script_flag'),
        raw=d,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 5 Survival Gates
# ─────────────────────────────────────────────────────────────────────────────

def _gate_1_market(rec: DemonRecord) -> bool:
    """
    Gate 1: Market Pass
    PP Demon line must be ≤ sharp consensus line + MARKET_TOLERANCE.
    If sharp books set the line LOWER than PP, the market disagrees → reject.
    If no sharp data, pass only if PP shade is lean_over (PP itself agrees).
    """
    if rec.sharp_line is not None:
        # PP line should not be more than MARKET_TOLERANCE above sharp line
        # (PP demon lines are typically BELOW sharp = easier over = good)
        # Reject if PP line is well above sharp (market says it's too hard)
        if rec.pp_line > rec.sharp_line + MARKET_TOLERANCE:
            log.info("[demon_gate_1_FAIL] %s %s %.1f pp_line > sharp_line+tol (%.1f+%.1f)",
                     rec.player_name, rec.stat_type, rec.pp_line, rec.sharp_line, MARKET_TOLERANCE)
            return False
        log.info("[demon_gate_1_PASS] %s %s pp=%.1f sharp=%.1f gap=%.2f",
                 rec.player_name, rec.stat_type, rec.pp_line, rec.sharp_line,
                 rec.sharp_line - rec.pp_line)
        return True

    # No sharp data — use PP shade signal as proxy
    if rec.shade_signal == 'lean_over':
        log.info("[demon_gate_1_PASS] %s %s no sharp data, PP shaded over",
                 rec.player_name, rec.stat_type)
        return True

    if rec.shade_signal == 'neutral':
        # No market disagreement, allow with warning
        log.info("[demon_gate_1_PASS_WARN] %s %s no sharp data, PP neutral — marginal pass",
                 rec.player_name, rec.stat_type)
        return True

    # lean_under or no_data with no confirmation → reject
    log.info("[demon_gate_1_FAIL] %s %s no sharp data and shade=%s",
             rec.player_name, rec.stat_type, rec.shade_signal)
    return False


def _gate_2_probability(rec: DemonRecord) -> bool:
    """
    Gate 2: Probability Pass
    Estimated hit probability must be >= PROB_FLOOR (0.62).
    Near-certain overs (>= PROB_CEILING) are rejected — house trap.
    """
    p = _estimate_p_win(rec.pp_line, rec.stat_type)
    rec.proj_hit_prob = round(p, 4)  # store computed value

    if p < PROB_FLOOR:
        log.info("[demon_gate_2_FAIL] %s %s %.1f p_win=%.3f < floor=%.2f",
                 rec.player_name, rec.stat_type, rec.pp_line, p, PROB_FLOOR)
        return False

    if p >= PROB_CEILING:
        log.info("[demon_gate_2_FAIL] %s %s %.1f p_win=%.3f — near-certain, house trap",
                 rec.player_name, rec.stat_type, rec.pp_line, p)
        return False

    log.info("[demon_gate_2_PASS] %s %s p_win=%.3f", rec.player_name, rec.stat_type, p)
    return True


def _gate_3_role(rec: DemonRecord) -> bool:
    """
    Gate 3: Role Pass
    Player role must be locked. Injury noise or bench risk → reject.
    Without role data, use stat type as proxy — pitching stats imply locked role.
    """
    if rec.injury_flag:
        log.info("[demon_gate_3_FAIL] %s — injury flag set", rec.player_name)
        return False

    if rec.recent_role is not None:
        if rec.recent_role >= ROLE_FLOOR:
            log.info("[demon_gate_3_PASS] %s role=%.2f", rec.player_name, rec.recent_role)
            return True
        else:
            log.info("[demon_gate_3_FAIL] %s role=%.2f < floor=%.2f",
                     rec.player_name, rec.recent_role, ROLE_FLOOR)
            return False

    # No role data — use stat type as proxy
    # Pitching stats (starter) imply locked role by definition
    LOCKED_ROLE_STATS = {
        'Pitcher Strikeouts', 'Pitches Thrown', 'Pitching Outs',
        'Hits Allowed', 'Earned Runs Allowed', 'Pitcher Fantasy Score',
        'Significant Strikes', 'Fight Time', 'Takedowns',
    }
    if rec.stat_type in LOCKED_ROLE_STATS:
        log.info("[demon_gate_3_PASS] %s %s — locked-role stat type, no role data needed",
                 rec.player_name, rec.stat_type)
        return True

    # No data + not a locked-role stat = reject (can't confirm stable role)
    log.info("[demon_gate_3_FAIL] %s %s — no role data and stat is not locked-role type",
             rec.player_name, rec.stat_type)
    return False


def _gate_4_matchup(rec: DemonRecord) -> bool:
    """
    Gate 4: Matchup Pass
    Opponent and game environment must support the stat.
    If no matchup data, pass — we don't reject on missing data alone here.
    """
    if rec.matchup_flag == 'unfavorable':
        log.info("[demon_gate_4_FAIL] %s %s — matchup unfavorable",
                 rec.player_name, rec.stat_type)
        return False

    log.info("[demon_gate_4_PASS] %s %s matchup=%s",
             rec.player_name, rec.stat_type, rec.matchup_flag or 'no_data')
    return True


def _gate_5_normal_path(rec: DemonRecord) -> bool:
    """
    Gate 5: Normal Path Pass
    Demon must be reachable through ordinary volume — not a ceiling or outlier event.
    Reject if:
      - Script requires blowout, overtime, or unsustainably hot shooting
      - Stat type is fragile/single-event (in DEMON_EXCLUDED_STATS)
      - Script conflicts with the stat path
    """
    # Excluded stats = structurally single-event or fragile
    if rec.stat_type in DEMON_EXCLUDED_STATS:
        log.info("[demon_gate_5_FAIL] %s %s — excluded stat, no normal hit path",
                 rec.player_name, rec.stat_type)
        return False

    # Script actively conflicts
    if rec.script_flag == 'conflicts':
        log.info("[demon_gate_5_FAIL] %s %s — script conflicts with stat path",
                 rec.player_name, rec.stat_type)
        return False

    # Line below floor = requires outlier event to hit
    floor = DEMON_LINE_FLOOR.get(rec.stat_type, DEMON_LINE_FLOOR['_default'])
    if rec.pp_line < floor:
        log.info("[demon_gate_5_FAIL] %s %s %.1f < floor %.1f — below normal volume path",
                 rec.player_name, rec.stat_type, rec.pp_line, floor)
        return False

    log.info("[demon_gate_5_PASS] %s %s %.1f — normal volume path confirmed",
             rec.player_name, rec.stat_type, rec.pp_line)
    return True


# ────────────────────────────────────────────────���────────────────────────────
# Scoring (only after all gates pass)
# ─────────────────────────────────────────────────────────────────────────────

def _score_demon(rec: DemonRecord) -> float:
    """
    Deterministic composite score. Called only after all 5 gates pass.
    Order mirrors the gate order (market → prob → role → matchup → path).
    """
    score = 0.0

    # 1. Market agreement
    if rec.sharp_line is not None:
        gap = rec.sharp_line - rec.pp_line  # positive = PP line is below sharp = easier over
        score += min(0.20, max(0.0, gap * 0.10))
    elif rec.shade_signal == 'lean_over':
        score += 0.10  # PP itself agrees

    # 2. Probability — centered on PROB_FLOOR
    p = rec.proj_hit_prob or _estimate_p_win(rec.pp_line, rec.stat_type)
    score += min(0.25, (p - PROB_FLOOR) * 1.50)

    # 3. Role certainty
    if rec.recent_role is not None:
        score += min(0.15, (rec.recent_role - ROLE_FLOOR) * 0.50)
    else:
        score += 0.05  # implicit locked-role stat bonus

    # 4. Matchup
    if rec.matchup_flag == 'favorable':
        score += 0.10
    elif rec.matchup_flag is None:
        score += 0.05  # neutral

    # 5. Normal path / script
    if rec.script_flag == 'fits':
        score += 0.10
    else:
        score += 0.05  # no explicit conflict

    # Line difficulty bonus — higher line relative to floor = harder = more value
    floor = DEMON_LINE_FLOOR.get(rec.stat_type, DEMON_LINE_FLOOR['_default'])
    difficulty = min(rec.pp_line / max(floor, 1.0), 3.0)
    score += min(0.15, (difficulty - 1.0) * 0.10)

    # Line movement confirmation
    if rec.line_move >= 1 and rec.first_seen_line is not None:
        if rec.pp_line > rec.first_seen_line:  # moved up = harder, but PP still likes it
            score += 0.03 * min(rec.line_move, 3)

    return round(min(1.0, max(0.0, score)), 4)


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def run_demon_pipeline(
    raw_props: List[dict],
    sharp_map: Optional[Dict[str, dict]] = None,
    max_demons: int = 1,
) -> List[dict]:
    """
    Full demon pipeline. Returns list of eligible demon dicts (max max_demons),
    each enriched with demonScore metadata.

    Guarantees:
      - Only demons (isDemon=True) are processed
      - Every rejected demon has a logged reason
      - Only eligible_demon=True props are returned
      - MILP-ready: caller just hands this list to MILP as the demon pool
    """
    sharp_map = sharp_map or {}

    # Normalize all demon props
    candidates: List[DemonRecord] = []
    for d in raw_props:
        rec = _normalize(d, sharp_map)
        if rec is None:
            continue  # not a demon
        candidates.append(rec)

    log.info("[demon_pipeline] %d demon candidates", len(candidates))

    # Run all 5 gates
    survivors: List[DemonRecord] = []
    for rec in candidates:
        gates = [
            ('market_pass',      _gate_1_market),
            ('probability_pass', _gate_2_probability),
            ('role_pass',        _gate_3_role),
            ('matchup_pass',     _gate_4_matchup),
            ('normal_path_pass', _gate_5_normal_path),
        ]
        passed = True
        for gate_name, gate_fn in gates:
            if gate_fn(rec):
                rec.gates_passed.append(gate_name)
            else:
                rec.gates_failed.append(gate_name)
                rec.reject_reason = gate_name
                passed = False
                log.info("[demon_pipeline] REJECT %s %s %.1f — failed %s",
                         rec.player_name, rec.stat_type, rec.pp_line, gate_name)
                break  # stop at first failure

        if not passed:
            continue

        # All gates passed — score
        rec.demon_score   = _score_demon(rec)
        rec.eligible_demon = rec.demon_score >= SCORE_FLOOR

        if rec.eligible_demon:
            log.info("[demon_pipeline] ACCEPT %s %s %.1f score=%.4f gates=%s",
                     rec.player_name, rec.stat_type, rec.pp_line,
                     rec.demon_score, rec.gates_passed)
            survivors.append(rec)
        else:
            log.info("[demon_pipeline] REJECT %s %s — score %.4f below floor",
                     rec.player_name, rec.stat_type, rec.demon_score)

    # Sort by score descending, pick top max_demons distinct players
    survivors.sort(key=lambda r: r.demon_score, reverse=True)
    seen_players: set = set()
    top: List[DemonRecord] = []
    for rec in survivors:
        if rec.player_name in seen_players:
            continue
        seen_players.add(rec.player_name)
        top.append(rec)
        if len(top) >= max_demons:
            break

    log.info("[demon_pipeline] %d/%d demons survived all gates",
             len(top), len(candidates))

    # Build output dicts
    result = []
    for rec in top:
        out = {
            **rec.raw,
            'isDemon': True,
            'demonScore': {
                'composite':   rec.demon_score,
                'p_win':       rec.proj_hit_prob,
                'line':        rec.pp_line,
                'stat':        rec.stat_type,
                'gates_passed': rec.gates_passed,
                'gates_failed': rec.gates_failed,
            },
        }
        result.append(out)

    return result
