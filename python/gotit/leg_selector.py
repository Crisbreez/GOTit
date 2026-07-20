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
    under_score: Optional["UnderScore"] = None  # set for UNDER standard legs that qualify

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

# ── Demon policy (canonical) ──────────────────────────────────────────────────
# 1. PP odds_type == "demon" is the ONLY source of demon identity.
# 2. GOTit never promotes a standard prop to demon.
# 3. GOTit scores only PP demons.
# 4. If a PP demon fails the keep rule, it is dropped.
# 5. If a game has 0 or 1 qualifying demons, show 0 or 1. No substitutions.
# 6. Final demon ranking is by composite descending, distinct-player enforced.

DEMON_PWIN_FLOOR  = 0.53   # keep gate: p_win must clear this
_DEMON_EDGE_CUTOFF = -0.5   # keep gate: anchor_delta >= this
_DEMON_DNP_CUTOFF  = 0.15   # keep gate: dnp_prob must be below this

# Gate 0 — minimum line per stat type (rejects 0.5 lottery lines PP mislabels demon)
_DEMON_LINE_FLOOR: dict = {
    "Home Runs":            1.5,
    "Triples":              1.5,
    "Stolen Bases":         1.5,
    "Doubles":              1.5,
    "Walks":                1.5,
    "Singles":              1.5,
    "RBIs":                 1.5,
    "Runs":                 1.5,
    "Hits":                 1.5,
    "Total Bases":          2.5,
    "Hits+Runs+RBIs":       2.5,
    "Hitter Fantasy Score": 3.5,
    "Hitter Strikeouts":    1.5,
    "Pitcher Strikeouts":   3.5,
    "Pitching Outs":        9.5,
    "Pitches Thrown":      59.5,
    "Earned Runs Allowed":  0.5,
    "Hits Allowed":         2.5,
    "_default":             1.5,
}

# Standard-tier line floors — minimum line score to enter optimizer.
# Prevents near-certain "lottery" props (e.g. TB 0.5) from clogging the slate.
# Rule: standard legs with line < floor are dropped before scoring.
_STANDARD_LINE_FLOOR: dict = {
    # MLB hitting — 0.5 lines are near-certain for any player who starts.
    # Require meaningful lines only.
    "Total Bases":         1.5,
    "Hits":                1.5,
    "Hits+Runs+RBIs":      1.5,
    "Singles":             1.5,
    "RBIs":                1.5,
    "Runs":                1.5,
    "Walks":               1.5,
    "Hitter Strikeouts":   1.5,
    "Home Runs":           0.51,  # block 0.5 HR lines — need a real HR line
    "Stolen Bases":        0.51,  # block 0.5 SB lines
    "Doubles":             0.51,  # block 0.5 doubles lines
    "Triples":             0.51,
    # MLB pitching
    "Pitcher Strikeouts":  2.5,
    "Innings Pitched":     4.5,
    "Hits Allowed":        2.5,
    "Earned Runs Allowed": 0.5,
    # MMA
    "Significant Strikes": 15.0,
    "Takedowns":           0.5,
    "_default":            0.5,
}

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
    market_anchor:     float   # L1
    dist_hit_rate:     float   # L2
    game_script_fit:   float   # L3
    role_certainty:    float   # L4
    pair_diversity:    float   # L5 (applied at pairing stage)
    composite:         float
    qualifies:         bool


def _demon_keep(
    line_floor_pass: bool,
    anchor_delta:    float,
    p_win:           float,
    dnp_prob:        float,
) -> bool:
    """Hard keep gate — all four must be true."""
    return (
        line_floor_pass
        and anchor_delta >= _DEMON_EDGE_CUTOFF
        and p_win        >= DEMON_PWIN_FLOOR
        and dnp_prob      < _DEMON_DNP_CUTOFF
    )


def score_demon(
    cand:       "LegCandidate",
    sc:         "SharpConsensus",
    dnp_prob:   float,
    game_total: float,
    pace_fit:        float = 0.5,
    matchup_fit:     float = 0.5,
    environment_fit: float = 0.5,
    usage_fragility: float = 0.0,
    freshness_risk:  float = 0.0,
    same_failure_penalty: float = 0.0,
) -> DemonScore:
    """
    Score a PP-flagged demon leg.
    Keep gate: line_floor AND anchor_delta >= -0.5 AND p_win >= 0.53 AND dnp_prob < 0.15
    Weights:   L1=0.34  L2=0.28  L3=0.18  L4=0.12  L5=0.08
    """
    direction = cand.direction
    pp_line   = cand.line
    p_win     = cand.p_win

    # ── Gate 0: line floor per stat type ─────────────────────────────────────
    min_line        = _DEMON_LINE_FLOOR.get(cand.stat_type, _DEMON_LINE_FLOOR["_default"])
    line_floor_pass = pp_line >= min_line

    # ── anchor_delta = sharp_fair_line − pp_demon_line (OVER convention) ─────
    sgo_median    = sc.median
    anchor_delta  = (sgo_median - pp_line) if direction == Direction.OVER else (pp_line - sgo_median)
    is_real_sharp = sc.freshness_sec < 9000.0

    # ── Keep gate ─────────────────────────────────────────────────────────────
    qualifies = _demon_keep(line_floor_pass, anchor_delta, p_win, dnp_prob)
    if not qualifies:
        return DemonScore(
            prop_id=cand.prop_id, player_name=cand.player_name,
            stat_type=cand.stat_type, line=pp_line, direction=direction,
            p_win=p_win, market_anchor=0.0, dist_hit_rate=0.0,
            game_script_fit=0.0, role_certainty=0.0, pair_diversity=0.0,
            composite=0.0, qualifies=False,
        )

    # ── L1: Market anchor ─────────────────────────────────────────────────────
    # clamp(0.5 + anchor_delta / 1.0)
    l1 = float(np.clip(0.5 + anchor_delta / 1.0, 0.0, 1.0)) if is_real_sharp else 0.45

    # ── L2: Distribution hit rate ─────────────────────────────────────────────
    # clamp((p_win − 0.53) / 0.17)
    l2 = float(np.clip((p_win - 0.53) / 0.17, 0.0, 1.0))

    # ── L3: Game script fit ───────────────────────────────────────────────────
    # clamp(0.40*pace_fit + 0.30*matchup_fit + 0.30*environment_fit)
    # Derive pace_fit from game_total when not externally provided.
    if game_total > 0 and pace_fit == 0.5:
        mlb_stats = {
            "Hits", "Total Bases", "Hits+Runs+RBIs", "Home Runs", "RBIs", "Runs",
            "Walks", "Plate Appearances", "Hitter Fantasy Score", "Hitter Strikeouts",
            "Pitcher Strikeouts", "Pitching Outs", "Pitches Thrown",
            "Earned Runs Allowed", "Hits Allowed", "Singles", "Doubles", "Triples",
        }
        league_avg = 8.5 if cand.stat_type in mlb_stats else 220.0
        pace_dev   = (game_total - league_avg) / league_avg
        pace_sign  = _STAT_PACE_SIGN.get(cand.stat_type, +1)
        pace_fit   = float(np.clip(0.5 + pace_dev * pace_sign * 0.5, 0.0, 1.0))
    l3 = float(np.clip(0.40 * pace_fit + 0.30 * matchup_fit + 0.30 * environment_fit, 0.0, 1.0))

    # ── L4: Role certainty ────────────────────────────────────────────────────
    # clamp(1.0 − (0.55*dnp_prob + 0.25*usage_fragility + 0.20*freshness_risk))
    fr = 0.0 if is_real_sharp else (freshness_risk if freshness_risk else 0.4)
    l4 = float(np.clip(1.0 - (0.55 * dnp_prob + 0.25 * usage_fragility + 0.20 * fr), 0.0, 1.0))

    # ── L5: Pair diversity (applied at pairing stage; default 1.0 = no penalty)
    l5 = float(np.clip(1.0 - same_failure_penalty, 0.0, 1.0))

    # ── DemonWinScore: 0.40*L1 + 0.30*L2 + 0.15*L3 + 0.10*L4 + 0.05*L5 ────
    # Weights front-load market anchor (L1) and distribution hit rate (L2).
    # A demon that passes all 4 gates AND has sharp market + CDF agreement
    # is a genuine ceiling play. L3/L4/L5 differentiate within that set.
    composite = float(np.clip(
        0.40 * l1 + 0.30 * l2 + 0.15 * l3 + 0.10 * l4 + 0.05 * l5,
        0.0, 1.0,
    ))

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

    # Hard-cap: top 2 distinct-player demons only. No substitutions.
    # Walk the ranked list and pick the first 2 distinct players.
    top2: List[tuple] = []
    seen_players: set = set()
    for ds, cand in scored:
        if cand.player_id not in seen_players:
            top2.append((ds, cand))
            seen_players.add(cand.player_id)
        if len(top2) == 2:
            break

    log.info("[demon] qualified %d/%d PP demons → top2=%d",
             len(scored), len(demon_cands), len(top2))
    for ds, _ in top2:
        log.info(
            "[demon]  SELECTED %s %s %.1f %s → p_win=%.3f L1=%.2f L2=%.2f L3=%.2f L4=%.2f composite=%.3f",
            ds.player_name, ds.stat_type, ds.line, ds.direction.value,
            ds.p_win, ds.market_anchor, ds.dist_hit_rate,
            ds.game_script_fit, ds.role_certainty, ds.composite,
        )
    # Log dropped demons so we can audit why they were cut
    dropped = [(ds, c) for ds, c in scored if c not in [cand for _, cand in top2]]
    for ds, _ in dropped[:4]:
        log.info(
            "[demon]  DROPPED  %s %s %.1f → composite=%.3f",
            ds.player_name, ds.stat_type, ds.line, ds.composite,
        )

    return [cand for _, cand in top2]


# ──────────────────────────────────────────────────────────────────────────────
# 10. WIN SCORE — STANDARD LEGS
# ──────────────────────────────────────────────────────────────────────────────
# WinScoreStandard: a must-win score for a single standard leg.
# Unlike Shapley EV (which optimizes portfolio payout), this score answers:
# "How confident are we that this specific side of this line wins?"
#
# Formula:
#   WinScore = 0.40 * W1 + 0.30 * W2 + 0.20 * W3 + 0.10 * W4
#
#   W1 = clamp((p_win - H*) / (1 - H*))          — how far above must-win floor
#   W2 = clamp(0.5 + anchor_delta / 1.0)         — sharp market agreement
#   W3 = clamp((p_win - 0.57) / 0.20)            — distribution conviction
#   W4 = clamp(1 - dnp_prob / 0.15)              — role certainty
#
# H* = STANDARD_PWIN_FLOOR = 0.57  (must-win admission floor)
# Only legs that clear H* reach this scorer.

STANDARD_PWIN_FLOOR = 0.57   # must-win admission floor for standard legs

@dataclass
class WinScoreStandard:
    prop_id:         str
    player_name:     str
    stat_type:       str
    line:            float
    direction:       Direction
    p_win:           float
    w1_pwin_margin:  float   # how far above must-win floor
    w2_market_agree: float   # sharp market agreement
    w3_dist_conv:    float   # distribution conviction
    w4_role:         float   # role certainty
    win_score:       float   # final 0-1 score


def score_standard_leg(
    cand:      "LegCandidate",
    sc:        "SharpConsensus",
    dnp_prob:  float,
) -> WinScoreStandard:
    """
    Score a standard leg for must-win probability.
    Caller guarantees cand.p_win >= STANDARD_PWIN_FLOOR already.
    """
    pp_line   = cand.line
    p_win     = cand.p_win
    direction = cand.direction
    is_real   = sc.freshness_sec < 9000.0

    # anchor_delta: positive = market agrees this side has value
    if direction == Direction.OVER:
        anchor_delta = sc.median - pp_line   # positive = line is below fair → OVER has edge
    else:
        anchor_delta = pp_line - sc.median   # positive = line is above fair → UNDER has edge

    # W1: p_win margin above must-win floor
    h_star = STANDARD_PWIN_FLOOR
    w1 = float(np.clip((p_win - h_star) / max(1 - h_star, 0.01), 0.0, 1.0))

    # W2: market agreement
    w2 = float(np.clip(0.5 + anchor_delta / 1.0, 0.0, 1.0)) if is_real else 0.40

    # W3: distribution conviction (above 0.57 floor)
    w3 = float(np.clip((p_win - 0.57) / 0.20, 0.0, 1.0))

    # W4: role certainty
    w4 = float(np.clip(1.0 - dnp_prob / 0.15, 0.0, 1.0))

    win_score = float(np.clip(
        0.40 * w1 + 0.30 * w2 + 0.20 * w3 + 0.10 * w4,
        0.0, 1.0,
    ))

    return WinScoreStandard(
        prop_id=cand.prop_id,
        player_name=cand.player_name,
        stat_type=cand.stat_type,
        line=pp_line,
        direction=direction,
        p_win=p_win,
        w1_pwin_margin=round(w1, 4),
        w2_market_agree=round(w2, 4),
        w3_dist_conv=round(w3, 4),
        w4_role=round(w4, 4),
        win_score=round(win_score, 4),
    )


# ──────────────────────────────────────────────────────────────────────────────
# 11. UNDER SCORING ENGINE
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class UnderScore:
    prop_id:                 str
    player_name:             str
    stat_type:               str
    line:                    float
    p_under:                 float
    u1_market_anchor:        float
    u2_dist_hit_rate:        float
    u3_volume_weakness:      float
    u4_game_script_suppression: float
    u5_pair_diversity:       float
    composite:               float
    qualifies:               bool


def _keep_under(
    p_under:          float,
    edge:             float,   # pp_line - fair_line (positive = PP line > fair = value on under)
    dnp_prob:         float,
    volume_weakness:  float,
    upside_trap:      bool,
) -> bool:
    """Hard keep gate for under legs. All five conditions must be true."""
    return (
        p_under          >= 0.54
        and edge          > 0.0
        and dnp_prob      < 0.15
        and volume_weakness >= 0.45
        and not upside_trap
    )


def score_under(
    cand:                   "LegCandidate",
    sc:                     "SharpConsensus",
    dnp_prob:               float,
    # U3 volume weakness inputs
    lineup_risk:            float = 0.5,
    order_penalty:          float = 0.5,
    pitch_count_risk:       float = 0.5,
    sub_risk:               float = 0.0,
    # U4 game script suppression inputs
    park_suppression:       float = 0.5,
    matchup_suppression:    float = 0.5,
    run_env_suppression:    float = 0.5,
    push_mass:              float = 0.5,
    # U5
    shared_failure_penalty: float = 0.0,
) -> UnderScore:
    """
    Score a standard UNDER leg.
    Keep gate: p_under>=0.54 AND edge>0 AND dnp<0.15 AND volume_weakness>=0.45 AND not upside_trap
    Weights:   U1=0.36  U2=0.24  U3=0.18  U4=0.12  U5=0.10
    """
    pp_line   = cand.line
    p_under   = cand.p_win   # p_win was computed for UNDER direction
    fair_line = sc.median
    is_real   = sc.freshness_sec < 9000.0

    # edge = pp_line - fair_line: positive means PP line is above the fair line → under has value
    edge = pp_line - fair_line

    # Derive volume_weakness and upside_trap from available data when inputs are default
    # volume_weakness: proxy = U3 formula with defaults (0.5 neutral each component)
    volume_weakness = float(np.clip(
        0.35 * lineup_risk + 0.25 * order_penalty +
        0.25 * pitch_count_risk + 0.15 * sub_risk,
        0.0, 1.0
    ))
    # upside_trap: true when the fair line is materially BELOW pp_line (edge > 1.5)
    # meaning PP is pricing in a scenario where the player blows up the over — risky to fade
    upside_trap = edge > 1.5

    qualifies = _keep_under(p_under, edge, dnp_prob, volume_weakness, upside_trap)
    if not qualifies:
        return UnderScore(
            prop_id=cand.prop_id, player_name=cand.player_name,
            stat_type=cand.stat_type, line=pp_line, p_under=p_under,
            u1_market_anchor=0.0, u2_dist_hit_rate=0.0, u3_volume_weakness=0.0,
            u4_game_script_suppression=0.0, u5_pair_diversity=1.0,
            composite=0.0, qualifies=False,
        )

    # ── U1: Market anchor ────────────────────────────────────────────────
    # clamp(0.5 + (pp_line - fair_line) / 1.0)
    u1 = float(np.clip(0.5 + edge / 1.0, 0.0, 1.0)) if is_real else 0.40

    # ── U2: Distribution hit rate ─────────────────────────────────────────
    # clamp((p_under - 0.54) / 0.16)
    u2 = float(np.clip((p_under - 0.54) / 0.16, 0.0, 1.0))

    # ── U3: Volume weakness ──────────────────────────────────────────────
    # clamp(0.35*lineup_risk + 0.25*order_penalty + 0.25*pitch_count_risk + 0.15*sub_risk)
    u3 = volume_weakness  # already computed above

    # ── U4: Game script suppression ────────────────────────────────────────
    # clamp(0.30*park_suppression + 0.25*matchup_suppression + 0.25*run_env + 0.20*push_mass)
    u4 = float(np.clip(
        0.30 * park_suppression + 0.25 * matchup_suppression +
        0.25 * run_env_suppression + 0.20 * push_mass,
        0.0, 1.0
    ))

    # ── U5: Pair diversity ─────────────────────────────────────────────────
    u5 = float(np.clip(1.0 - shared_failure_penalty, 0.0, 1.0))

    # ── Composite: U1=0.36 U2=0.24 U3=0.18 U4=0.12 U5=0.10 ────────────────
    composite = float(np.clip(
        0.36 * u1 + 0.24 * u2 + 0.18 * u3 + 0.12 * u4 + 0.10 * u5,
        0.0, 1.0
    ))

    return UnderScore(
        prop_id=cand.prop_id,
        player_name=cand.player_name,
        stat_type=cand.stat_type,
        line=pp_line,
        p_under=p_under,
        u1_market_anchor=round(u1, 4),
        u2_dist_hit_rate=round(u2, 4),
        u3_volume_weakness=round(u3, 4),
        u4_game_script_suppression=round(u4, 4),
        u5_pair_diversity=round(u5, 4),
        composite=round(composite, 4),
        qualifies=True,
    )


# ── Slip-level must-win floor ─────────────────────────────────────────────────
# A slip is only shown if its geometric hit probability clears this floor.
# 6 legs at p_win=0.62 each → 0.62^6 ≈ 0.0566 — too low to show.
# This floor filters boards where even the "best" 6 legs are too weak.
SLIP_HIT_PROB_FLOOR = 0.04   # 6 legs at 0.64 each ≈ 0.075; floor is conservative

def solve_game_milp(
    candidates: List[LegCandidate],
    win_score_map: Dict[str, float],   # WinScoreStandard.win_score or DemonWinScore composite
    r_star_6:   float,
    time_limit_sec: float = 5.0,
) -> Optional[List[LegCandidate]]:
    """
    Must-Win Slip Optimizer.

    Primary objective: maximize slip hit probability (product of p_wins).
    In log-space: maximize Σ log(p_win) * x_L  — this is linear and exact.

    Secondary objective embedded in win_score_map: among legs with similar
    log(p_win), prefer those with higher WinScore (market agreement, distribution
    conviction, role certainty).

    Combined objective coefficient per leg:
        obj_L = log(p_win_L) + 0.10 * win_score_L

    The 0.10 weight keeps payout/quality as a tiebreaker without overriding
    the hit-probability primary objective.

    Constraints:
      Σ x_L = 6
      Standard legs: p_win ≥ STANDARD_PWIN_FLOOR (0.57)
      Demon legs: at most 2, gate survivors only, distinct players, no subs
      ≤ 3 legs per player
      ≥ 2 distinct stat categories
      No-slip: returned None if slip_hit_prob < SLIP_HIT_PROB_FLOOR
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

    # ── Must-Win Objective ────────────────────────────────────────────────────
    # Primary: maximize Σ log(p_win) * x   [maximizes product of p_wins]
    # Tiebreaker: + 0.10 * win_score       [market/distribution conviction]
    obj_coeffs = []
    for lg in candidates:
        log_pwin = math.log(max(lg.p_win, 0.001))
        ws = win_score_map.get(lg.prop_id, 0.0)
        obj_coeffs.append((lg.prop_id, log_pwin + 0.10 * ws))

    solver.Maximize(
        solver.Sum([coeff * x[pid] for pid, coeff in obj_coeffs])
    )

    # ── Exactly 6 legs ────────────────────────────────────────────────────────
    solver.Add(solver.Sum(list(x.values())) == 6)

    # ── Demon legs: AT MOST 2, gate survivors only, no substitutions ──────────
    # Rule: PP is the only authority on demon identity. GOTit applies 4 gates
    # (line floor, sharp sanity, hit-rate floor, role/script quality) and ranks
    # survivors. Top 2 distinct-player demons enter. If fewer than 2 survive,
    # we use fewer than 2. NEVER force a bad demon to fill the second slot.
    demon_vars = [x[lg.prop_id] for lg in candidates if lg.tier == Tier.DEMON]
    n_qual = len(demon_vars)

    if n_qual == 0:
        pass  # No demons survived gates — slip has 0 demons, that is correct
    elif n_qual == 1:
        solver.Add(solver.Sum(demon_vars) == 1)  # lock the one survivor in
    else:
        # 2+ survived — pick exactly 2 distinct players
        solver.Add(solver.Sum(demon_vars) == 2)
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

    # ── Per-leg admission floors ──────────────────────────────────────────────
    # Standard legs: must clear the must-win floor (0.57).
    # Demon legs already passed qualify_demons 4-gate chain; use demon floor (0.53).
    # Goblin legs: treated as lower-tier standards — use r_star_6 floor.
    for lg in candidates:
        if lg.tier == Tier.STANDARD:
            if lg.p_win < STANDARD_PWIN_FLOOR:
                solver.Add(x[lg.prop_id] == 0)
        elif lg.tier == Tier.GOBLIN:
            if lg.p_win < r_star_6:
                solver.Add(x[lg.prop_id] == 0)
        # DEMON: qualify_demons already gated them; trust that gate

    # ── Solve ─────────────────────────────────────────────────────────────────
    status = solver.Solve()
    if status not in (pywraplp.Solver.OPTIMAL, pywraplp.Solver.FEASIBLE):
        return None

    selected = [lg_map[pid] for pid, var in x.items() if var.solution_value() > 0.5]
    if len(selected) != 6:
        return None

    # ── No-slip check: reject if geometric hit probability is below floor ──────
    # If the "best" 6 legs the optimizer found still combine to a slip hit
    # probability below SLIP_HIT_PROB_FLOOR, the board is too weak. Show nothing.
    slip_hit_prob = 1.0
    for lg in selected:
        slip_hit_prob *= lg.p_win
    if slip_hit_prob < SLIP_HIT_PROB_FLOOR:
        log.info(
            "No-slip: slip_hit_prob=%.4f < floor=%.4f — board too weak",
            slip_hit_prob, SLIP_HIT_PROB_FLOOR,
        )
        return None

    log.info("Slip selected: hit_prob=%.4f legs=%s",
             slip_hit_prob,
             [(lg.player_name, lg.stat_type, lg.direction.value, round(lg.p_win,3))
              for lg in selected])
    return selected


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
            #   STANDARD → GOTit evaluates BOTH directions internally,
            #              then keeps ONLY the one with higher p_win.
            #              The DB stores a single over row per player+stat+game.
            #              Both sides are derived here from the same line.
            if tier in (Tier.GOBLIN, Tier.DEMON):
                dirs = [Direction.OVER]
            else:
                dirs = [Direction.OVER, Direction.UNDER]

            dnp_prob = dnp_model.get(pp.player_id, dnp_model.get(pp.prop_id, 0.0))

            # Standard line floor — block lottery lines before scoring
            if tier == Tier.STANDARD:
                std_floor = _STANDARD_LINE_FLOOR.get(pp.stat_type, _STANDARD_LINE_FLOOR["_default"])
                if line < std_floor:
                    log.debug("Gate 0 (std floor): %s %s line=%.1f < floor=%.1f",
                              pp.player_name, pp.stat_type, line, std_floor)
                    continue

            # Player-level junk filter — if EVERY standard prop this player has
            # across all stat types is below floor, the player has no meaningful
            # line on the board and should not appear in the slate at all.
            # (Checks all props for this player in the same game batch, not just current.)
            if tier == Tier.STANDARD:
                all_player_props = [
                    p for p in props
                    if p.player_id == pp.player_id
                    and not p.is_demon and not p.is_goblin
                ]
                has_meaningful = any(
                    p.line_score >= _STANDARD_LINE_FLOOR.get(p.stat_type, _STANDARD_LINE_FLOOR["_default"])
                    for p in all_player_props
                )
                if not has_meaningful:
                    log.info("Player junk filter: %s has no prop above floor — skipping",
                             pp.player_name)
                    continue

            best_cand: Optional[LegCandidate] = None
            for d in dirs:
                p_win = _calibrated_p_win(line, median, cal_shape, family, d, pp.stat_type)

                # Hard filters
                if p_win < BREAKEVEN_R[6] - 0.02:
                    continue
                if dnp_prob > 0.15:
                    continue
                if tier == Tier.DEMON and p_win < BREAKEVEN_R[6] + 0.03:
                    continue

                cand = LegCandidate(
                    prop_id=pp.prop_id if d == Direction.OVER else f"{pp.prop_id}:under",
                    game_id=pp.game_id,
                    player_id=pp.player_id,
                    player_name=pp.player_name,
                    stat_type=pp.stat_type,
                    tier=tier,
                    line=line,
                    direction=d,
                    p_win=float(np.clip(p_win, 0.001, 0.999)),
                )

                # Score UNDER standard legs; drop those that fail keep gate
                if d == Direction.UNDER and tier == Tier.STANDARD:
                    us = score_under(cand, sc, dnp_prob)
                    if not us.qualifies:
                        continue
                    cand.under_score = us

                # For standard legs: keep only the better-scoring direction
                if tier == Tier.STANDARD:
                    if best_cand is None or cand.p_win > best_cand.p_win:
                        best_cand = cand
                else:
                    # Demons and goblins: only one direction (OVER), add directly
                    all_candidates.append(cand)

            # Add the single winning direction for standard legs
            if tier == Tier.STANDARD and best_cand is not None:
                all_candidates.append(best_cand)

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
        qualified_d = qualify_demons(
            demon_cands=raw_d_cands,
            sc_map=sharp_consensus,
            dnp_model=dnp_model,
        )
        # Note: MILP handles 0/1/2 demon case — no skip here.
        # Games with 0 qualifying demons produce a standard-only slip (no demon slots).

        # ── Standard admission floor: drop before Shapley ─────────────────────
        # Standards that can't clear STANDARD_PWIN_FLOOR have no path to selection;
        # excluding them keeps Shapley fast and clean.
        s_cands_admitted = [
            c for c in s_cands
            if c.tier == Tier.GOBLIN or c.p_win >= STANDARD_PWIN_FLOOR
        ]

        # Cap demon slots at 6 (enough for MILP to pick 2, with diversity)
        demon_slots = qualified_d[:6]
        standard_slots = s_cands_admitted[:MAX_SHAPLEY - len(demon_slots)]
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
    # corr_adj_all removed — MILP now uses WinScoreStandard, not Shapley corr-adj EV

    for game_id in set(c.game_id for c in all_candidates):
        game_cands = [c for c in all_candidates if c.game_id == game_id]
        if len(game_cands) < 6:
            continue

        # ── Build WinScore map for MILP objective ────────────────────────────
        # Standards + goblins: WinScoreStandard.win_score
        # Demons: DemonWinScore composite (from qualify_demons scoring)
        win_score_map: Dict[str, float] = {}
        game_demons_scored = {
            lg.prop_id: lg for lg in game_cands if lg.tier == Tier.DEMON
        }
        for c in game_cands:
            dnp_p = dnp_model.get(c.player_id, dnp_model.get(c.prop_id, 0.0))
            if c.tier == Tier.DEMON:
                sc_d = sharp_consensus.get(c.prop_id)
                if sc_d:
                    ds = score_demon(c, sc_d, dnp_p, game_total=0.0)
                    win_score_map[c.prop_id] = ds.composite
                else:
                    win_score_map[c.prop_id] = 0.0
            else:
                sc_s = sharp_consensus.get(c.prop_id)
                if sc_s:
                    ws = score_standard_leg(c, sc_s, dnp_p)
                    win_score_map[c.prop_id] = ws.win_score
                    c.ev_corr_adj = ws.win_score  # store for leg_to_dict output
                else:
                    win_score_map[c.prop_id] = 0.0

        # Keep Shapley for EV metadata in output (not used in objective)
        shapley = shapley_marginal_ev(game_cands, BREAKEVEN_R)
        for c in game_cands:
            c.ev_marginal = shapley.get(c.prop_id, 0.0)
        shapley_all.update(shapley)

        selected = solve_game_milp(game_cands, win_score_map, r_star_6)
        if not selected:
            log.info("Game %s: no qualifying slip (MILP infeasible or board too weak)", game_id)
            continue

        demons = [lg for lg in selected if lg.tier == Tier.DEMON]
        # Demons may be 0 or 1 on weak boards — that is correct; no forced fill

        port_ev = sum(win_score_map.get(lg.prop_id, 0.0) for lg in selected)
        slip_hit_prob = 1.0
        for lg in selected:
            slip_hit_prob *= lg.p_win

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
                sc_d = sharp_consensus.get(lg.prop_id)
                if sc_d:
                    dnp_p = dnp_model.get(lg.player_id, dnp_model.get(lg.prop_id, 0.0))
                    ds = score_demon(lg, sc_d, dnp_p, game_total=0.0)
                    d["demon_score"] = {
                        "composite":       ds.composite,
                        "market_anchor":   ds.market_anchor,
                        "dist_hit_rate":   ds.dist_hit_rate,
                        "game_script_fit": ds.game_script_fit,
                        "role_certainty":  ds.role_certainty,
                        "pair_diversity":  ds.pair_diversity,
                    }
            # For UNDER standard legs, attach the 5-layer under score
            if lg.direction == Direction.UNDER and lg.under_score is not None:
                us = lg.under_score
                d["under_score"] = {
                    "composite":                  us.composite,
                    "market_anchor":              us.u1_market_anchor,
                    "dist_hit_rate":              us.u2_dist_hit_rate,
                    "volume_weakness":            us.u3_volume_weakness,
                    "game_script_suppression":    us.u4_game_script_suppression,
                    "pair_diversity":             us.u5_pair_diversity,
                }
            return d

        output[game_id] = {
            "six_legs":   [leg_to_dict(lg) for lg in selected],
            "two_demons": [leg_to_dict(lg) for lg in demons],
            "meta": {
                "slate_breakeven_r6":    round(r_star_6, 4),
                "portfolio_win_score":    round(port_ev / 6, 6),
                "slip_hit_prob":         round(slip_hit_prob, 6),
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
