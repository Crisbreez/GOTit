#!/usr/bin/env python3
"""
GOTit Engine v1
- Score PrizePicks-style legs vs fair P and slip break-even
- Demontime: always return top 2 demons (PASS or closest)
- Slip builder: max EV under constraints
- Bankroll sizing + results log hooks

NOT financial advice. No guarantees. Edges are estimates.
"""

from __future__ import annotations

import json
import math
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple


# =============================================================================
# Config
# =============================================================================

@dataclass
class GotitConfig:
    market_weight: float = 0.60
    proj_weight: float = 0.40
    w_p_true: float = 0.35
    w_p_edge: float = 0.25
    w_gap: float = 0.15
    w_bump: float = 0.10
    w_boost: float = 0.10
    w_role: float = 0.03
    w_ctx: float = 0.02
    p_edge_min_pass: float = 0.00
    role_min_pass: float = 0.70
    close_band_p: float = 0.08
    correlation_max: float = 0.65
    demontime_count: int = 2
    min_legs: int = 2
    max_legs: int = 6
    max_demons_per_slip: int = 2
    min_leg_p_true: float = 0.42
    min_avg_p_edge: float = -0.01
    prefer_flex_if_pair_p_below: float = 0.32
    lottery_stats: Tuple[str, ...] = ("HR", "HRs", "HR+", "pitcher_win")
    lottery_penalty: float = 0.05
    lottery_elite_gap: float = 0.35
    bankroll: float = 1000.0
    max_stake_pct: float = 0.02
    kelly_fraction: float = 0.25
    min_stake: float = 5.0
    max_stake: float = 50.0
    power_be: Dict[int, float] = field(default_factory=lambda: {
        2: 0.577, 3: 0.55, 4: 0.52, 5: 0.50, 6: 0.48
    })
    flex_be_delta: float = -0.04
    demon_be_relief: float = 0.012
    goblin_be_tax: float = 0.015
    # Demontime misprice scoring — PP alt-line fixed-multiplier edge
    w_misprice: float = 0.12          # weight in Demontime final score
    misprice_bump_sweet_spot: float = 0.5  # ideal bump size (±0.5 = full credit)
    misprice_bump_max: float = 2.0    # bump > this → edge collapses
    misprice_mult_jump_min: float = 2.0    # multiplier jump must be >= this to count
    misprice_p_true_min: float = 0.50      # books/model must still like More at demon line


class Tier(str, Enum):
    PASS = "PASS"
    CLOSE = "CLOSE"
    STRETCH = "STRETCH"


class Mode(str, Enum):
    EXACT_PAIR = "EXACT_PAIR"
    MIXED = "MIXED"
    CLOSEST_AVAILABLE = "CLOSEST_AVAILABLE"
    INSUFFICIENT_INVENTORY = "INSUFFICIENT_INVENTORY"
    NO_DEMONS = "NO_DEMONS"


class SlipType(str, Enum):
    POWER = "POWER"
    FLEX = "FLEX"


# =============================================================================
# Data models
# =============================================================================

@dataclass
class MarketQuote:
    line: float
    over_american: int
    under_american: int
    book: str = "sharp"


@dataclass
class RawLeg:
    leg_id: str
    game_id: str
    player_id: str
    player_name: str
    team: str
    opponent: str
    stat_type: str
    line: float
    side: str
    is_demon: bool = False
    is_goblin: bool = False
    standard_line: Optional[float] = None
    proj_mean: Optional[float] = None
    market: Optional[MarketQuote] = None
    role_score: float = 0.75
    ctx_score: float = 0.50
    boost_score: float = 0.50
    correlation_keys: List[str] = field(default_factory=list)
    killed: bool = False
    kill_reason: str = ""
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ScoredLeg:
    raw: RawLeg
    p_market: Optional[float]
    p_proj: Optional[float]
    p_true: float
    p_need: float
    p_edge: float
    gap: float
    bump: Optional[float]
    bump_quality: float
    final_score: float
    tier: Tier
    flags: List[str] = field(default_factory=list)
    why: str = ""


@dataclass
class DemontimeResult:
    game_id: str
    mode: Mode
    demons: List[ScoredLeg]
    rejected_top: List[Dict[str, Any]]
    warnings: List[str]
    generated_at: float


@dataclass
class SlipLeg:
    scored: ScoredLeg
    side: str


@dataclass
class SlipPlan:
    slip_id: str
    slip_type: SlipType
    legs: List[SlipLeg]
    n: int
    n_demons: int
    p_each: List[float]
    p_all_hit: float
    avg_p_true: float
    avg_p_edge: float
    est_multiplier: float
    est_ev_per_dollar: float
    stake: float
    warnings: List[str]
    thesis: str


# =============================================================================
# Math helpers
# =============================================================================

def american_to_implied(american: int) -> float:
    if american == 0:
        raise ValueError("invalid american odds")
    if american > 0:
        return 100.0 / (american + 100.0)
    return (-american) / ((-american) + 100.0)


def devig_two_way(over_american: int, under_american: int) -> Tuple[float, float]:
    io = american_to_implied(over_american)
    iu = american_to_implied(under_american)
    s = io + iu
    if s <= 0:
        raise ValueError("bad implied sum")
    return io / s, iu / s


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def tanh_norm(x: float, scale: float = 1.0) -> float:
    return math.tanh(x / scale)


def approx_p_from_mean_line(mean: float, line: float, side: str, sigma: float = 1.0) -> float:
    z = (mean - line) / max(sigma, 1e-6)
    p_over = 1.0 / (1.0 + math.exp(-1.7 * z))
    return p_over if side.upper() == "MORE" else 1.0 - p_over


def independence_joint(ps: Sequence[float]) -> float:
    j = 1.0
    for p in ps:
        j *= clamp(p, 1e-6, 1.0 - 1e-6)
    return j


# =============================================================================
# Break-even & multiplier
# =============================================================================

def estimate_p_need(n_legs: int, slip_type: SlipType, n_demons: int, n_goblins: int, cfg: GotitConfig) -> float:
    n = clamp(int(n_legs), 2, 6)
    base = cfg.power_be.get(n, 0.52)
    if slip_type == SlipType.FLEX:
        base += cfg.flex_be_delta
    base -= cfg.demon_be_relief * min(n_demons, 3)
    base += cfg.goblin_be_tax * min(n_goblins, 3)
    return clamp(base, 0.34, 0.70)


def estimate_multiplier(n_legs: int, slip_type: SlipType, n_demons: int, n_goblins: int) -> float:
    power = {2: 3.0, 3: 5.0, 4: 10.0, 5: 20.0, 6: 37.5}
    flex_all = {3: 2.25, 4: 5.0, 5: 10.0, 6: 25.0}
    n = int(clamp(n_legs, 2, 6))
    if slip_type == SlipType.POWER:
        m = power.get(n, 10.0)
    else:
        m = flex_all.get(n, 5.0) if n >= 3 else power.get(n, 3.0) * 0.75
    m *= (1.0 + 0.15 * n_demons)
    m *= max(0.5, 1.0 - 0.12 * n_goblins)
    return round(m, 4)


# =============================================================================
# Core scoring
# =============================================================================

def compute_p_true(leg: RawLeg, cfg: GotitConfig) -> Tuple[float, Optional[float], Optional[float], List[str]]:
    flags: List[str] = []
    p_m: Optional[float] = None
    p_p: Optional[float] = None

    if leg.market is not None:
        try:
            p_over, p_under = devig_two_way(leg.market.over_american, leg.market.under_american)
            if abs(leg.market.line - leg.line) < 1e-9:
                p_m = p_over if leg.side.upper() == "MORE" else p_under
            else:
                delta = leg.line - leg.market.line
                adj = tanh_norm(delta, 1.0) * 0.12
                base = p_over if leg.side.upper() == "MORE" else p_under
                p_m = clamp(base - adj if leg.side.upper() == "MORE" else base + adj, 0.02, 0.98)
                flags.append("market_line_adjusted")
        except Exception:
            flags.append("market_parse_fail")

    if leg.proj_mean is not None:
        sigma = 1.15 if leg.stat_type.upper() in cfg.lottery_stats else 1.0
        p_p = approx_p_from_mean_line(leg.proj_mean, leg.line, leg.side, sigma=sigma)

    if p_m is not None and p_p is not None:
        p = cfg.market_weight * p_m + cfg.proj_weight * p_p
        flags.append("blend_market_proj")
    elif p_m is not None:
        p = p_m
        flags.append("market_only")
    elif p_p is not None:
        p = p_p
        flags.append("proj_only")
    else:
        p = 0.45
        flags.append("prior_only_weak")

    return clamp(p, 0.01, 0.99), p_m, p_p, flags


def misprice_score(leg: RawLeg, p_true: float, cfg: GotitConfig) -> float:
    """
    PP Demon alt-line misprice edge scorer.

    PrizePicks pairs a harder More line with a fixed multiplier boost.
    The edge is:
      • line only moved a little (+0.5-ish from standard)  →  difficulty barely up
      • multiplier jump is large                            →  payout disproportionate
      • books/model still like More at that number         →  p_true >= threshold

    Returns 0.0–1.0.  0.0 = no misprice edge; 1.0 = peak misprice.
    """
    if leg.standard_line is None:
        return 0.0

    bump = leg.line - leg.standard_line  # how much harder the demon line is
    if bump <= 0:
        return 0.0  # line didn't actually move up — not a demon alt-line scenario

    # ── Bump-size score: sweet spot near +0.5, falls off for larger bumps ──
    sweet  = cfg.misprice_bump_sweet_spot  # 0.5
    b_max  = cfg.misprice_bump_max         # 2.0
    if bump <= sweet:
        # Small bump — very little extra difficulty, near-full credit
        bump_score = bump / sweet
    else:
        # Larger bump — difficulty increases faster than payout scales
        bump_score = clamp(1.0 - (bump - sweet) / (b_max - sweet), 0.0, 1.0)

    # ── Multiplier-jump score: large jump = large edge ──────────────────────
    # Estimate multiplier jump from demon boost_score field (0–1 proxy)
    # boost_score=1.0 ≈ max demon multiplier; boost_score=0 ≈ no boost
    # Also use leg.meta if it carries an explicit mult_jump
    mult_jump = float(leg.meta.get('mult_jump', 0) or 0)
    if mult_jump == 0:
        # Fall back to boost_score as a proxy for mult jump magnitude
        # boost_score maps linearly to implied mult jump 0→ min, 1→ ~5x
        mult_jump = leg.boost_score * 5.0
    min_jump = cfg.misprice_mult_jump_min  # 2.0
    mult_score = clamp((mult_jump - min_jump) / 3.0, 0.0, 1.0)

    # ── Market-still-likes-More gate ─────────────────────────────────────────
    p_min = cfg.misprice_p_true_min  # 0.50
    if p_true < p_min:
        return 0.0  # books don't like More at demon line — no misprice edge

    # Slight boost if p_true well clear of line
    p_confidence = clamp((p_true - p_min) / 0.15, 0.0, 1.0)

    # ── Composite ─────────────────────────────────────────────────────────────
    score = 0.45 * bump_score + 0.35 * mult_score + 0.20 * p_confidence
    return clamp(score, 0.0, 1.0)


def bump_quality(leg: RawLeg) -> Tuple[Optional[float], float]:
    if leg.standard_line is None:
        return None, 0.45
    bump = leg.line - leg.standard_line
    q = clamp(1.0 - (bump / 2.0), 0.0, 1.0) if leg.side.upper() == "MORE" else 0.5
    return bump, q


def assign_tier(leg: RawLeg, p_edge: float, cfg: GotitConfig) -> Tier:
    if leg.killed:
        return Tier.STRETCH
    if p_edge >= cfg.p_edge_min_pass and leg.role_score >= cfg.role_min_pass:
        return Tier.PASS
    if p_edge >= -cfg.close_band_p:
        return Tier.CLOSE
    return Tier.STRETCH


def score_leg(leg: RawLeg, p_need: float, cfg: GotitConfig) -> ScoredLeg:
    p_true, p_m, p_p, flags = compute_p_true(leg, cfg)
    # Use canonical p_hit_formula result when available (stored on meta by prop_to_raw_leg)
    _p_hit_meta = leg.meta.get("p_hit") if leg.meta else None
    if _p_hit_meta is not None:
        p_true = clamp(float(_p_hit_meta), 0.01, 0.99)
        flags.append(f"p_hit_formula={p_true:.3f}")
    if leg.killed:
        flags.append(f"killed:{leg.kill_reason or 'unknown'}")

    gap = 0.0
    if leg.proj_mean is not None:
        gap = (leg.proj_mean - leg.line) if leg.side.upper() == "MORE" else (leg.line - leg.proj_mean)

    bump, bq = bump_quality(leg)
    p_edge = p_true - p_need

    lottery = leg.stat_type.upper() in {s.upper() for s in cfg.lottery_stats}
    lot_pen = cfg.lottery_penalty if lottery and gap < cfg.lottery_elite_gap else 0.0
    if lot_pen:
        flags.append("lottery_penalty")

    kill_pen = 0.25 if leg.killed else 0.0

    # Misprice edge — Demontime only: alt-line bump vs multiplier-jump vs p_true
    misprice = 0.0
    misprice_flag = ""
    if leg.is_demon:
        misprice = misprice_score(leg, p_true, cfg)
        if misprice >= 0.6:
            misprice_flag = f"misprice_edge={misprice:.2f}"
            flags.append(misprice_flag)
        elif misprice >= 0.3:
            flags.append(f"misprice_moderate={misprice:.2f}")

    # Adjust w_bump for demons: standard bump_quality captures difficulty only;
    # replace with misprice when demon to avoid double-counting bump penalty
    w_bump_eff = 0.0 if leg.is_demon else cfg.w_bump
    w_misprice_eff = cfg.w_misprice if leg.is_demon else 0.0

    s = (
        cfg.w_p_true * p_true
        + cfg.w_p_edge * clamp(p_edge, -0.25, 0.25)
        + cfg.w_gap * tanh_norm(gap, 1.0)
        + w_bump_eff * bq
        + w_misprice_eff * misprice
        + cfg.w_boost * clamp(leg.boost_score, 0.0, 1.0)
        + cfg.w_role * clamp(leg.role_score, 0.0, 1.0)
        + cfg.w_ctx * clamp(leg.ctx_score, 0.0, 1.0)
        - lot_pen - kill_pen
    )

    tier = assign_tier(leg, p_edge, cfg)
    why = (
        f"p_true={p_true:.3f}; need={p_need:.3f}; edge={p_edge:+.3f}; "
        f"gap={gap:+.2f}; role={leg.role_score:.2f}"
        + (f"; bump={bump:+.2f}" if bump is not None else "")
        + (f"; misprice={misprice:.2f}" if leg.is_demon else "")
        + ("; DEMON" if leg.is_demon else "")
    )

    return ScoredLeg(
        raw=leg, p_market=p_m, p_proj=p_p, p_true=p_true, p_need=p_need,
        p_edge=p_edge, gap=gap, bump=bump, bump_quality=bq,
        final_score=s, tier=tier, flags=flags, why=why,
    )


# =============================================================================
# Correlation
# =============================================================================

def pair_correlation(a: ScoredLeg, b: ScoredLeg) -> float:
    ka = set(a.raw.correlation_keys)
    kb = set(b.raw.correlation_keys)
    if not ka or not kb:
        return 0.35 if a.raw.game_id == b.raw.game_id else 0.05
    inter = len(ka & kb)
    union = len(ka | kb)
    jacc = inter / union if union else 0.0
    same_game = 0.2 if a.raw.game_id == b.raw.game_id else 0.0
    return clamp(jacc + same_game, 0.0, 1.0)


def pick_pair(ranked: List[ScoredLeg], cfg: GotitConfig) -> Tuple[Optional[ScoredLeg], Optional[ScoredLeg], List[str]]:
    warnings: List[str] = []
    if not ranked:
        return None, None, warnings
    if len(ranked) == 1:
        return ranked[0], None, warnings
    a = ranked[0]
    for cand in ranked[1:]:
        if pair_correlation(a, cand) <= cfg.correlation_max:
            return a, cand, warnings
    warnings.append("pair_high_correlation")
    return a, ranked[1], warnings


# =============================================================================
# Demontime
# =============================================================================

# MLB allowlist — only these stats enter Demontime for MLB
MLB_DEMON_ALLOWLIST = {"Total Bases", "Hits+Runs+RBIs", "Hitter Fantasy Score", "Singles"}
MLB_SINGLES_MAX_LINE = 0.5


def _mlb_demon_allowed(leg: RawLeg) -> bool:
    sport = str(leg.meta.get("league", leg.meta.get("sport", "")) or "").upper()
    if sport != "MLB":
        return True  # non-MLB: no allowlist
    if leg.stat_type not in MLB_DEMON_ALLOWLIST:
        return False
    if leg.stat_type == "Singles" and leg.line > MLB_SINGLES_MAX_LINE:
        return False
    return True


def demontime_for_game(
    legs: Sequence[RawLeg],
    game_id: str,
    cfg: GotitConfig,
    slip_type: SlipType = SlipType.POWER,
    assumed_n_legs: int = 5,
) -> DemontimeResult:
    demons_raw = [
        L for L in legs
        if L.game_id == game_id
        and L.is_demon
        and L.side.upper() == "MORE"
        and _mlb_demon_allowed(L)
    ]
    ts = time.time()

    if not demons_raw:
        return DemontimeResult(game_id, Mode.NO_DEMONS, [], [], [], ts)

    n_dem = min(cfg.demontime_count, len(demons_raw))
    p_need = estimate_p_need(assumed_n_legs, slip_type, n_demons=n_dem, n_goblins=0, cfg=cfg)

    scored = [score_leg(d, p_need, cfg) for d in demons_raw]
    scored.sort(key=lambda x: x.final_score, reverse=True)

    if len(scored) == 1:
        return DemontimeResult(game_id, Mode.INSUFFICIENT_INVENTORY, scored[:1], [], ["only_one_demon"], ts)

    a, b, warn = pick_pair(scored, cfg)
    assert a and b
    chosen = [a, b]

    tiers = {a.tier, b.tier}
    mode = Mode.EXACT_PAIR if tiers == {Tier.PASS} else (Mode.MIXED if Tier.PASS in tiers else Mode.CLOSEST_AVAILABLE)

    rejected = []
    for s in scored:
        if s is a or s is b:
            continue
        if len(rejected) >= 3:
            break
        rejected.append({
            "player": s.raw.player_name, "stat": s.raw.stat_type,
            "line": s.raw.line, "score": round(s.final_score, 4),
            "tier": s.tier.value, "reason": "below_top2_or_corr",
        })

    warn = list(warn) + ["payout_verify_on_submit"]
    if any(x.tier == Tier.STRETCH for x in chosen):
        warn.append("includes_stretch_tier")

    return DemontimeResult(game_id=game_id, mode=mode, demons=chosen,
                           rejected_top=rejected, warnings=warn, generated_at=ts)


def demontime_slate(legs: Sequence[RawLeg], cfg: GotitConfig) -> List[DemontimeResult]:
    games = sorted({L.game_id for L in legs if L.is_demon})
    return [demontime_for_game(legs, g, cfg) for g in games]


# =============================================================================
# Slip optimizer
# =============================================================================

def _slip_ok(scored_legs: List[ScoredLeg], cfg: GotitConfig) -> bool:
    if not (cfg.min_legs <= len(scored_legs) <= cfg.max_legs):
        return False
    if any(s.p_true < cfg.min_leg_p_true for s in scored_legs):
        return False
    if sum(1 for s in scored_legs if s.raw.is_demon) > cfg.max_demons_per_slip:
        return False
    avg_edge = sum(s.p_edge for s in scored_legs) / len(scored_legs)
    return avg_edge >= cfg.min_avg_p_edge


def build_candidate_slip(legs: List[ScoredLeg], slip_type: SlipType, cfg: GotitConfig) -> Optional[SlipPlan]:
    if not _slip_ok(legs, cfg):
        return None
    n = len(legs)
    n_dem = sum(1 for s in legs if s.raw.is_demon)
    n_gob = sum(1 for s in legs if s.raw.is_goblin)
    p_need = estimate_p_need(n, slip_type, n_dem, n_gob, cfg)
    refreshed = [score_leg(s.raw, p_need, cfg) for s in legs]
    ps = [r.p_true for r in refreshed]
    p_all = independence_joint(ps)
    hair = max((pair_correlation(refreshed[i], refreshed[j]) * 0.08
                for i in range(len(refreshed)) for j in range(i+1, len(refreshed))), default=0.0)
    p_all *= (1.0 - hair)
    mult = estimate_multiplier(n, slip_type, n_dem, n_gob)
    ev = p_all * mult - 1.0
    avg_p = sum(ps) / n
    avg_edge = sum(r.p_edge for r in refreshed) / n
    warnings = ["multipliers_illustrative_verify_in_app", "joint_p_independence_approx"]
    if p_all < cfg.prefer_flex_if_pair_p_below and slip_type == SlipType.POWER and n >= 3:
        warnings.append("consider_FLEX_low_joint_p")
    thesis = (f"{slip_type.value} {n}-pick | dem={n_dem} | "
              f"avg_p={avg_p:.3f} | p_all≈{p_all:.3f} | mult≈{mult:.2f} | EV/≈{ev:+.3f}")
    return SlipPlan(
        slip_id=str(uuid.uuid4())[:8], slip_type=slip_type,
        legs=[SlipLeg(scored=r, side=r.raw.side) for r in refreshed],
        n=n, n_demons=n_dem, p_each=ps, p_all_hit=p_all,
        avg_p_true=avg_p, avg_p_edge=avg_edge,
        est_multiplier=mult, est_ev_per_dollar=ev,
        stake=size_stake(ev, p_all, mult, cfg),
        warnings=warnings, thesis=thesis,
    )


def size_stake(ev_per_dollar: float, p_all: float, mult: float, cfg: GotitConfig) -> float:
    if ev_per_dollar <= 0 or p_all <= 0:
        return 0.0
    b = max(mult - 1.0, 1e-6)
    q = 1.0 - p_all
    f_star = (b * p_all - q) / b
    f = clamp(f_star * cfg.kelly_fraction, 0.0, cfg.max_stake_pct)
    stake = clamp(cfg.bankroll * f, 0.0, cfg.max_stake)
    if 0 < stake < cfg.min_stake:
        stake = cfg.min_stake if stake >= cfg.min_stake * 0.5 else 0.0
    return round(stake, 2)


def optimize_slips(
    universe: Sequence[RawLeg],
    cfg: GotitConfig,
    target_n: int = 5,
    slip_type: SlipType = SlipType.POWER,
    lock_demons: Optional[Sequence[ScoredLeg]] = None,
    top_pool: int = 40,
) -> List[SlipPlan]:
    n_dem_lock = len(lock_demons or [])
    p_need = estimate_p_need(target_n, slip_type, n_demons=max(n_dem_lock, 0), n_goblins=0, cfg=cfg)
    pool = [score_leg(L, p_need, cfg) for L in universe if not L.killed]
    pool = [s for s in pool if s.p_true >= cfg.min_leg_p_true * 0.90]
    pool.sort(key=lambda x: x.final_score, reverse=True)
    pool = pool[:top_pool]

    locked_ids = set()
    chosen: List[ScoredLeg] = []
    if lock_demons:
        for d in lock_demons:
            chosen.append(d)
            locked_ids.add(d.raw.leg_id)

    def corr_ok(c: ScoredLeg, team: List[ScoredLeg]) -> bool:
        return all(pair_correlation(c, t) <= cfg.correlation_max for t in team)

    for s in pool:
        if len(chosen) >= target_n:
            break
        if s.raw.leg_id in locked_ids:
            continue
        n_dem = sum(1 for x in chosen if x.raw.is_demon) + (1 if s.raw.is_demon else 0)
        if n_dem > cfg.max_demons_per_slip:
            continue
        if not corr_ok(s, chosen):
            continue
        chosen.append(s)
        locked_ids.add(s.raw.leg_id)

    plans: List[SlipPlan] = []
    best = build_candidate_slip(chosen, slip_type, cfg)
    if best and best.est_ev_per_dollar > 0:
        plans.append(best)

    if best:
        for i, cur in enumerate(list(chosen)):
            if lock_demons and cur.raw.leg_id in {d.raw.leg_id for d in lock_demons}:
                continue
            for alt in pool:
                if alt.raw.leg_id in {c.raw.leg_id for c in chosen}:
                    continue
                trial = chosen.copy()
                trial[i] = alt
                if sum(1 for x in trial if x.raw.is_demon) > cfg.max_demons_per_slip:
                    continue
                if any(pair_correlation(trial[a], trial[b]) > cfg.correlation_max
                       for a in range(len(trial)) for b in range(a+1, len(trial))):
                    continue
                plan = build_candidate_slip(trial, slip_type, cfg)
                if plan and (best is None or plan.est_ev_per_dollar > best.est_ev_per_dollar):
                    best = plan
                    chosen = trial
        if best and best not in plans:
            plans.insert(0, best)

    if slip_type == SlipType.POWER and target_n >= 3:
        flex = build_candidate_slip(chosen, SlipType.FLEX, cfg)
        if flex and flex.est_ev_per_dollar > 0:
            plans.append(flex)

    plans.sort(key=lambda p: p.est_ev_per_dollar, reverse=True)
    return plans


# =============================================================================
# Serialization
# =============================================================================

def scored_to_dict(s: ScoredLeg) -> Dict[str, Any]:
    # Extract misprice_edge from flags if present
    misprice_val = 0.0
    for f in (s.flags or []):
        if f.startswith("misprice_edge="):
            try:
                misprice_val = float(f.split("=")[1])
            except (IndexError, ValueError):
                pass
            break
    return {
        "leg_id": s.raw.leg_id, "game_id": s.raw.game_id,
        "player": s.raw.player_name, "stat": s.raw.stat_type,
        "line": s.raw.line, "standard_line": s.raw.standard_line,
        "side": s.raw.side, "is_demon": s.raw.is_demon,
        "tier": s.tier.value, "p_true": round(s.p_true, 4),
        "p_need": round(s.p_need, 4), "p_edge": round(s.p_edge, 4),
        "final_score": round(s.final_score, 4), "why": s.why, "flags": s.flags,
        # Misprice edge fields (Demontime only)
        "misprice_score": round(misprice_val, 3),
        "bump": round(s.bump, 3) if s.bump is not None else None,
    }


def demontime_to_dict(r: DemontimeResult) -> Dict[str, Any]:
    return {
        "game_id": r.game_id, "mode": r.mode.value,
        "demons": [scored_to_dict(d) for d in r.demons],
        "rejected_top": r.rejected_top, "warnings": r.warnings,
        "generated_at": r.generated_at,
    }


def slip_to_dict(p: SlipPlan) -> Dict[str, Any]:
    return {
        "slip_id": p.slip_id, "type": p.slip_type.value,
        "stake": p.stake, "thesis": p.thesis,
        "est_multiplier": p.est_multiplier,
        "est_ev_per_dollar": round(p.est_ev_per_dollar, 4),
        "p_all_hit": round(p.p_all_hit, 4),
        "legs": [{**scored_to_dict(sl.scored), "side": sl.side} for sl in p.legs],
        "warnings": p.warnings,
    }


# =============================================================================
# PropContext — 6-factor prop context scorer
# Factors:
#   1. role         — expected usage (minutes, PA, pitch count, snap%)
#   2. matchup      — opponent defense, pitcher handedness, stack context
#   3. game_env     — total, pace, spread/script, weather, park
#   4. recent_form  — L5/L10 rate vs baseline (baked in the number — context only)
#   5. market_anchor— where sharp books sit on similar props
#   6. liability    — move or limited (sharp action, steam, reverse)
# =============================================================================

@dataclass
class PropContext:
    # Factor 1 — Role
    role_confirmed: bool = True       # starter/regular, not capped
    role_score: float = 0.75          # 0–1; 1 = full usage confirmed
    usage_cap_risk: float = 0.0       # 0–1; injury/rest/limit risk
    is_callup: bool = False

    # Factor 2 — Matchup
    opp_rank: float = 0.50            # 0–1; 1 = weakest opponent
    pitcher_adv: float = 0.50         # 0–1; 1 = big platoon / handedness adv
    stack_context: float = 0.50       # 0–1; 1 = strong positive game stack

    # Factor 3 — Game environment
    game_total: float = 0.0           # O/U total (0 = unknown)
    game_total_z: float = 0.0         # z-score vs league avg total (+= more runs/pts)
    pace_factor: float = 0.50         # 0–1; 1 = fast pace (helps volume stats)
    park_factor: float = 0.50         # 0–1; 1 = hitter-friendly park
    spread_script: float = 0.0        # positive = favored (usage preservation)
    weather_risk: float = 0.0         # 0–1; 1 = high weather kill risk
    bullpen_day: bool = False

    # Factor 4 — Recent form (context only — baked into line)
    l10_rate: Optional[float] = None  # fraction of last 10 games that hit (prop-specific)
    baseline_rate: Optional[float] = None  # season baseline hit rate
    form_trend: float = 0.0           # +1 = hot, -1 = cold, 0 = neutral

    # Factor 5 — Market anchor
    book_consensus: Optional[float] = None  # 0–1 fair P from sharp books
    line_move: float = 0.0            # units moved (positive = moved up/harder)
    line_move_count: int = 0          # number of moves

    # Factor 6 — Liability / sharp action
    sharp_action: float = 0.0        # 0–1; 1 = heavy sharp action on More
    reverse_line_move: bool = False   # public on under, sharp on more
    is_limited: bool = False          # book limiting bets (too sharp)
    liability_flag: bool = False      # book moving for liability (move = fade signal)


def score_prop_context(ctx: PropContext) -> Dict[str, float]:
    """
    Convert a PropContext into scored components used by both
    The System (leg_selector ctx dict) and Demontime (RawLeg fields).

    Returns dict with:
      role_score      → RawLeg.role_score, conf_score factor 'role_locked'
      ctx_score       → RawLeg.ctx_score (matchup + env composite)
      boost_score     → RawLeg.boost_score (sharp action / liability edge)
      matchup_score   → leg_selector ctx['matchup_score']
      park_boost      → leg_selector ctx['park_boost']
      pace_boost      → leg_selector ctx['pace_boost']
      pitcher_adv     → leg_selector ctx['pitcher_adv']
      game_total_boost→ leg_selector ctx['game_total_boost']
      news_kill       → leg_selector ctx['news_kill'] (bool)
      weather_risk    → leg_selector ctx['weather_risk']
      fragility       → pre-computed fragility float (passed to _fragility_score)
      form_edge       → mild signal from form trend (not a primary driver)
    """
    # ── Factor 1: Role ───────────────────────────────────────────────────────
    role_score = clamp(ctx.role_score * (1.0 - ctx.usage_cap_risk * 0.5), 0.0, 1.0)
    if ctx.is_callup:
        role_score = clamp(role_score * 0.6, 0.0, 1.0)  # call-up penalty

    # ── Factor 2: Matchup ────────────────────────────────────────────────────
    matchup_raw = (ctx.opp_rank * 0.40 + ctx.pitcher_adv * 0.35 + ctx.stack_context * 0.25)
    matchup_score = clamp(matchup_raw, 0.0, 1.0)
    pitcher_adv   = clamp(ctx.pitcher_adv, 0.0, 1.0)

    # ── Factor 3: Game environment ───────────────────────────────────────────
    total_boost = clamp(0.5 + ctx.game_total_z * 0.20, 0.0, 1.0)
    park_boost  = clamp(ctx.park_factor, 0.0, 1.0)
    pace_boost  = clamp(ctx.pace_factor, 0.0, 1.0)
    env_score   = (total_boost * 0.40 + park_boost * 0.30 + pace_boost * 0.30)

    # Script: if team heavily favored, volume risk late (negative)
    script_pen = clamp(-ctx.spread_script * 0.02, 0.0, 0.10) if ctx.spread_script < -7 else 0.0
    env_score  = clamp(env_score - script_pen, 0.0, 1.0)

    # ── Factor 4: Recent form (mild — line already prices it) ────────────────
    form_edge = 0.0
    if ctx.l10_rate is not None and ctx.baseline_rate is not None and ctx.baseline_rate > 0:
        # Deviation from baseline, capped at ±0.1 edge
        form_edge = clamp((ctx.l10_rate - ctx.baseline_rate) * 0.5, -0.10, 0.10)
    form_edge += clamp(ctx.form_trend * 0.03, -0.06, 0.06)

    # ── Factor 5: Market anchor ──────────────────────────────────────────────
    # Line moving up = harder; line_move_count > 2 = market has seen it
    line_move_pen = clamp(ctx.line_move * 0.02, 0.0, 0.08)  # upward moves hurt
    market_confidence = 0.0
    if ctx.book_consensus is not None:
        market_confidence = clamp(ctx.book_consensus - 0.5, 0.0, 0.30)  # edge above 50

    # ── Factor 6: Liability / sharp action ──────────────────────────────────
    # Sharp action on More = positive signal; liability move (book moving) = caution
    sharp_edge = 0.0
    if ctx.reverse_line_move:
        sharp_edge = 0.15  # sharp on More despite public fading
    elif ctx.sharp_action > 0.6:
        sharp_edge = clamp((ctx.sharp_action - 0.6) / 0.4 * 0.10, 0.0, 0.10)

    liability_pen = 0.08 if ctx.liability_flag else 0.0
    limited_boost = 0.05 if ctx.is_limited else 0.0  # limits = sharp agreed
    boost_score = clamp(0.50 + sharp_edge + limited_boost - liability_pen, 0.0, 1.0)

    # ── Composite ctx_score (matchup + env + form mild + market) ────────────
    ctx_score = clamp(
        matchup_score * 0.35
        + env_score   * 0.35
        + market_confidence * 0.20
        + form_edge   * 0.10
        - line_move_pen,
        0.0, 1.0
    )

    # ── Kill flags ───────────────────────────────────────────────────────────
    news_kill    = (ctx.usage_cap_risk >= 0.90) or (ctx.weather_risk >= 0.90)
    weather_risk = ctx.weather_risk
    bullpen_day  = ctx.bullpen_day
    fragility    = clamp(
        ctx.usage_cap_risk * 0.30
        + ctx.weather_risk * 0.25
        + (0.20 if ctx.bullpen_day else 0.0)
        + (0.15 if not ctx.role_confirmed else 0.0)
        + (0.10 if ctx.is_callup else 0.0),
        0.0, 1.0
    )

    return {
        "role_score":       round(role_score, 3),
        "ctx_score":        round(ctx_score, 3),
        "boost_score":      round(boost_score, 3),
        "matchup_score":    round(matchup_score, 3),
        "park_boost":       round(park_boost, 3),
        "pace_boost":       round(pace_boost, 3),
        "pitcher_adv":      round(pitcher_adv, 3),
        "game_total_boost": round(total_boost, 3),
        "game_total_z":     round(ctx.game_total_z, 3),
        "form_edge":        round(form_edge, 4),
        "sharp_edge":       round(sharp_edge, 3),
        "market_confidence":round(market_confidence, 3),
        "news_kill":        news_kill,
        "weather_risk":     round(weather_risk, 3),
        "bullpen_day":      bullpen_day,
        "fragility":        round(fragility, 3),
        "role_confirmed":   ctx.role_confirmed,
        "is_callup":        ctx.is_callup,
    }


def prop_context_from_dict(d: Dict[str, Any]) -> PropContext:
    """
    Build a PropContext from a flat dict of prop/context fields.
    Used by prop_to_raw_leg when context data is embedded on the prop row.
    All fields are optional — missing fields use PropContext defaults.
    """
    return PropContext(
        role_confirmed   = bool(d.get('role_confirmed', True)),
        role_score       = float(d.get('role_score', 0.75) or 0.75),
        usage_cap_risk   = float(d.get('usage_cap_risk', 0) or 0),
        is_callup        = bool(d.get('is_callup') or d.get('callup')),
        opp_rank         = float(d.get('opp_rank', 0.50) or 0.50),
        pitcher_adv      = float(d.get('pitcher_adv', 0.50) or 0.50),
        stack_context    = float(d.get('stack_context', 0.50) or 0.50),
        game_total       = float(d.get('game_total', 0) or 0),
        game_total_z     = float(d.get('game_total_z', 0) or 0),
        pace_factor      = float(d.get('pace_factor', 0.50) or 0.50),
        park_factor      = float(d.get('park_factor', 0.50) or 0.50),
        spread_script    = float(d.get('spread_script', 0) or 0),
        weather_risk     = float(d.get('weather_risk', 0) or 0),
        bullpen_day      = bool(d.get('bullpen_day')),
        l10_rate         = float(d['l10_rate']) if d.get('l10_rate') is not None else None,
        baseline_rate    = float(d['baseline_rate']) if d.get('baseline_rate') is not None else None,
        form_trend       = float(d.get('form_trend', 0) or 0),
        book_consensus   = float(d['book_consensus']) if d.get('book_consensus') is not None else None,
        line_move        = float(d.get('lineMove', d.get('line_move', 0)) or 0),
        line_move_count  = int(d.get('lineMoveCount', d.get('line_move_count', 0)) or 0),
        sharp_action     = float(d.get('sharp_action', 0) or 0),
        reverse_line_move= bool(d.get('reverse_line_move')),
        is_limited       = bool(d.get('is_limited')),
        liability_flag   = bool(d.get('liability_flag')),
    )


# =============================================================================
# Live data adapter — converts GOTit DB props → RawLeg
# =============================================================================

def prop_to_raw_leg(prop: Dict[str, Any]) -> Optional[RawLeg]:
    """Convert a DB prop dict (from /api/slate) into a RawLeg for the engine."""
    try:
        prop_id    = str(prop.get("id") or "")
        game_id    = str(prop.get("gameId") or prop.get("game_id") or "")
        player_id  = str(prop.get("playerId") or prop.get("player_id") or prop_id)
        player     = str(prop.get("playerName") or prop.get("player_name") or "")
        stat_type  = str(prop.get("statType") or prop.get("stat_type") or "")
        line       = float(prop.get("lineScore") or prop.get("line_score") or 0)
        team       = str(prop.get("teamAbbr") or prop.get("team") or "")
        opponent   = str(prop.get("opponent") or "")
        league     = str(prop.get("league") or "MLB").upper()
        is_demon   = bool(prop.get("isDemon") or prop.get("is_demon"))
        is_goblin  = bool(prop.get("isGoblin") or prop.get("is_goblin"))
        is_synth   = bool(prop.get("isSynthetic"))
        direction  = str(prop.get("direction") or "over").lower()

        if is_synth or direction == "under":
            return None

        market: Optional[MarketQuote] = None
        sharp_fair = prop.get("sharpFairLine") or prop.get("sharp_fair_line")
        if sharp_fair is not None:
            try:
                # Convert fair line to synthetic american odds for de-vig
                # p_over ~ 0.5 + bias based on fair_line vs line
                sf = float(sharp_fair)
                bias = (sf - line) / max(line, 1.0) * 0.15
                p_est = clamp(0.50 + bias, 0.10, 0.90)
                over_am  = int(-100 * p_est / (1 - p_est)) if p_est > 0.5 else int(100 * (1 - p_est) / p_est)
                under_am = int(-100 * (1 - p_est) / p_est) if p_est < 0.5 else int(100 * p_est / (1 - p_est))
                market = MarketQuote(line=sf, over_american=over_am, under_american=under_am, book="sgo_sharp")
            except Exception:
                pass

        # Projection from mu/sigma — prefer projMu (mlb_projections) then mu
        proj_mean: Optional[float] = None
        mu_raw = float(prop.get("projMu") or prop.get("proj_mu") or prop.get("mu") or 0)
        if mu_raw > 0:
            proj_mean = mu_raw

        # lineup_ok kill — applied before scoring
        lineup_ok = prop.get('lineupOk') if prop.get('lineupOk') is not None else True
        if not lineup_ok:
            # Will be set killed on RawLeg below
            pass

        # Script tag + matchup tag for Demontime boost/kill
        script_tag  = prop.get('scriptTag')  or 'BLIND'
        matchup_tag = prop.get('matchupTag') or 'NEUTRAL'

        # Correlation keys
        corr_keys = [f"game:{game_id}"]
        if team:
            corr_keys.append(f"team:{team}")

        # Build PropContext from embedded fields on the prop row
        pctx = prop_context_from_dict(prop)
        scores = score_prop_context(pctx)

        # Demontime p_hit formula — same canonical formula as The System
        # sharp_edge = fair_p_win (book de-vigged) - 0.50
        # Also keep boost_score for EV/mult estimation (unchanged pipeline)
        _fair_p_win = prop.get('fairPWinOver') or prop.get('fair_p_win_over')
        _sharp_edge_dt = (float(_fair_p_win) - 0.50) if _fair_p_win is not None else None

        _hr_raw     = prop.get('hitRate') or prop.get('hit_rate')
        _p_hr_dt    = float(_hr_raw) if _hr_raw is not None else None
        _sample_dt  = int(prop.get('hitRateSample') or prop.get('hit_rate_sample') or 0)
        _matchup_dt = {'PLUS': 0.015, 'NEUTRAL': 0.0, 'MINUS': -0.015}.get(matchup_tag, 0.0)

        try:
            from gotit.leg_selector import p_hit_formula
            _p_hit_dt = p_hit_formula(_sharp_edge_dt, script_tag, _p_hr_dt, _sample_dt)
            _p_hit_dt = clamp(_p_hit_dt + _matchup_dt, 0.01, 0.99)
        except Exception:
            _p_hit_dt = 0.50

        # boost_score: legacy ranking proxy — anchored to p_hit so rankings are consistent
        _base_boost = scores["boost_score"] if not is_demon else clamp(scores["boost_score"] + 0.10, 0.0, 1.0)
        _final_boost = clamp(_base_boost, 0.0, 1.0)

        leg = RawLeg(
            leg_id=prop_id, game_id=game_id, player_id=player_id,
            player_name=player, team=team, opponent=opponent,
            stat_type=stat_type, line=line, side="MORE",
            is_demon=is_demon, is_goblin=is_goblin,
            proj_mean=proj_mean, market=market,
            role_score=scores["role_score"],
            ctx_score=scores["ctx_score"],
            boost_score=_final_boost,
            correlation_keys=corr_keys,
            killed=not lineup_ok,
            kill_reason='lineup_unconfirmed' if not lineup_ok else '',
            meta={
                "league":           league,
                "news_kill":        scores["news_kill"],
                "weather_risk":     scores["weather_risk"],
                "bullpen_day":      scores["bullpen_day"],
                "fragility":        scores["fragility"],
                "matchup_score":    scores["matchup_score"],
                "park_boost":       scores["park_boost"],
                "pace_boost":       scores["pace_boost"],
                "pitcher_adv":      scores["pitcher_adv"],
                "game_total_boost": scores["game_total_boost"],
                "form_edge":        scores["form_edge"],
                "sharp_edge":       scores["sharp_edge"],
                "role_confirmed":   scores["role_confirmed"],
                "is_callup":        scores["is_callup"],
                "script_tag":       script_tag,
                "matchup_tag":      matchup_tag,
                "lineup_ok":        lineup_ok,
                "p_hit":            round(_p_hit_dt, 4),
            },
        )
        return leg
    except Exception:
        return None


def run_gotit(legs: Sequence[RawLeg], cfg: Optional[GotitConfig] = None, target_n: int = 5) -> Dict[str, Any]:
    cfg = cfg or GotitConfig()
    dt_results = demontime_slate(legs, cfg)
    pass_demons: List[ScoredLeg] = []
    for r in dt_results:
        for d in r.demons:
            if d.tier == Tier.PASS:
                pass_demons.append(d)
    pass_demons.sort(key=lambda x: x.final_score, reverse=True)
    lock = pass_demons[:cfg.max_demons_per_slip]
    slips = optimize_slips(universe=legs, cfg=cfg, target_n=target_n,
                           slip_type=SlipType.POWER, lock_demons=lock if lock else None)
    return {
        "ok": True,
        "disclaimer": "Estimates only. No guaranteed wins. Verify payouts in-app.",
        "demontime": [demontime_to_dict(r) for r in dt_results],
        "locked_demons_for_slips": [scored_to_dict(d) for d in lock],
        "slips": [slip_to_dict(s) for s in slips[:5]],
        "best_slip": slip_to_dict(slips[0]) if slips else None,
    }
