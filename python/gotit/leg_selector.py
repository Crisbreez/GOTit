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
# Payout tables (PrizePicks standard)
# ─────────────────────────────────────────────────────────────────────────────

PAYOUTS: Dict[str, Dict[int, float]] = {
    "5_flex": {5: 10.0, 4: 2.0, 3: 0.4, 2: 0.0, 1: 0.0, 0: 0.0},
    "6_flex": {6: 25.0, 5: 2.0, 4: 0.4, 3: 0.0, 2: 0.0, 1: 0.0, 0: 0.0},
    "2_power": {2: 3.0, 1: 0.0, 0: 0.0},
    "3_power": {3: 5.0, 2: 0.0, 1: 0.0, 0: 0.0},
    "4_power": {4: 10.0, 3: 0.0, 2: 0.0, 1: 0.0, 0: 0.0},
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
    "max_same_game_legs": 2,
    "unit_pct_bankroll": 0.01,
    "lock_unit_pct":     0.02,
    "preferred_slips":   ["5_flex", "6_flex"],
    "fat_count":         0.03,
    "combo_head":        12,      # top-N by count to enumerate combos from
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
    is_demon:    bool = False
    is_goblin:   bool = False
    sport:       str  = ''
    team:        str  = ''


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
    return flags


# ─────────────────────────────────────────────────────────────────────────────
# Score one prop → two ScoredLegs (Section 5)
# ─────────────────────────────────────────────────────────────────────────────

def _estimate_mu_sigma(row: Dict, sharps: List[Dict]) -> Tuple[float, float]:
    """
    Estimate mu/sigma when no model projection is available.
    Use sharp line gap as signal; fall back to line * calibrated ratio.
    """
    L = float(row.get('lineScore', row.get('line', 0)) or 0)
    stat_type = str(row.get('statType', row.get('stat_type', '')) or '')

    sigma_ratios = {
        'Total Bases': 0.38, 'Hits': 0.55, 'Hits+Runs+RBIs': 0.40,
        'Pitcher Strikeouts': 0.32, 'Pitches Thrown': 0.14,
        'Pitching Outs': 0.28, 'Hits Allowed': 0.42,
        'Earned Runs Allowed': 0.70, 'Walks Allowed': 0.75,
        'Significant Strikes': 0.22, 'Hitter Fantasy Score': 0.40,
        'Points': 0.26, 'Rebounds': 0.40, 'Assists': 0.45,
        'Points+Rebounds+Assists': 0.28, 'Takedowns': 0.55,
        'Fight Time': 0.30, 'Rushing Attempts': 0.35,
        'Strikeouts': 0.32, 'Fantasy Score': 0.40,
    }
    sigma = max(0.5, L * sigma_ratios.get(stat_type, 0.40))

    # Sharp-informed mu
    sharp_lines = [q.get('line', L) for q in sharps if q.get('line') is not None]
    sharp_mu = sum(sharp_lines) / len(sharp_lines) if sharp_lines else L * 1.03

    return sharp_mu, sigma


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

    if model:
        mu    = float(model.get('mu', L) or L)
        sigma = float(model.get('sigma', 1.0) or 1.0)
        if sigma <= 1e-9:
            sigma = 1.0
    else:
        mu, sigma = _estimate_mu_sigma(row, sharps)

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

        pt = p_true_blend(pm, ps_raw, cfg.get('p_true_mode', 'min'))
        ps = ps_raw if ps_raw is not None else pm
        gap = _sharp_gap(sharps, L, side)
        traps = _detect_traps(row, side, ctx, cfg)

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

        eligible = len(kills) == 0

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
            eligible     = eligible,
            kill_reasons = kills,
            is_demon     = is_demon,
            is_goblin    = is_goblin,
            sport        = sport,
            team         = team,
        ))

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
        ctx = context.get(player_id, {})
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
        'eligible':    leg.eligible,
        'game_id':     leg.game_id,
        'is_demon':    leg.is_demon,
        'is_goblin':   leg.is_goblin,
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
