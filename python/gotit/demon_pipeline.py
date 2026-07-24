"""
demon_pipeline.py — GOTit Demon Selection Engine

Goal: Maximize joint hit probability of exactly 2 Demons in the same game.
      Must-hit mode: ignore payout boost, pure EV. Only care about P(both clear).

Core formula:
  For each Demon candidate i with raised line L_i:
    P_i = Phi((mu_i - L_i) / sigma_i)   # More-only (over), standard normal CDF

  For a pair (A, B):
    P_joint = P_A * P_B + c * sqrt(P_A*(1-P_A) * P_B*(1-P_B))
    where c = correlation adjustment (0.05–0.15), 0 if treated as independent

Selection thresholds:
  P_i floor (tau): 0.50
  P_joint floor:   0.25
  If no pair clears P_joint threshold → NO-GO (do not force two Demons)

Priority signals (in order):
  1. Line gap vs sharp — prefer small raises (+0.5–1.0) over large juice traps
  2. Model edge — (mu_i - L_i) / sigma_i, bigger z = higher P_i
  3. Fragility / floor — minutes security, usage stability, injury risk
  4. Game script alignment — both Demons benefit from same likely flow
  5. Recent underlying metrics — xStats, barrel rate, CSW%, regression candidates
  6. Role confirmation — confirmed starter, no load management
  7. Correlation — mild positive correlation preferred; avoid pure opposites

Hard rules:
  - Demons are OVER only, never under
  - Only appear in the Demon section, never in the Slate section
  - Exactly 2 per game; if no survivable pair → emit NO-GO, do not force
  - Separate pipeline from standard/goblin legs entirely
"""

import json
import logging
import math
import sys
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

TAU           = 0.50   # per-demon P_hit floor
JOINT_FLOOR   = 0.25   # P(both hit) floor — below this → NO-GO
RHO_DEFAULT   = 0.08   # default correlation when no game-script signal available
RHO_ALIGNED   = 0.13   # correlation when both Demons benefit from same script
RHO_OPPOSED   = 0.00   # correlation when Demons are script-opposed (treat independent)

# Fragility scoring weights
FRAGILITY_WEIGHTS = {
    'minutes_risk':    0.35,
    'injury_risk':     0.30,
    'role_unstable':   0.20,
    'weather_risk':    0.15,  # MLB only
}

# ─────────────────────────────────────────────────────────────────────────────
# Data structures
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DemonCandidate:
    prop_id:       str
    player_name:   str
    stat_type:     str
    line:          float
    game_id:       str
    team:          str = ''
    opponent:      str = ''

    # Model projection
    mu:            float = 0.0   # projected mean
    sigma:         float = 1.0   # residual volatility
    z_score:       float = 0.0   # (mu - line) / sigma
    p_hit:         float = 0.0   # Phi(z_score)

    # Signal scores (0–1)
    sharp_gap:     float = 0.0   # line vs sharp consensus (normalized)
    fragility:     float = 0.0   # 0=low fragility (good), 1=high fragility (bad)
    script_score:  float = 0.0   # game script alignment score

    # Meta
    tier_used:     str   = ''
    signals:       List[str] = field(default_factory=list)


@dataclass
class DemonPair:
    demon_a:    DemonCandidate
    demon_b:    DemonCandidate
    rho:        float = 0.0
    p_joint:    float = 0.0
    decision:   str   = 'ABORT'   # 'LOCK' or 'ABORT'


@dataclass
class DemonResult:
    game_id:    str
    status:     str              # 'CLEAR' or 'NO-GO'
    pair:       Optional[DemonPair] = None
    candidates: List[DemonCandidate] = field(default_factory=list)
    trace:      Dict[str, Any]   = field(default_factory=dict)
    error:      Optional[str]    = None


# ─────────────────────────────────────────────────────────────────────────────
# Math helpers
# ─────────────────────────────────────────────────────────────────────────────

def _phi(z: float) -> float:
    """Standard normal CDF."""
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _p_hit(mu: float, sigma: float, line: float) -> float:
    """P(stat > line) for a More-only (over) Demon."""
    if sigma <= 0:
        return 1.0 if mu > line else 0.0
    z = (mu - line) / sigma
    return _phi(z)


def _p_joint(p_a: float, p_b: float, rho: float) -> float:
    """
    Bivariate joint probability approximation.
    P_joint = P_A * P_B + rho * sqrt(P_A*(1-P_A) * P_B*(1-P_B))
    rho is the correlation adjustment (0.05–0.15 for mildly aligned game scripts).
    """
    corr_adj = rho * math.sqrt(p_a * (1.0 - p_a) * p_b * (1.0 - p_b))
    return p_a * p_b + corr_adj


def _estimate_sigma(line: float, stat_type: str) -> float:
    """
    Estimate residual volatility sigma from line and stat type.
    Used when no historical variance is available.
    Rough calibration: sigma ≈ 30–40% of line for counting stats.
    """
    base_ratios = {
        'Total Bases':          0.38,
        'Hits':                 0.55,
        'Hits+Runs+RBIs':       0.40,
        'Pitcher Strikeouts':   0.32,
        'Pitches Thrown':       0.14,
        'Pitching Outs':        0.28,
        'Hits Allowed':         0.42,
        'Earned Runs Allowed':  0.70,
        'Walks Allowed':        0.75,
        'Significant Strikes':  0.22,
        'Hitter Fantasy Score': 0.40,
        'Points':               0.26,
        'Rebounds':             0.40,
        'Assists':              0.45,
        'Points+Rebounds+Assists': 0.28,
        'Takedowns':            0.55,
        'Fight Time':           0.30,
        'Rushing Attempts':     0.35,
    }
    ratio = base_ratios.get(stat_type, 0.40)
    return max(0.5, line * ratio)


def _estimate_mu(line: float, sharp_gap: float, stat_type: str) -> float:
    """
    Estimate mu from line and sharp gap.
    sharp_gap > 0 means the sharp consensus is above the PP line (good for over).
    mu = line + sharp_gap (if available) else line * 1.05 (slight regression-to-mean edge).
    """
    if sharp_gap != 0.0:
        return line + sharp_gap
    # No sharp data: assume slight positive edge on PP demons (they're marketed as high-value)
    return line * 1.05


# ─────────────────────────────────────────────────────────────────────────────
# Candidate scoring
# ─────────────────────────────────────────────────────────────────────────────

def _score_candidate(raw: Dict[str, Any]) -> DemonCandidate:
    """
    Convert a raw prop dict into a scored DemonCandidate.
    raw keys (from orchestrator): playerName, statType, lineScore, id, gameId,
                                   teamAbbr, isDemon, sharpGap (optional),
                                   mu (optional), sigma (optional),
                                   fragility (optional), scriptScore (optional)
    """
    line       = float(raw.get('lineScore', 0) or 0)
    stat_type  = str(raw.get('statType', ''))
    sharp_gap  = float(raw.get('sharpGap', 0) or 0)

    mu    = float(raw.get('mu', 0) or 0) or _estimate_mu(line, sharp_gap, stat_type)
    sigma = float(raw.get('sigma', 0) or 0) or _estimate_sigma(line, stat_type)
    z     = (mu - line) / sigma if sigma > 0 else 0.0
    p     = _phi(z)

    fragility    = float(raw.get('fragility', 0.3) or 0.3)   # default: medium-low
    script_score = float(raw.get('scriptScore', 0.5) or 0.5)

    signals = []
    if sharp_gap > 0:
        signals.append(f'sharp gap +{sharp_gap:.2f}')
    if z > 0.3:
        signals.append(f'z={z:.2f} edge')
    if fragility < 0.3:
        signals.append('low fragility')
    if script_score > 0.6:
        signals.append('script aligned')

    return DemonCandidate(
        prop_id     = str(raw.get('id', '')),
        player_name = str(raw.get('playerName', '')),
        stat_type   = stat_type,
        line        = line,
        game_id     = str(raw.get('gameId', '')),
        team        = str(raw.get('teamAbbr', '')),
        mu          = mu,
        sigma       = sigma,
        z_score     = z,
        p_hit       = p,
        sharp_gap   = sharp_gap,
        fragility   = fragility,
        script_score= script_score,
        signals     = signals,
    )


def _rho_for_pair(a: DemonCandidate, b: DemonCandidate) -> float:
    """
    Estimate correlation between two Demons in the same game.
    - Same team, same game-script direction → RHO_ALIGNED
    - Opposing pitchers / script-opposed → RHO_OPPOSED
    - Default → RHO_DEFAULT
    """
    same_team = (a.team and b.team and a.team == b.team)
    both_script_aligned = (a.script_score > 0.6 and b.script_score > 0.6)

    if same_team and both_script_aligned:
        return RHO_ALIGNED
    # Pitching vs hitting on same team can be opposed
    pitcher_stats = {'Pitcher Strikeouts', 'Pitches Thrown', 'Pitching Outs',
                     'Hits Allowed', 'Earned Runs Allowed', 'Walks Allowed', 'Significant Strikes'}
    a_pitcher = a.stat_type in pitcher_stats
    b_pitcher = b.stat_type in pitcher_stats
    if a_pitcher != b_pitcher and same_team:
        return RHO_OPPOSED
    return RHO_DEFAULT


# ─────────────────────────────────────────────────────────────────────────────
# Core selection
# ─────────────────────────────────────────────────────────────────────────────

def _select_demon_pair(
    candidates: List[DemonCandidate],
    tau: float = TAU,
    joint_floor: float = JOINT_FLOOR,
) -> Tuple[Optional[DemonPair], List[str]]:
    """
    From a list of scored candidates for one game:
    1. Filter to P_hit >= tau
    2. Enumerate all unique pairs
    3. Compute P_joint for each pair with correlation adjustment
    4. Return the pair with highest P_joint if >= joint_floor, else None
    """
    trace_msgs = []

    # Step 1: filter by tau
    eligible = [c for c in candidates if c.p_hit >= tau]
    trace_msgs.append(f'candidates={len(candidates)} eligible(p>={tau})={len(eligible)}')

    if len(eligible) < 2:
        # Relax tau by 0.03 once
        relaxed_tau = tau - 0.03
        eligible = [c for c in candidates if c.p_hit >= relaxed_tau]
        trace_msgs.append(f'relaxed tau to {relaxed_tau:.2f} → eligible={len(eligible)}')
        if len(eligible) < 2:
            trace_msgs.append('NO-GO: fewer than 2 candidates survive tau (even relaxed)')
            return None, trace_msgs

    # Step 2: enumerate pairs, compute P_joint
    best_pair: Optional[DemonPair] = None
    best_pj   = -1.0

    for a, b in combinations(eligible, 2):
        rho = _rho_for_pair(a, b)
        pj  = _p_joint(a.p_hit, b.p_hit, rho)
        if pj > best_pj:
            best_pj = pj
            best_pair = DemonPair(demon_a=a, demon_b=b, rho=rho, p_joint=pj)

    if best_pair is None or best_pj < joint_floor:
        trace_msgs.append(f'NO-GO: best P_joint={best_pj:.4f} < floor={joint_floor}')
        return None, trace_msgs

    best_pair.decision = 'LOCK'
    trace_msgs.append(
        f'LOCK: {best_pair.demon_a.player_name} + {best_pair.demon_b.player_name} '
        f'P_joint={best_pj:.4f} rho={best_pair.rho:.2f}'
    )
    return best_pair, trace_msgs


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def run_demon_pipeline(props: List[Dict[str, Any]], game_id: str) -> DemonResult:
    """
    Run the demon selection pipeline for a single game.

    Args:
        props:   List of raw prop dicts for this game (isDemon=True only)
        game_id: Game identifier

    Returns:
        DemonResult with status='CLEAR' and pair, or status='NO-GO'
    """
    trace: Dict[str, Any] = {'game_id': game_id, 'steps': [], 'warnings': []}

    # Filter to demons only, over direction only
    demon_props = [p for p in props if p.get('isDemon') and
                   str(p.get('direction', 'over')).lower() != 'under']
    trace['steps'].append(f'demon_props={len(demon_props)}')

    if not demon_props:
        return DemonResult(
            game_id  = game_id,
            status   = 'NO-GO',
            trace    = trace,
            error    = 'no demon props for game',
        )

    # Score all candidates
    candidates = [_score_candidate(p) for p in demon_props]
    trace['steps'].append(f'scored={len(candidates)}')

    # Select best pair
    pair, pair_trace = _select_demon_pair(candidates)
    trace['steps'].extend(pair_trace)

    if pair is None:
        return DemonResult(
            game_id    = game_id,
            status     = 'NO-GO',
            candidates = candidates,
            trace      = trace,
        )

    return DemonResult(
        game_id    = game_id,
        status     = 'CLEAR',
        pair       = pair,
        candidates = candidates,
        trace      = trace,
    )


def format_output(result: DemonResult) -> Dict[str, Any]:
    """
    Format DemonResult into the structured output GOTit expects.
    Returns selected_demons list (0 or 2 items) + trace.
    """
    selected_demons = []

    if result.status == 'CLEAR' and result.pair:
        p = result.pair
        for demon in [p.demon_a, p.demon_b]:
            selected_demons.append({
                'prop_id':     demon.prop_id,
                'playerName':  demon.player_name,
                'statType':    demon.stat_type,
                'lineScore':   demon.line,
                'direction':   'over',
                'isDemon':     True,
                'p_hit':       round(demon.p_hit, 4),
                'mu':          round(demon.mu, 2),
                'sigma':       round(demon.sigma, 2),
                'z_score':     round(demon.z_score, 3),
                'sharp_gap':   round(demon.sharp_gap, 2),
                'fragility':   round(demon.fragility, 2),
                'signals':     demon.signals,
            })

    return {
        'selected_demons':        selected_demons,
        'post_relaxation_demons': selected_demons,  # same — no separate relaxation pool
        'status':                 result.status,
        'p_joint':                round(result.pair.p_joint, 4) if result.pair else 0.0,
        'rho':                    round(result.pair.rho, 3) if result.pair else 0.0,
        'decision':               result.pair.decision if result.pair else 'ABORT',
        'trace':                  result.trace,
        'error':                  result.error,
    }


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point (called from routes.ts via python subprocess)
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    try:
        payload = json.loads(sys.stdin.read())
        props   = payload.get('props', [])
        game_id = payload.get('game_id', 'unknown')

        result = run_demon_pipeline(props, game_id)
        output = format_output(result)
        print(json.dumps(output))
    except Exception as exc:
        log.exception('demon_pipeline fatal error')
        print(json.dumps({'error': str(exc), 'selected_demons': [], 'post_relaxation_demons': []}))
        sys.exit(1)
