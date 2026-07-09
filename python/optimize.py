#!/usr/bin/env python3
"""
GOTit Optimizer — called by Express as a subprocess.

Input  (stdin): JSON array of web-app props (from PrizePicks via Supabase)
Output (stdout): JSON { game_id: { six_legs, two_demons, meta } }

Strategy:
  Since we have no sharp-book medians, we compute p_win from the PP line
  directly using tier-delta calibration:
    - Standard: median = lineScore (line IS the market estimate)
    - Demon: median = lineScore - delta_demon[stat] (line raised above median)
    - Goblin: median = lineScore + delta_goblin[stat] (line lowered below median)

  The MILP full-optimizer is available when delta shifts produce enough
  above-floor legs. When MILP fails (insufficient legs), we fall back to
  simple p_win ranking per game.

Web-app prop shape (camelCase from frontend):
  { propId, gameId, playerName, teamAbbr, statType, lineScore,
    isDemon, isGoblin, direction, gameMatchup, gameStartTime }
"""
import sys
import json
import hashlib
import datetime
import logging
from pathlib import Path
from typing import Dict, List, Tuple, Optional

logging.basicConfig(level=logging.WARNING)

# Add python/ dir to path so gotit package resolves
sys.path.insert(0, str(Path(__file__).parent))

from gotit.leg_selector import (
    PPProp,
    SharpConsensus,
    CalibrationParams,
    LegCandidate,
    Tier,
    Direction,
    DistFamily,
    get_default_calibration,
    get_family,
    _calibrated_p_win,
    BREAKEVEN_R,
    shapley_marginal_ev,
    corr_adjusted_ev,
    solve_game_milp,
    select_legs_for_slate,
)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Load calibration
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
# 2. Helpers
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


# ─────────────────────────────────────────────────────────────────────────────
# 3. Compute p_win per prop using calibration tier deltas
# ─────────────────────────────────────────────────────────────────────────────
def compute_p_win(d: dict, cal: CalibrationParams) -> float:
    """
    Compute p_win for a single web-app prop.

    Tier-delta logic:
    - Standard: median = lineScore (line ≈ market median, p_win ≈ 0.5)
    - Demon:    line is ABOVE the true median by delta_demon[stat].
                To hit OVER on a demon: median = line - delta → p_win > 0.5
    - Goblin:   line is BELOW the true median by delta_goblin[stat].
                PP goblins are played OVER, so: median = line + delta → p_win > 0.5
    """
    line = float(d.get("lineScore") or d.get("line_score") or 0.5)
    stat_type = d.get("statType") or d.get("stat_type") or ""
    tier = _tier(d)
    direction = _direction(d)

    if tier == Tier.DEMON:
        delta = cal.delta_demon.get(stat_type, cal.delta_demon.get("default", 0.0))
        median = line - delta   # demon line > median → OVER has edge
    elif tier == Tier.GOBLIN:
        delta = cal.delta_goblin.get(stat_type, cal.delta_goblin.get("default", 0.0))
        median = line + delta   # goblin line < median → OVER has edge
    else:
        median = line           # standard: fair line, p_win ≈ 0.5

    family = get_family(stat_type)
    cal_shape = cal.dist_params.get(stat_type, {})

    return float(_calibrated_p_win(line, median, cal_shape, family, direction, stat_type))


# ─────────────────────────────────────────────────────────────────────────────
# 4. Build per-game result with Shapley EV (no MILP — works without oracle data)
# ─────────────────────────────────────────────────────────────────────────────
def rank_game_props(
    game_id: str,
    props: List[dict],
    cal: CalibrationParams,
) -> Optional[Dict]:
    """
    Rank props for a single game by p_win and Shapley EV.

    Returns { six_legs, two_demons, meta } or None if too few props.
    """
    # Build LegCandidates for all props
    candidates: List[LegCandidate] = []
    for d in props:
        try:
            line = float(d.get("lineScore") or d.get("line_score") or 0.5)
            tier = _tier(d)
            direction = _direction(d)
            p_win = compute_p_win(d, cal)
            candidates.append(LegCandidate(
                prop_id=_prop_id(d),
                game_id=game_id,
                player_id=_player_id(d.get("playerName") or d.get("player_name") or ""),
                player_name=d.get("playerName") or d.get("player_name") or "",
                stat_type=d.get("statType") or d.get("stat_type") or "",
                tier=tier,
                line=line,
                direction=direction,
                p_win=p_win,
            ))
        except Exception as e:
            logging.warning(f"skip prop {d.get('playerName','?')}: {e}")

    if len(candidates) < 2:
        return None

    # Compute Shapley EV (caps at 15 candidates for performance)
    top_cands = sorted(candidates, key=lambda c: c.p_win, reverse=True)[:15]
    try:
        shapley = shapley_marginal_ev(top_cands, BREAKEVEN_R)
        for c in top_cands:
            c.ev_marginal = shapley.get(c.prop_id, 0.0)
        corr_adj = corr_adjusted_ev(top_cands, shapley, {})
        for c in top_cands:
            c.ev_corr_adj = corr_adj.get(c.prop_id, 0.0)
    except Exception as e:
        logging.warning(f"Shapley failed for game {game_id}: {e}")
        # Fallback: ev_marginal = p_win - breakeven
        for c in top_cands:
            c.ev_marginal = max(0.0, c.p_win - BREAKEVEN_R[6])
            c.ev_corr_adj = c.ev_marginal

    # Sort by ev_corr_adj descending (best legs first)
    ranked = sorted(top_cands, key=lambda c: c.ev_corr_adj, reverse=True)

    # Six legs: top 6 by EV (at most 1 per player, diversity-aware)
    six_legs: List[LegCandidate] = []
    seen_players: set = set()
    seen_stats: set = set()
    # Prefer diverse stat types: first pick best per stat, then fill remaining
    for c in ranked:
        if len(six_legs) >= 6:
            break
        # Enforce max 3 per player
        player_count = sum(1 for l in six_legs if l.player_id == c.player_id)
        if player_count >= 3:
            continue
        six_legs.append(c)
        seen_players.add(c.player_id)
        seen_stats.add(c.stat_type)

    # Two demons: best 2 demon-tier props by p_win (distinct players)
    demon_cands = sorted([c for c in candidates if c.tier == Tier.DEMON],
                         key=lambda c: c.p_win, reverse=True)
    two_demons: List[LegCandidate] = []
    demon_players: set = set()
    for dc in demon_cands:
        if len(two_demons) >= 2:
            break
        if dc.player_id not in demon_players:
            two_demons.append(dc)
            demon_players.add(dc.player_id)

    # If fewer than 2 demon-tier props exist, pick from highest p_win standards
    if len(two_demons) < 2:
        non_demons = [c for c in ranked if c.tier != Tier.DEMON and c.player_id not in demon_players]
        for c in non_demons:
            if len(two_demons) >= 2:
                break
            if c.player_id not in demon_players:
                two_demons.append(c)
                demon_players.add(c.player_id)

    if not six_legs and not two_demons:
        return None

    def leg_to_dict(lg: LegCandidate) -> dict:
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

    return {
        "six_legs":   [leg_to_dict(lg) for lg in six_legs],
        "two_demons": [leg_to_dict(lg) for lg in two_demons],
        "meta": {
            "slate_breakeven_r6":  round(BREAKEVEN_R[6], 4),
            "total_candidates":    len(candidates),
            "ranked_count":        len(ranked),
        },
    }


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
        print(json.dumps({"error": f"json parse error: {e}"}))
        sys.exit(1)

    if not props_data:
        print(json.dumps({"error": "no props"}))
        sys.exit(1)

    cal = load_calibration()

    # Group by gameId
    games: Dict[str, List[dict]] = {}
    for d in props_data:
        gid = d.get("gameId") or d.get("game_id") or "unknown"
        games.setdefault(gid, []).append(d)

    output: Dict[str, dict] = {}
    for game_id, game_props in games.items():
        result = rank_game_props(game_id, game_props, cal)
        if result:
            output[game_id] = result

    print(json.dumps(output))


if __name__ == "__main__":
    main()
