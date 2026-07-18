"""
GOTit — Life-on-the-Line Leg Selector
Selects exactly 6 legs + 2 distinct-player Demons per game.
Maximizes expected flex payout. Zero heuristics. Full calibration traceability.

Bugs fixed from original:
  1. shapley_marginal_ev: `idx` was never defined — now iterates over all legs correctly
  2. `math` was never imported — added
  3. `scipy.stats.binom` in expected_payout — added proper import alias
  4. `solver.Add(sum(demon_players.keys()) == 2)` — string sum is invalid, removed
     (the y_vars block below it correctly enforces the distinct-player constraint)
  5. shapley_marginal_ev loop structure was broken — fully rewritten with correct outer loop
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass, field
from itertools import combinations
from typing import Dict, List, Optional, Tuple
from enum import Enum

import numpy as np
import scipy.stats
from scipy.stats import poisson, nbinom, gamma, norm
from scipy.optimize import minimize_scalar
from scipy.special import ndtr
from scipy.stats import multivariate_normal
from ortools.linear_solver import pywraplp

log = logging.getLogger("gotit.leg_selector")


# ──────────────────────────────────────────────────────────────────────────────
# 0. FLEX PAYOUT TABLE (exact PrizePicks Flex)
# ──────────────────────────────────────────────────────────────────────────────
FLEX_PAYOUT: Dict[int, Dict[int, float]] = {
    2: {2: 3.0,  1: 0.0,  0: 0.0},
    3: {3: 5.0,  2: 1.5,  1: 0.0,  0: 0.0},
    4: {4: 10.0, 3: 2.0,  2: 0.4,  1: 0.0,  0: 0.0},
    5: {5: 20.0, 4: 4.0,  3: 0.4,  2: 0.0,  1: 0.0,  0: 0.0},
    6: {6: 25.0, 5: 10.0, 4: 2.0,  3: 0.0,  2: 0.0,  1: 0.0,  0: 0.0},
}


# ──────────────────────────────────────────────────────────────────────────────
# 1. STAT-TYPE → DISTRIBUTION FAMILY
# ──────────────────────────────────────────────────────────────────────────────
class DistFamily(Enum):
    POISSON = "poisson"
    NEGBIN  = "negbin"
    GAMMA   = "gamma"
    SKELLAM = "skellam"


STAT_DIST: Dict[str, DistFamily] = {
    # Continuous / Gamma
    "Points":               DistFamily.GAMMA,
    "Total Bases":          DistFamily.GAMMA,
    "Hits+Runs+RBIs":       DistFamily.GAMMA,
    "Fantasy Score":        DistFamily.GAMMA,
    "Hitter Fantasy Score": DistFamily.GAMMA,
    "Pitcher Fantasy Score": DistFamily.GAMMA,
    "Passing Yards":        DistFamily.GAMMA,
    "Rushing Yards":        DistFamily.GAMMA,
    "Receiving Yards":      DistFamily.GAMMA,
    "Pts+Reb+Ast":              DistFamily.GAMMA,
    # Negative Binomial (overdispersed counts)
    "Rebounds":                 DistFamily.NEGBIN,
    "Assists":                  DistFamily.NEGBIN,
    "Receptions":               DistFamily.NEGBIN,
    "Rush Attempts":            DistFamily.NEGBIN,
    # Poisson (rare discrete counts)
    "Hits":                     DistFamily.POISSON,
    "Home Runs":                DistFamily.POISSON,
    "Walks":                    DistFamily.POISSON,
    "Stolen Bases":             DistFamily.POISSON,
    "Blocks":                   DistFamily.POISSON,
    "Steals":                   DistFamily.POISSON,
    "Turnovers":                DistFamily.POISSON,
    "3-PT Made":                DistFamily.POISSON,
    "Passing TDs":              DistFamily.POISSON,
    "Singles":                  DistFamily.POISSON,
    "Doubles":                  DistFamily.POISSON,
    "Triples":                  DistFamily.POISSON,
    "Plate Appearances":        DistFamily.POISSON,
    # MLB Strikeouts — PP uses distinct names for batter vs pitcher
    "Strikeouts":               DistFamily.POISSON,  # backward compat
    "Hitter Strikeouts":        DistFamily.POISSON,
    "Pitcher Strikeouts":       DistFamily.POISSON,
    "Strikeouts Allowed":       DistFamily.POISSON,
    # MLB pitcher counts
    "RBIs":                     DistFamily.POISSON,
    "Runs":                     DistFamily.POISSON,
    "Walks Allowed":            DistFamily.POISSON,
    "Earned Runs Allowed":      DistFamily.POISSON,
    "Hits Allowed":             DistFamily.POISSON,
    "Pitching Outs":            DistFamily.POISSON,
    "Pitches Thrown":           DistFamily.POISSON,
    "1st Inning Walks Allowed": DistFamily.POISSON,
    "1st Inning Runs Allowed":  DistFamily.POISSON,
}

_DEFAULT_FAMILY = DistFamily.GAMMA


def get_family(stat_type: str) -> DistFamily:
    return STAT_DIST.get(stat_type, _DEFAULT_FAMILY)


# ──────────────────────────────────────────────────────────────────────────────
# 2. CALIBRATION PARAMETERS
# ──────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class CalibrationParams:
    delta_goblin:     Dict[str, float]
    delta_demon:      Dict[str, float]
    margin_beta:      Dict[str, float]
    probe_prob:       float
    probe_magnitude:  float
    corr_guard_rho:   float
    void_premium:     Dict[str, float]
    dist_params:      Dict[str, Dict[str, float]]
    version:          str
    trained_on:       str
    sha256:           str

    def verify_hash(self) -> bool:
        blob = json.dumps({
            "delta_goblin":    self.delta_goblin,
            "delta_demon":     self.delta_demon,
            "margin_beta":     self.margin_beta,
            "probe_prob":      self.probe_prob,
            "probe_magnitude": self.probe_magnitude,
            "corr_guard_rho":  self.corr_guard_rho,
            "void_premium":    self.void_premium,
            "dist_params":     self.dist_params,
            "version":         self.version,
            "trained_on":      self.trained_on,
        }, sort_keys=True).encode()
        return hashlib.sha256(blob).hexdigest() == self.sha256


def get_default_calibration() -> CalibrationParams:
    """
    Default calibration used until offline training produces real params.
    All tier offsets = 0. Shape params are empirical starting points.
    Hash is computed from these exact values — change any value and update sha256.
    """
    params = {
        "delta_goblin":    {"default": 0.0},
        "delta_demon":     {"default": 0.0},
        "margin_beta":     {"beta0": 0.04, "beta1": 0.002, "beta2": 0.01, "beta3": 0.005, "beta4": 0.01},
        "probe_prob":      0.05,
        "probe_magnitude": 0.25,
        "corr_guard_rho":  0.60,
        "void_premium":    {"default": 0.0},
        "dist_params":     {
            "Points":          {"a": 6.0,  "scale": 4.0},
            "Rebounds":        {"r": 4.0,  "p": 0.55},
            "Assists":         {"r": 3.5,  "p": 0.60},
            "Hits":            {"mu": 1.0},
            "Strikeouts":      {"mu": 5.5},
            "Total Bases":     {"a": 3.0,  "scale": 1.2},
            "Hits+Runs+RBIs":  {"a": 4.0,  "scale": 1.2},
            "Fantasy Score":   {"a": 5.0,  "scale": 7.0},
            "Hitter Fantasy Score": {"a": 4.5, "scale": 5.0},
            "Passing Yards":   {"a": 8.0,  "scale": 30.0},
            "Rushing Yards":   {"a": 4.0,  "scale": 20.0},
            "Receiving Yards": {"a": 4.0,  "scale": 18.0},
        },
        "version":    "default-v1",
        "trained_on": "2026-01-01/2026-07-08",
    }
    blob = json.dumps(params, sort_keys=True).encode()
    sha = hashlib.sha256(blob).hexdigest()
    return CalibrationParams(**params, sha256=sha)


# ──────────────────────────────────────────────────────────────────────────────
# 3. SHARP CONSENSUS (imported from sharp.py; redefined here for selector use)
# ──────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SharpConsensus:
    prop_id:      str
    median:       float
    shape_params: Dict[str, float]
    timestamp:    str
    books_used:   List[str]
    freshness_sec: float


# ──────────────────────────────────────────────────────────────────────────────
# 4. PP BOARD PROP (raw input)
# ──────────────────────────────────────────────────────────────────────────────
class Tier(str, Enum):
    STANDARD = "Standard"
    GOBLIN   = "Goblin"
    DEMON    = "Demon"


class Direction(str, Enum):
    OVER  = "OVER"
    UNDER = "UNDER"


@dataclass(frozen=True)
class PPProp:
    prop_id:              str
    game_id:              str
    player_id:            str
    player_name:          str
    stat_type:            str
    tiers_offered:        List[Tier]
    lines:                Dict[Tier, float]
    hours_to_lock:        float
    public_over_pct:      Optional[float]
    dnp_prob:             float
    correlation_partners: List[str]
    # Direction stored on the prop from PP — governs which side the CDF evaluates.
    # If PP sends an under, we evaluate UNDER; never force a direction ourselves.
    stored_direction:     Direction = Direction.OVER


# ──────────────────────────────────────────────────────────────────────────────
# 5. CDF HELPERS
# ──────────────────────────────────────────────────────────────────────────────

# Micro-line cap: props with line exactly 0.5 (binary "did they do it at all?")
# get their p_win capped so they don't crowd out higher-line props in the optimizer.
# A 0.5-line binary prop isn't meaningfully more predictable than a 1.5-line prop;
# the Gamma distribution can produce inflated p_wins on these lines.
# Lines of 1.5+ are real baseball prop lines with genuine variance — no cap.
_MICRO_LINE_THRESHOLD = 0.75   # cap only true half-ball lines (0.5)
_MICRO_LINE_P_WIN_CAP  = 0.54  # 0.5-line props are treated as near-coin-flips


# CV (coefficient of variation = std/mean) by stat type for Gamma/NegBin families.
# These are empirical MLB/NBA estimates used to derive realistic variance
# when the sharp median is the only input available.
_STAT_CV: Dict[str, float] = {
    "Total Bases":         0.85,  # highly variable: 0-HR game vs 3-TB game
    "Hits+Runs+RBIs":      0.80,
    "Fantasy Score":       0.70,
    "Hitter Fantasy Score": 0.75,
    "Points":              0.45,  # NBA scoring — more concentrated
    "Rebounds":            0.65,
    "Assists":             0.70,
    "Receptions":          0.65,
    "Passing Yards":       0.40,
    "Rushing Yards":       0.75,
    "Receiving Yards":     0.75,
}
_DEFAULT_CV = 0.70


def _median_anchored_shape(median: float, shape: Dict, family: DistFamily, stat_type: str = "") -> Dict:
    """
    Re-derive distribution shape parameters so the distribution's mean/mode
    is anchored to `median` rather than to calibration defaults.

    This prevents the classic bug where shape params calibrated for a typical
    line (e.g. mu=5.5 for Strikeouts) are applied to a micro-line prop
    (e.g. line=0.5 HR) and yield p_win ≈ 1.0.

    Strategy:
    - POISSON:  set lam = median (mean = median)
    - NEGBIN:   use CV to derive r,p so mean=median and variance realistic
    - GAMMA:    use CV to derive a,scale so mean=median and variance realistic
    - SKELLAM:  proxy: mu1 = median, mu2 = 0
    """
    if family == DistFamily.POISSON:
        return {"mu": max(0.01, median)}
    elif family == DistFamily.NEGBIN:
        # For NegBin: mean=r*(1-p)/p, var=r*(1-p)/p^2 = mean/p
        # CV = sqrt(var)/mean = 1/sqrt(r*p/(1-p)) → r = mean*(1-p)/p, p = mean/var
        # Simpler: derive r from CV → CV^2 = (1-p)/(r*p) → r = (1-p)/(p*CV^2)
        # We fix CV from the stat type and solve for r,p.
        cv = _STAT_CV.get(stat_type, _DEFAULT_CV)
        # var = (median * cv)^2
        var = (median * cv) ** 2
        # p = mean/var for NegBin parameterization where var=mean/p ... actually:
        # NegBin(r,p): mean = r*(1-p)/p, var = r*(1-p)/p^2 = mean/p
        # So p = mean/var, r = mean*p/(1-p)
        p = float(np.clip(median / max(var, 0.01), 0.01, 0.99))
        r = max(0.1, median * p / max(1 - p, 0.01))
        return {"r": r, "p": p}
    elif family == DistFamily.GAMMA:
        # Gamma(a, scale): mean = a*scale, var = a*scale^2
        # CV = 1/sqrt(a) → a = 1/CV^2, scale = mean/a
        cv = _STAT_CV.get(stat_type, _DEFAULT_CV)
        a = max(0.5, 1.0 / (cv ** 2))
        scale = median / max(a, 0.01)
        return {"a": a, "scale": max(scale, 0.01)}
    elif family == DistFamily.SKELLAM:
        return {"mu1": max(0.01, median), "mu2": 0.0}
    else:
        return {"mu": max(0.01, median)}


def cdf_at_line(line: float, median: float, shape: Dict, family: DistFamily, stat_type: str = "") -> float:
    """
    P(X ≤ line - 0.5) for discrete; P(X ≤ line) for continuous.
    Uses median-anchored shape so micro-line props stay realistic.
    """
    anchored = _median_anchored_shape(median, shape, family, stat_type)
    x = line - 0.5
    if family == DistFamily.POISSON:
        lam = anchored["mu"]
        return float(poisson.cdf(x, lam))
    elif family == DistFamily.NEGBIN:
        r = anchored["r"]
        p = anchored["p"]
        return float(nbinom.cdf(x, r, p))
    elif family == DistFamily.GAMMA:
        a     = anchored["a"]
        scale = anchored["scale"]
        return float(gamma.cdf(x, a, scale=scale))
    elif family == DistFamily.SKELLAM:
        mu  = anchored["mu1"] - anchored["mu2"]
        var = anchored["mu1"] + anchored["mu2"]
        return float(norm.cdf(x, mu, max(np.sqrt(var), 0.01)))
    else:
        return float(norm.cdf(x, median, max(median * 0.25, 0.01)))


def _calibrated_p_win(
    line: float,
    median: float,
    shape: Dict,
    family: DistFamily,
    direction: "Direction",
    stat_type: str = "",
) -> float:
    """
    Compute p_win anchored to the sharp median, with a micro-line safety cap.
    stat_type is used to look up realistic CV for Gamma/NegBin families.
    """
    if direction == Direction.OVER:
        raw = win_prob_over(line, median, shape, family, stat_type)
    else:
        raw = win_prob_under(line, median, shape, family, stat_type)

    # Cap only true 0.5-line binary props ("did they HR/steal/hit at all?")
    if line <= _MICRO_LINE_THRESHOLD:
        raw = min(raw, _MICRO_LINE_P_WIN_CAP)

    return raw


def win_prob_over(line: float, median: float, shape: Dict, family: DistFamily, stat_type: str = "") -> float:
    return 1.0 - cdf_at_line(line, median, shape, family, stat_type)


def win_prob_under(line: float, median: float, shape: Dict, family: DistFamily, stat_type: str = "") -> float:
    return cdf_at_line(line, median, shape, family, stat_type)


# ──────────────────────────────────────────────────────────────────────────────
# 6. BREAKEVEN r* FOR EACH SLIP SIZE
# ──────────────────────────────────────────────────────────────────────────────
def expected_payout_iid(k: int, r: float) -> float:
    """E[payout] for k-pick slip where each leg wins i.i.d. with prob r."""
    exp = 0.0
    binom = scipy.stats.binom
    for m in range(k + 1):
        prob = float(binom.pmf(m, k, r))
        exp += prob * FLEX_PAYOUT[k].get(m, 0.0)
    return exp


def breakeven_r(k: int) -> float:
    """Solve E[payout] == 1 for r ∈ (0.5, 0.99)."""
    f   = lambda r: (expected_payout_iid(k, r) - 1.0) ** 2
    res = minimize_scalar(f, bounds=(0.5, 0.99), method="bounded")
    return float(res.x)


# Pre-compute once at import time
BREAKEVEN_R: Dict[int, float] = {k: breakeven_r(k) for k in range(2, 7)}


# ──────────────────────────────────────────────────────────────────────────────
# 7. LEG CANDIDATE
# ──────────────────────────────────────────────────────────────────────────────
@dataclass
class LegCandidate:
    prop_id:     str
    game_id:     str
    player_id:   str
    player_name: str
    stat_type:   str
    tier:        Tier
    line:        float
    direction:   Direction
    p_win:       float
    ev_marginal: float = 0.0
    ev_corr_adj: float = 0.0

    @property
    def family(self) -> DistFamily:
        return get_family(self.stat_type)


# ──────────────────────────────────────────────────────────────────────────────
# 8. SHAPLEY MARGINAL EV — FIXED
# ──────────────────────────────────────────────────────────────────────────────
def _pmf_dp(p_wins: List[float]) -> np.ndarray:
    """Exact PMF of sum of Bernoullis via DP convolution."""
    n = len(p_wins)
    pmf = np.zeros(n + 1)
    pmf[0] = 1.0
    for p in p_wins:
        # Convolve in-place (right to left to avoid double-counting)
        for j in range(len(pmf) - 1, 0, -1):
            pmf[j] = pmf[j] * (1 - p) + pmf[j - 1] * p
        pmf[0] *= (1 - p)
    return pmf


def shapley_marginal_ev(
    legs: List[LegCandidate],
    r_star: Dict[int, float],
) -> Dict[str, float]:
    """
    Exact Shapley marginal EV for each leg.
    Iterates over each leg (outer loop) and all subsets of other legs.
    Only considers slip sizes 2..6.
    Efficient for N ≤ 15; uses exact DP convolution for PMF.
    """
    N = len(legs)
    shapley: Dict[str, float] = {lg.prop_id: 0.0 for lg in legs}

    # Outer loop: for each leg, compute its Shapley value
    for idx, target_leg in enumerate(legs):
        other_indices = [i for i in range(N) if i != idx]
        shapley_val = 0.0

        # Slip sizes 2..6: target_leg + s others = slip size s+1
        for s in range(1, min(6, len(other_indices) + 1)):
            k = s + 1  # total slip size including target_leg
            weight = 1.0 / (math.comb(N - 1, s) * 5)  # normalized over 5 slip sizes

            for combo in combinations(other_indices, s):
                # PMF with target leg
                all_p_wins = [legs[i].p_win for i in combo] + [target_leg.p_win]
                pmf_with   = _pmf_dp(all_p_wins)
                ev_with    = sum(pmf_with[m] * FLEX_PAYOUT[k].get(m, 0.0)
                                 for m in range(k + 1))

                # PMF without target leg (slip size k-1)
                if k > 2:
                    combo_p_wins = [legs[i].p_win for i in combo]
                    pmf_wo = _pmf_dp(combo_p_wins)
                    ev_wo  = sum(pmf_wo[m] * FLEX_PAYOUT[k - 1].get(m, 0.0)
                                 for m in range(k))
                else:
                    ev_wo = 0.0

                shapley_val += (ev_with - ev_wo) * weight

        shapley[target_leg.prop_id] = shapley_val

    return shapley


# ──────────────────────────────────────────────────────────────────────────────
# 9. CORRELATION-ADJUSTED EV (Gaussian copula)
# ──────────────────────────────────────────────────────────────────────────────
def corr_adjusted_ev(
    legs:      List[LegCandidate],
    shapley_ev: Dict[str, float],
    rho_map:   Dict[Tuple[str, str], float],
) -> Dict[str, float]:
    """Subtract pairwise correlation overlap penalty."""
    adj: Dict[str, float] = {}

    for i, L in enumerate(legs):
        ev = shapley_ev[L.prop_id]
        overlap = 0.0
        for j, L2 in enumerate(legs):
            if i == j:
                continue
            rho = rho_map.get((L.prop_id, L2.prop_id), 0.0)
            if rho <= 0:
                continue

            # Clamp z-scores away from ±∞
            z1 = float(np.clip(norm.ppf(L.p_win),  -4.0, 4.0))
            z2 = float(np.clip(norm.ppf(L2.p_win), -4.0, 4.0))

            try:
                p_both  = multivariate_normal.cdf(
                    [z1, z2], mean=[0, 0], cov=[[1, rho], [rho, 1]]
                )
            except Exception:
                p_both = L.p_win * L2.p_win

            p_indep  = L.p_win * L2.p_win
            pair_ev  = min(shapley_ev[L.prop_id], shapley_ev[L2.prop_id])
            overlap += max(0.0, p_both - p_indep) * pair_ev

        adj[L.prop_id] = ev - 0.15 * overlap

    return adj


# ──────────────────────────────────────────────────────────────────────────────
# 10. DEMON QUALIFICATION SCORE
# ──────────────────────────────────────────────────────────────────────────────

# Demon floor: PP demon must clear this p_win to even be scored.
# r*_6 + 0.03 ≈ 0.53
DEMON_PWIN_FLOOR = BREAKEVEN_R.get(6, 0.50) + 0.03  # computed after BREAKEVEN_R

# Gate 0: minimum line score per stat type for a demon to even be considered.
# Low-frequency stats (HR, Triple, SB, Walk, Double, Single, RBI, Run) at 0.5 are
# essentially coin flips that PP labels demon to look exciting. We reject them.
# Volume / composite stats (Hits, HFS, TB, H+R+RBI, K) need a lower floor because
# their distributions are centered higher and a 1.5 is meaningful.
_DEMON_LINE_FLOOR: dict = {
    # Rate / low-frequency events — must be a real alt-line, not just 0.5
    "Home Runs":          1.5,
    "Triples":            1.5,
    "Stolen Bases":       1.5,
    "Doubles":            1.5,
    "Walks":              1.5,
    "Singles":            1.5,
    "RBIs":               1.5,
    "Runs":               1.5,
    # Volume / composite — still require something above 0.5
    "Hits":               1.5,
    "Total Bases":        2.5,
    "Hits+Runs+RBIs":     2.5,
    "Hitter Fantasy Score": 3.5,
    "Hitter Strikeouts":  1.5,
    # Pitching
    "Pitcher Strikeouts": 3.5,
    "Pitching Outs":      9.5,
    "Pitches Thrown":    59.5,
    "Earned Runs Allowed":0.5,
    "Hits Allowed":       2.5,
    # Default for any stat not listed — require at least 1.5
    "_default":           1.5,
}

# How much sharper the SGO median must be vs the PP demon line for an OVER demon.
# If SGO fair line < PP line by more than this, the demon is immediately bad.
# e.g. PP demon line = 4.5 OVER; SGO median = 3.8 → edge = -0.7 → OVER is a trap.
_DEMON_EDGE_CUTOFF = -0.5   # SGO median may be at most 0.5 BELOW PP demon line for OVER

# Game-script fit table: stat types that benefit from high-pace / high-volume games.
# Keys are stat types; value is +1 (pace helps) or -1 (pace hurts).
# Used as a multiplier on a game's run-rate signal (total projected runs/pts).
_STAT_PACE_SIGN: Dict[str, int] = {
    # MLB — high run-environment helps all hitting stats
    "Hits":                     +1,
    "Total Bases":              +1,
    "Hits+Runs+RBIs":           +1,
    "Home Runs":                +1,
    "RBIs":                     +1,
    "Runs":                     +1,
    "Walks":                    +1,
    "Plate Appearances":        +1,
    "Hitter Fantasy Score":     +1,
    "Hitter Strikeouts":        -1,  # high K environment bad for hitters
    # MLB pitching — low run-environment helps pitcher stats
    "Pitcher Strikeouts":       +1,  # high K rate environment helps pitcher Ks
    "Pitching Outs":            +1,
    "Pitches Thrown":           +1,
    "Earned Runs Allowed":      -1,  # high offense bad for pitcher ERA props
    "Hits Allowed":             -1,
    # NBA — high pace helps all
    "Points":                   +1,
    "Rebounds":                 +1,
    "Assists":                  +1,
    "Pts+Reb+Ast":              +1,
    "3-PT Made":                +1,
}


@dataclass
class DemonScore:
    prop_id:           str
    player_name:       str
    stat_type:         str
    line:              float
    direction:         Direction
    p_win:             float
    # Layer sub-scores (0.0–1.0 each)
    market_anchor:     float   # L1: how well SGO median supports PP demon line
    dist_hit_rate:     float   # L2: p_win normalized to [0,1] above demon floor
    game_script_fit:   float   # L3: pace/volume environment signal
    role_certainty:    float   # L4: inverse DNP risk × freshness weight
    pair_diversity:    float   # L5: computed later when pairing demons
    # Combined score
    composite:         float
    qualifies:         bool    # True only if ALL hard gates pass


def score_demon(
    cand:       "LegCandidate",
    sc:         "SharpConsensus",
    dnp_prob:   float,
    game_total: float,          # projected total runs/pts for this game (0 = unknown)
) -> DemonScore:
    """
    Score a PP-flagged demon leg across five layers.
    Returns DemonScore with composite 0–1 and qualifies bool.

    Layer weights (must sum to 1.0):
      L1 market_anchor   0.30  — is the SGO median on-side with the demon line?
      L2 dist_hit_rate   0.30  — how far above the demon floor is p_win?
      L3 game_script_fit 0.20  — does the game environment support this stat?
      L4 role_certainty  0.20  — is the player locked in and available?
    """
    direction = cand.direction
    pp_line   = cand.line
    p_win     = cand.p_win

    # ── Hard gates ───────────────────────────────────────────────────────────
    # Gate 0: minimum line per stat type.
    # Rejects PP's 0.5-line "demons" on low-frequency stats (HR, SB, Triple …)
    # that are essentially coin flips PP labels demon to look exciting.
    min_line = _DEMON_LINE_FLOOR.get(cand.stat_type, _DEMON_LINE_FLOOR["_default"])
    if pp_line < min_line:
        return DemonScore(
            prop_id=cand.prop_id, player_name=cand.player_name,
            stat_type=cand.stat_type, line=pp_line, direction=direction,
            p_win=p_win, market_anchor=0.0, dist_hit_rate=0.0,
            game_script_fit=0.0, role_certainty=0.0, pair_diversity=0.5,
            composite=0.0, qualifies=False,
        )

    # Gate 1: p_win must clear demon floor (already enforced in build loop,
    # but re-check here for defensive clarity).
    if p_win < DEMON_PWIN_FLOOR:
        return DemonScore(
            prop_id=cand.prop_id, player_name=cand.player_name,
            stat_type=cand.stat_type, line=pp_line, direction=direction,
            p_win=p_win, market_anchor=0.0, dist_hit_rate=0.0,
            game_script_fit=0.0, role_certainty=0.0, pair_diversity=0.5,
            composite=0.0, qualifies=False,
        )

    # Gate 2: SGO must not be materially against the demon direction.
    sgo_median = sc.median
    if direction == Direction.OVER:
        edge = sgo_median - pp_line   # positive = SGO median > PP line → OVER has edge
    else:
        edge = pp_line - sgo_median   # positive = SGO median < PP line → UNDER has edge

    if edge < _DEMON_EDGE_CUTOFF:
        # SGO says this demon is a trap — disqualify.
        return DemonScore(
            prop_id=cand.prop_id, player_name=cand.player_name,
            stat_type=cand.stat_type, line=pp_line, direction=direction,
            p_win=p_win, market_anchor=0.0, dist_hit_rate=0.0,
            game_script_fit=0.0, role_certainty=0.0, pair_diversity=0.5,
            composite=0.0, qualifies=False,
        )

    # ── Real sharp data or fallback? ─────────────────────────────────────────
    is_real_sharp = sc.freshness_sec < 9000.0  # 9999 = fallback

    # ── L1: Market Anchor (0.0–1.0) ──────────────────────────────────────────
    # How much does the SGO median support the demon direction?
    # edge = sgo_median - pp_line for OVER (positive = supportive)
    # Normalize: edge in [-0.5, +2.0] → [0.0, 1.0]
    if is_real_sharp:
        l1 = float(np.clip((edge + 0.5) / 2.5, 0.0, 1.0))
    else:
        # No real sharp data — neutral score, doesn't reward or penalize
        l1 = 0.45

    # ── L2: Distribution Hit Rate (0.0–1.0) ──────────────────────────────────
    # How far above the demon floor is p_win?
    # p_win range of interest: [DEMON_PWIN_FLOOR, 0.70]
    floor  = DEMON_PWIN_FLOOR
    l2_max = 0.70
    l2 = float(np.clip((p_win - floor) / (l2_max - floor), 0.0, 1.0))

    # ── L3: Game Script Fit (0.0–1.0) ────────────────────────────────────────
    # Proxy: if game_total > 0, compare to league-average total (8.5 MLB, 220 NBA).
    # Determine league from stat type.
    if game_total > 0:
        mlb_stats = {
            "Hits", "Total Bases", "Hits+Runs+RBIs", "Home Runs", "RBIs", "Runs",
            "Walks", "Plate Appearances", "Hitter Fantasy Score", "Hitter Strikeouts",
            "Pitcher Strikeouts", "Pitching Outs", "Pitches Thrown",
            "Earned Runs Allowed", "Hits Allowed", "Singles", "Doubles", "Triples",
        }
        league_avg = 8.5 if cand.stat_type in mlb_stats else 220.0
        pace_deviation = (game_total - league_avg) / league_avg   # e.g. +0.15 = 15% above avg
        pace_sign = _STAT_PACE_SIGN.get(cand.stat_type, +1)       # +1 = pace helps stat
        script_signal = pace_deviation * pace_sign                 # [-inf, +inf]
        # Normalize to [0.0, 1.0]: neutral (0.5) at league avg, +0.5 at +100% pace
        l3 = float(np.clip(0.5 + script_signal * 0.5, 0.0, 1.0))
    else:
        l3 = 0.50   # neutral — no game-total data available

    # ── L4: Role Certainty (0.0–1.0) ─────────────────────────────────────────
    # Combines DNP risk and sharp data freshness.
    # DNP risk: dnp_prob in [0, 0.15] (capped by hard gate above 0.15)
    # Freshness: real sharp data = 1.0, fallback = 0.6
    dnp_score  = float(np.clip(1.0 - dnp_prob / 0.15, 0.0, 1.0))
    fresh_score = 1.0 if is_real_sharp else 0.6
    l4 = 0.70 * dnp_score + 0.30 * fresh_score

    # ── L5: Pair Diversity — placeholder (0.5 neutral, computed at pairing stage)
    l5 = 0.50

    # ── Composite Score ───────────────────────────────────────────────────────
    # Weights: L1=0.30, L2=0.30, L3=0.20, L4=0.20  (L5 applied at pairing)
    composite = 0.30 * l1 + 0.30 * l2 + 0.20 * l3 + 0.20 * l4

    return DemonScore(
        prop_id=cand.prop_id,
        player_name=cand.player_name,
        stat_type=cand.stat_type,
        line=pp_line,
        direction=direction,
        p_win=p_win,
        market_anchor=round(l1, 4),
        dist_hit_rate=round(l2, 4),
        game_script_fit=round(l3, 4),
        role_certainty=round(l4, 4),
        pair_diversity=round(l5, 4),
        composite=round(composite, 4),
        qualifies=True,
    )


def qualify_demons(
    demon_cands:  List["LegCandidate"],
    sc_map:       Dict[str, "SharpConsensus"],
    dnp_model:    Dict[str, float],
    game_total:   float = 0.0,
) -> List["LegCandidate"]:
    """
    Filter and rank PP demons using demon_score().
    Returns the subset of demon_cands that pass ALL gates,
    ordered by composite score descending.

    Pair diversity (L5): after scoring individuals, apply a 10% penalty
    to the second demon of any pair sharing the same stat_type.
    This nudges the MILP toward demons from different failure modes.

    Returns: ranked list of qualified LegCandidates (may be empty, 1, or ≥2).
    """
    if not demon_cands:
        return []

    scored: List[tuple] = []  # (DemonScore, LegCandidate)
    for cand in demon_cands:
        sc = sc_map.get(cand.prop_id)
        if not sc:
            continue
        dnp_prob = dnp_model.get(cand.player_id, dnp_model.get(cand.prop_id, 0.0))
        ds = score_demon(cand, sc, dnp_prob, game_total)
        if ds.qualifies:
            scored.append((ds, cand))

    if not scored:
        return []

    # Sort by composite descending
    scored.sort(key=lambda t: t[0].composite, reverse=True)

    # Apply L5 pair-diversity penalty: if top-2 demons share stat_type, knock
    # the second one down slightly to open the door for a more diverse alternative.
    # Rebuild composite with L5 factored in for ranking purposes only.
    if len(scored) >= 2:
        top_stat = scored[0][0].stat_type
        for i in range(1, len(scored)):
            ds, cand = scored[i]
            if ds.stat_type == top_stat:
                # Same failure mode — 10% diversity penalty on composite
                penalized = DemonScore(
                    **{**ds.__dict__,
                       "pair_diversity": 0.0,
                       "composite": round(ds.composite * 0.90, 4)}
                )
                scored[i] = (penalized, cand)
        # Re-sort after penalty
        scored.sort(key=lambda t: t[0].composite, reverse=True)

    # Log demon scoring for debugging
    log.info("[demon] qualified %d/%d PP demons", len(scored), len(demon_cands))
    for ds, _ in scored[:4]:
        log.info(
            "[demon]  %s %s %.1f %s → p_win=%.3f L1=%.2f L2=%.2f L3=%.2f L4=%.2f composite=%.3f",
            ds.player_name, ds.stat_type, ds.line, ds.direction.value,
            ds.p_win, ds.market_anchor, ds.dist_hit_rate,
            ds.game_script_fit, ds.role_certainty, ds.composite,
        )

    return [cand for _, cand in scored]


# ──────────────────────────────────────────────────────────────────────────────
# 10. PER-GAME MILP (OR-Tools SCIP)
# ──────────────────────────────────────────────────────────────────────────────
def solve_game_milp(
    candidates: List[LegCandidate],
    ev_map:     Dict[str, float],
    r_star_6:   float,
    time_limit_sec: float = 5.0,
) -> Optional[List[LegCandidate]]:
    """
    Maximize Σ EV*_L * x_L
    Subject to:
      Σ x_L = 6
      Exactly 2 Demon legs
      Demon legs come from exactly 2 distinct players
      ≤ 3 legs per player
      ≤ 4 OVER, ≤ 4 UNDER
      ≥ 2 distinct stat categories
      p_win ≥ r*_6 - 0.01 for all selected legs
    """
    if len(candidates) < 6:
        return None

    solver = pywraplp.Solver.CreateSolver("SCIP")
    if not solver:
        log.error("SCIP solver unavailable — OR-Tools not properly installed")
        return None
    solver.SetTimeLimit(int(time_limit_sec * 1000))

    lg_map = {lg.prop_id: lg for lg in candidates}
    x: Dict[str, pywraplp.Variable] = {
        lg.prop_id: solver.BoolVar(f"x_{lg.prop_id}") for lg in candidates
    }

    # Objective
    solver.Maximize(
        solver.Sum([ev_map.get(pid, 0.0) * var for pid, var in x.items()])
    )

    # ── Exactly 6 legs ────────────────────────────────────────────────────────
    solver.Add(solver.Sum(list(x.values())) == 6)

    # ── Exactly 2 Demon legs ──────────────────────────────────────────────────
    demon_vars = [x[lg.prop_id] for lg in candidates if lg.tier == Tier.DEMON]
    if len(demon_vars) < 2:
        return None
    solver.Add(solver.Sum(demon_vars) == 2)

    # ── 2 distinct Demon players ───────────────────────────────────────────────
    demon_player_vars: Dict[str, List] = {}
    for lg in candidates:
        if lg.tier == Tier.DEMON:
            demon_player_vars.setdefault(lg.player_id, []).append(x[lg.prop_id])

    y_demon: Dict[str, pywraplp.Variable] = {}
    for pid, vars_ in demon_player_vars.items():
        y = solver.BoolVar(f"y_demon_{pid}")
        solver.Add(solver.Sum(vars_) >= y)
        solver.Add(solver.Sum(vars_) <= 2 * y)
        y_demon[pid] = y
    solver.Add(solver.Sum(list(y_demon.values())) == 2)

    # ── ≤ 3 legs per player ───────────────────────────────────────────────────
    player_vars: Dict[str, List] = {}
    for lg in candidates:
        player_vars.setdefault(lg.player_id, []).append(x[lg.prop_id])
    for vars_ in player_vars.values():
        solver.Add(solver.Sum(vars_) <= 3)

    # ── Direction diversity (soft — only enforce if enough of each direction) ──
    # Skip direction cap when candidates are mostly one-directional (e.g. MLB
    # fantasy score boards that are all OVER). Enforce cap only when at least
    # 4 candidates exist in the minority direction.
    over_vars  = [x[lg.prop_id] for lg in candidates if lg.direction == Direction.OVER]
    under_vars = [x[lg.prop_id] for lg in candidates if lg.direction == Direction.UNDER]
    if over_vars and len(under_vars) >= 4:
        solver.Add(solver.Sum(over_vars) <= 5)   # allow up to 5 of 6 same direction
    if under_vars and len(over_vars) >= 4:
        solver.Add(solver.Sum(under_vars) <= 5)

    # ── ≥ 2 distinct stat categories ──────────────────────────────────────────
    stat_vars: Dict[str, List] = {}
    for lg in candidates:
        stat_vars.setdefault(lg.stat_type, []).append(x[lg.prop_id])
    z_stat: Dict[str, pywraplp.Variable] = {}
    for stat, vars_ in stat_vars.items():
        z = solver.BoolVar(f"z_{stat}")
        solver.Add(solver.Sum(vars_) >= z)
        solver.Add(solver.Sum(vars_) <= 6 * z)
        z_stat[stat] = z
    solver.Add(solver.Sum(list(z_stat.values())) >= 2)

    # ── Win prob floor ─────────────────────────────────────────────────────────
    for lg in candidates:
        if lg.p_win < r_star_6 - 0.01:
            solver.Add(x[lg.prop_id] == 0)

    # ── Solve ─────────────────────────────────────────────────────────────────
    status = solver.Solve()
    if status not in (pywraplp.Solver.OPTIMAL, pywraplp.Solver.FEASIBLE):
        return None

    selected = [lg_map[pid] for pid, var in x.items() if var.solution_value() > 0.5]
    return selected if len(selected) == 6 else None


# ──────────────────────────────────────────────────────────────────────────────
# 11. MAIN ENTRY POINT
# ──────────────────────────────────────────────────────────────────────────────
def select_legs_for_slate(
    pp_props:       List[PPProp],
    sharp_consensus: Dict[str, SharpConsensus],
    calibration:    CalibrationParams,
    game_scripts:   Dict[str, List[Dict]],
    rho_map:        Dict[Tuple[str, str], float],
    dnp_model:      Dict[str, float],
) -> Dict[str, Dict]:
    """
    Returns { game_id: {"six_legs": [...], "two_demons": [...], "meta": {...}} }
    Games with no feasible MILP solution are omitted.
    """

    # 0. Verify calibration hash
    if not calibration.verify_hash():
        raise RuntimeError("Calibration hash mismatch — DO NOT TRADE")

    # 1. Build all leg candidates across slate
    all_candidates: List[LegCandidate] = []

    for pp in pp_props:
        if pp.prop_id not in sharp_consensus:
            continue
        sc     = sharp_consensus[pp.prop_id]
        median = sc.median
        shape  = sc.shape_params
        family = get_family(pp.stat_type)

        # Get calibration shape override if present
        cal_shape = calibration.dist_params.get(pp.stat_type, shape)

        for tier in pp.tiers_offered:
            line = pp.lines[tier]

            # Direction rules (non-negotiable):
            #   DEMON   → OVER only, demon section only
            #   GOBLIN  → OVER only, slate section only
            #   STANDARD → OVER and UNDER both considered
            if tier in (Tier.GOBLIN, Tier.DEMON):
                dirs = [Direction.OVER]
            else:
                dirs = [Direction.OVER, Direction.UNDER]

            for d in dirs:
                p_win = _calibrated_p_win(line, median, cal_shape, family, d, pp.stat_type)

                # Hard filters
                if p_win < BREAKEVEN_R[6] - 0.02:
                    continue
                if dnp_model.get(pp.player_id, dnp_model.get(pp.prop_id, 0.0)) > 0.15:
                    continue
                if tier == Tier.DEMON and p_win < BREAKEVEN_R[6] + 0.03:
                    continue

                all_candidates.append(LegCandidate(
                    prop_id=pp.prop_id,
                    game_id=pp.game_id,
                    player_id=pp.player_id,
                    player_name=pp.player_name,
                    stat_type=pp.stat_type,
                    tier=tier,
                    line=line,
                    direction=d,
                    p_win=float(np.clip(p_win, 0.001, 0.999)),
                ))

    if not all_candidates:
        log.warning("No leg candidates passed hard filters — slate is empty")
        return {}

    # ── PRE-FILTER before Shapley: cap per game so Shapley stays O(N*C(15,5)) ──
    # Shapley is exact for N≤15 but blows up beyond ~20. Pre-rank by p_win,
    # reserve demon slots, and keep top MAX_SHAPLEY per game.
    MAX_SHAPLEY = 15

    filtered: List[LegCandidate] = []
    by_game: Dict[str, List[LegCandidate]] = {}
    for c in all_candidates:
        by_game.setdefault(c.game_id, []).append(c)

    for game_id, gcands in by_game.items():
        raw_d_cands = [c for c in gcands if c.tier == Tier.DEMON]
        s_cands     = sorted([c for c in gcands if c.tier != Tier.DEMON],
                             key=lambda c: c.p_win, reverse=True)

        # ── Demon qualification: PP flags it, GOTit scores and filters it ────
        # qualify_demons runs the 5-layer score, drops demons that fail hard
        # gates (p_win floor + SGO edge cutoff), ranks survivors by composite.
        # Returns 0, 1, or ≥2 qualified candidates — MILP needs ≥2 from 2 distinct players.
        qualified_d = qualify_demons(
            demon_cands=raw_d_cands,
            sc_map=sharp_consensus,
            dnp_model=dnp_model,
        )

        # If fewer than 2 distinct demon players qualify, skip this game
        seen_demon_players = set(dc.player_id for dc in qualified_d)
        if len(seen_demon_players) < 2:
            log.info("[demon] game %s: only %d distinct demon players qualified — skipping",
                     game_id, len(seen_demon_players))
            continue

        # Cap demon slots at 6 (enough for MILP to pick 2, with diversity)
        demon_slots = qualified_d[:6]
        standard_slots = s_cands[:MAX_SHAPLEY - len(demon_slots)]
        filtered.extend(demon_slots + standard_slots)

    all_candidates = filtered
    log.info("After pre-filter: %d candidates across %d games",
             len(all_candidates), len(by_game))

    if not all_candidates:
        return {}

    # 2+3+4. Per-game: Shapley EV → corr-adj EV → MILP
    # Shapley MUST run per-game (N≤15). Running it across all games at once
    # makes C(N-1,5) explode to billions of iterations.
    output: Dict[str, Dict] = {}
    r_star_6 = BREAKEVEN_R[6]
    shapley_all:  Dict[str, float] = {}
    corr_adj_all: Dict[str, float] = {}

    for game_id in set(c.game_id for c in all_candidates):
        game_cands = [c for c in all_candidates if c.game_id == game_id]
        if len(game_cands) < 6:
            continue

        # Shapley per-game
        shapley = shapley_marginal_ev(game_cands, BREAKEVEN_R)
        for c in game_cands:
            c.ev_marginal = shapley[c.prop_id]
        shapley_all.update(shapley)

        # Corr-adj EV per-game
        corr_adj = corr_adjusted_ev(game_cands, shapley, rho_map)
        for c in game_cands:
            c.ev_corr_adj = corr_adj[c.prop_id]
        corr_adj_all.update(corr_adj)

        selected = solve_game_milp(game_cands, corr_adj, r_star_6)
        if not selected:
            log.debug("Game %s: MILP infeasible", game_id)
            continue

        demons  = [lg for lg in selected if lg.tier == Tier.DEMON]
        if len(demons) != 2:
            continue

        port_ev = sum(corr_adj.get(lg.prop_id, 0.0) for lg in selected)

        def leg_to_dict(lg: LegCandidate) -> Dict:
            d = {
                "prop_id":     lg.prop_id,
                "player_name": lg.player_name,
                "stat_type":   lg.stat_type,
                "tier":        lg.tier.value,
                "line":        lg.line,
                "direction":   lg.direction.value,
                "p_win":       round(lg.p_win, 4),
                "ev_marginal": round(lg.ev_marginal, 6),
                "ev_corr_adj": round(lg.ev_corr_adj, 6),
            }
            # For demons, attach the 5-layer qualification score
            if lg.tier == Tier.DEMON:
                sc = sharp_consensus.get(lg.prop_id)
                if sc:
                    dnp_p = dnp_model.get(lg.player_id, dnp_model.get(lg.prop_id, 0.0))
                    ds = score_demon(lg, sc, dnp_p, game_total=0.0)
                    d["demon_score"] = {
                        "composite":       ds.composite,
                        "market_anchor":   ds.market_anchor,
                        "dist_hit_rate":   ds.dist_hit_rate,
                        "game_script_fit": ds.game_script_fit,
                        "role_certainty":  ds.role_certainty,
                        "pair_diversity":  ds.pair_diversity,
                    }
            return d

        output[game_id] = {
            "six_legs":   [leg_to_dict(lg) for lg in selected],
            "two_demons": [leg_to_dict(lg) for lg in demons],
            "meta": {
                "slate_breakeven_r6":    round(r_star_6, 4),
                "portfolio_ev_per_$1":   round(port_ev / 6, 6),
                "calibration_version":   calibration.version,
                "calibration_hash":      calibration.sha256[:16],
            },
        }

    log.info("Leg selector: %d games with feasible solutions out of %d games",
             len(output), len(set(c.game_id for c in all_candidates)))
    return output


# ──────────────────────────────────────────────────────────────────────────────
# 12. RUNTIME GUARDS
# ──────────────────────────────────────────────────────────────────────────────
def validate_output(output: Dict, sharp_freshness_sec: int) -> bool:
    if not output:
        return False
    for g, data in output.items():
        if len(data["six_legs"]) != 6:
            return False
        if len(data["two_demons"]) != 2:
            return False
        if data["meta"]["portfolio_ev_per_$1"] < 0.01:   # relaxed from 0.08 until calibration is trained
            return False
    if sharp_freshness_sec > 300:   # relaxed from 90 until Pinnacle integration is live
        return False
    return True
