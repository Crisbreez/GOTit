#!/usr/bin/env python3
"""
GOTit Optimizer — subprocess entry point called by Express /api/optimize.

Input  (stdin): JSON array of web-app props (from Supabase)
Output (stdout): JSON { game_id: { six_legs, two_demons, meta } }

Data flow:
  1. Load real SharpConsensus from sharp_store.json (written by /api/pull)
  2. For each PP prop: if real SGO fair_p_win exists → use it directly
     else → fall back to tier-delta calibration heuristic
  3. Build LegCandidates with real p_wins
  4. Run Shapley EV + corr-adj EV per game
  5. For games with ≥6 candidates + ≥2 demon candidates: run MILP
     For games that fail MILP: rank by Shapley EV (graceful fallback)
  6. Output { game_id: { six_legs, two_demons, meta } }
"""
import sys
import json
import hashlib
import datetime
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logging.basicConfig(level=logging.WARNING)
sys.path.insert(0, str(Path(__file__).parent))

from gotit.leg_selector import (
    PPProp,
    SharpConsensus,
    CalibrationParams,
    LegCandidate,
    Tier,
    Direction,
    get_default_calibration,
    get_family,
    _calibrated_p_win,
    BREAKEVEN_R,
    shapley_marginal_ev,
    corr_adjusted_ev,
    solve_game_milp,
)
from gotit.sharp_consensus import load_sharp_consensus


# ─────────────────────────────────────────────────────────────────────────────
# 1. Calibration
# ─────────────────────────────────────────────────────────────────────────────
def load_calibration() -> CalibrationParams:
    cal_path = Path(__file__).parent / "config" / "calibration_latest.json"
    if cal_path.exists():
        with open(cal_path) as f:
            data = json.load(f)
        try:
            return CalibrationParams(**data)
        except Exception as e:
            logging.warning(f"calibration load failed: {e}, using default")
    return get_default_calibration()


# ─────────────────────────────────────────────────────────────────────────────
# 2. Prop conversion helpers
# ─────────────────────────────────────────────────────────────────────────────
def _player_id(player_name: str) -> str:
    return hashlib.md5(player_name.lower().encode()).hexdigest()[:16]


def _prop_id(d: dict) -> str:
    pid = d.get("propId") or d.get("id") or d.get("sourcePropId")
    if pid:
        return str(pid)
    key = f"{d.get('playerName','')}-{d.get('statType','')}-{d.get('gameId','')}"
    return hashlib.md5(key.encode()).hexdigest()[:16]


def _tier(d: dict) -> Tier:
    if d.get("isDemon"):
        return Tier.DEMON
    if d.get("isGoblin"):
        return Tier.GOBLIN
    raw = (d.get("tier") or "").lower()
    if raw == "demon":
        return Tier.DEMON
    if raw == "goblin":
        return Tier.GOBLIN
    return Tier.STANDARD


def _direction(d: dict) -> Direction:
    raw = (d.get("direction") or "over").lower()
    return Direction.UNDER if raw == "under" else Direction.OVER


def build_pp_prop(d: dict) -> PPProp:
    """Convert web-app prop dict to PPProp for sharp_consensus matching."""
    line  = float(d.get("lineScore") or d.get("line_score") or 0.5)
    tier  = _tier(d)
    return PPProp(
        prop_id=_prop_id(d),
        game_id=d.get("gameId") or d.get("game_id") or "unknown",
        player_id=_player_id(d.get("playerName") or d.get("player_name") or ""),
        player_name=d.get("playerName") or d.get("player_name") or "",
        stat_type=d.get("statType") or d.get("stat_type") or "",
        tiers_offered=[tier],
        lines={tier: line},
        hours_to_lock=4.0,
        public_over_pct=None,
        dnp_prob=0.0,
        correlation_partners=[],
    )


# ─────────────────────────────────────────────────────────────────────────────
# 3. p_win computation
#    Priority: real SGO fair_p_win > calibration CDF
# ─────────────────────────────────────────────────────────────────────────────
def compute_p_win(
    d: dict,
    sc: SharpConsensus,
    cal: CalibrationParams,
) -> float:
    """
    Compute p_win for a prop.

    If SGO gave us a real de-vigged p_win (stored in sc.shape_params):
        Use it directly — this is the most accurate signal.
    Otherwise:
        Fall back to _calibrated_p_win with tier-delta adjusted median.
    """
    direction = _direction(d)

    # Check for real SGO fair p_win
    if sc.freshness_sec < 9999.0 and sc.shape_params:
        if direction == Direction.OVER:
            fair_p = sc.shape_params.get("fair_p_win_over")
        else:
            fair_p = sc.shape_params.get("fair_p_win_under")

        if fair_p is not None and 0.0 < fair_p < 1.0:
            return float(fair_p)

    # Fallback: use calibration CDF with tier-delta adjusted median
    line      = float(d.get("lineScore") or d.get("line_score") or 0.5)
    stat_type = d.get("statType") or d.get("stat_type") or ""
    tier      = _tier(d)
    median    = sc.median  # already tier-delta adjusted by fallback_sc

    family    = get_family(stat_type)
    cal_shape = cal.dist_params.get(stat_type, {})

    return float(_calibrated_p_win(line, median, cal_shape, family, direction, stat_type))


# ─────────────────────────────────────────────────────────────────────────────
# 4. Per-game ranking (Shapley EV + optional MILP)
# ─────────────────────────────────────────────────────────────────────────────
MAX_SHAPLEY = 15  # caps Shapley iterations at O(C(15,5)) = 3003

def rank_game_props(
    game_id: str,
    props: List[dict],
    sc_map: Dict[str, SharpConsensus],
    cal: CalibrationParams,
) -> Optional[Dict]:
    """
    Build ranked { six_legs, two_demons, meta } for one game.
    Returns None if not enough props.
    """
    candidates: List[LegCandidate] = []

    for d in props:
        try:
            prop_id   = _prop_id(d)
            sc        = sc_map.get(prop_id)
            if sc is None:
                # Build a fallback SC inline
                line  = float(d.get("lineScore") or 0.5)
                tier  = _tier(d)
                stat  = d.get("statType") or ""
                from gotit.sharp_consensus import _fallback_sc
                sc = _fallback_sc(prop_id, line, tier, stat)

            p_win = compute_p_win(d, sc, cal)
            line  = float(d.get("lineScore") or 0.5)
            tier  = _tier(d)
            dir_  = _direction(d)

            candidates.append(LegCandidate(
                prop_id=prop_id,
                game_id=game_id,
                player_id=_player_id(d.get("playerName") or ""),
                player_name=d.get("playerName") or "",
                stat_type=d.get("statType") or "",
                tier=tier,
                line=line,
                direction=dir_,
                p_win=p_win,
            ))
        except Exception as e:
            logging.warning(f"skip prop {d.get('playerName','?')}: {e}")

    if len(candidates) < 2:
        return None

    # Pre-rank and cap for Shapley
    top = sorted(candidates, key=lambda c: c.p_win, reverse=True)[:MAX_SHAPLEY]

    # Shapley EV
    try:
        shapley   = shapley_marginal_ev(top, BREAKEVEN_R)
        corr_adj  = corr_adjusted_ev(top, shapley, {})
        for c in top:
            c.ev_marginal = shapley.get(c.prop_id, 0.0)
            c.ev_corr_adj = corr_adj.get(c.prop_id, 0.0)
    except Exception as e:
        logging.warning(f"Shapley failed for {game_id}: {e}")
        for c in top:
            c.ev_marginal = max(0.0, c.p_win - BREAKEVEN_R[6])
            c.ev_corr_adj = c.ev_marginal

    ranked = sorted(top, key=lambda c: c.ev_corr_adj, reverse=True)

    # ── Try MILP first (strict: ≥6 candidates, ≥2 distinct-player demons) ──
    milp_result = None
    demon_cands = [c for c in top if c.tier == Tier.DEMON]
    demon_players = set(c.player_id for c in demon_cands)

    if len(top) >= 6 and len(demon_players) >= 2:
        r_star_6 = BREAKEVEN_R[6]
        ev_map   = {c.prop_id: c.ev_corr_adj for c in top}
        try:
            milp_result = solve_game_milp(top, ev_map, r_star_6)
        except Exception as e:
            logging.warning(f"MILP failed for {game_id}: {e}")

    if milp_result and len(milp_result) == 6:
        # MILP succeeded — use its exact selection
        six_legs   = milp_result
        two_demons = [lg for lg in milp_result if lg.tier == Tier.DEMON][:2]
        source     = "milp"
    else:
        # Graceful fallback: Shapley EV ranking
        six_legs = _select_diverse(ranked, n=6)
        two_demons = _pick_demons(candidates, ranked, n=2)
        source = "shapley_fallback"

    if not six_legs:
        return None

    def leg_dict(lg: LegCandidate) -> dict:
        return {
            "prop_id":     lg.prop_id,
            "player_name": lg.player_name,
            "stat_type":   lg.stat_type,
            "tier":        lg.tier.value if hasattr(lg.tier, "value") else str(lg.tier),
            "line":        lg.line,
            "direction":   lg.direction.value if hasattr(lg.direction, "value") else str(lg.direction),
            "p_win":       round(lg.p_win, 4),
            "ev_marginal": round(lg.ev_marginal, 4),
            "ev_corr_adj": round(lg.ev_corr_adj, 4),
        }

    # Count how many had real SGO data
    real_sharp = sum(
        1 for d in props
        if sc_map.get(_prop_id(d), SharpConsensus("","",{},"",[],9999.0)).freshness_sec < 9999.0
    )

    return {
        "six_legs":   [leg_dict(lg) for lg in six_legs],
        "two_demons": [leg_dict(lg) for lg in two_demons],
        "meta": {
            "source":              source,
            "slate_breakeven_r6":  round(BREAKEVEN_R[6], 4),
            "total_candidates":    len(candidates),
            "real_sharp_props":    real_sharp,
        },
    }


def _select_diverse(ranked: List[LegCandidate], n: int) -> List[LegCandidate]:
    """Pick top n by EV with max-3-per-player diversity."""
    selected: List[LegCandidate] = []
    player_count: Dict[str, int] = {}
    for c in ranked:
        if len(selected) >= n:
            break
        if player_count.get(c.player_id, 0) >= 3:
            continue
        selected.append(c)
        player_count[c.player_id] = player_count.get(c.player_id, 0) + 1
    return selected


def _pick_demons(
    all_cands: List[LegCandidate],
    ranked: List[LegCandidate],
    n: int,
) -> List[LegCandidate]:
    """
    Pick n demon props for display (distinct players).
    Prefer Tier.DEMON, fall back to highest p_win if fewer than n.
    """
    demons: List[LegCandidate] = []
    seen: set = set()
    # First: native demons by p_win desc
    for c in sorted([c for c in all_cands if c.tier == Tier.DEMON],
                    key=lambda c: c.p_win, reverse=True):
        if len(demons) >= n:
            break
        if c.player_id not in seen:
            demons.append(c)
            seen.add(c.player_id)
    # Fill from ranked if needed
    if len(demons) < n:
        for c in ranked:
            if len(demons) >= n:
                break
            if c.player_id not in seen:
                demons.append(c)
                seen.add(c.player_id)
    return demons


# ─────────────────────────────────────────────────────────────────────────────
# 5. Main
# ─────────────────────────────────────────────────────────────────────────────
def main():
    raw = sys.stdin.read().strip()
    if not raw:
        print(json.dumps({"error": "empty input"}))
        sys.exit(1)

    try:
        props_data = json.loads(raw)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"json parse: {e}"}))
        sys.exit(1)

    if not props_data:
        print(json.dumps({"error": "no props"}))
        sys.exit(1)

    cal = load_calibration()

    # Convert to PPProp for SharpConsensus matching
    pp_props = []
    for d in props_data:
        try:
            pp_props.append(build_pp_prop(d))
        except Exception:
            pass

    # Load real sharp consensus (from store written by /api/pull)
    sc_map: Dict[str, SharpConsensus] = load_sharp_consensus(pp_props)

    # Group by gameId
    games: Dict[str, List[dict]] = {}
    for d in props_data:
        gid = d.get("gameId") or d.get("game_id") or "unknown"
        games.setdefault(gid, []).append(d)

    output: Dict[str, dict] = {}
    for game_id, game_props in games.items():
        result = rank_game_props(game_id, game_props, sc_map, cal)
        if result:
            output[game_id] = result

    print(json.dumps(output))


if __name__ == "__main__":
    main()
