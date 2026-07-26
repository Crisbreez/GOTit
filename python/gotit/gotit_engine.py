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

    s = (
        cfg.w_p_true * p_true
        + cfg.w_p_edge * clamp(p_edge, -0.25, 0.25)
        + cfg.w_gap * tanh_norm(gap, 1.0)
        + cfg.w_bump * bq
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
    return {
        "leg_id": s.raw.leg_id, "game_id": s.raw.game_id,
        "player": s.raw.player_name, "stat": s.raw.stat_type,
        "line": s.raw.line, "side": s.raw.side, "is_demon": s.raw.is_demon,
        "tier": s.tier.value, "p_true": round(s.p_true, 4),
        "p_need": round(s.p_need, 4), "p_edge": round(s.p_edge, 4),
        "final_score": round(s.final_score, 4), "why": s.why, "flags": s.flags,
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

        # Market from sharpFairLine (MoneyLine DK/FD)
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
                market = MarketQuote(line=sf, over_american=over_am, under_american=under_am, book="moneyline_sharp")
            except Exception:
                pass

        # Projection from mu/sigma
        proj_mean: Optional[float] = None
        mu_raw = float(prop.get("mu", 0) or 0)
        if mu_raw > 0:
            proj_mean = mu_raw

        # Correlation keys
        corr_keys = [f"game:{game_id}"]
        if team:
            corr_keys.append(f"team:{team}")

        return RawLeg(
            leg_id=prop_id, game_id=game_id, player_id=player_id,
            player_name=player, team=team, opponent=opponent,
            stat_type=stat_type, line=line, side="MORE",
            is_demon=is_demon, is_goblin=is_goblin,
            proj_mean=proj_mean, market=market,
            role_score=0.80, ctx_score=0.50, boost_score=0.60 if is_demon else 0.0,
            correlation_keys=corr_keys,
            meta={"league": league},
        )
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
