"""
demon_pipeline.py — Demontime (powered by GOTit Engine v1)

Thin shim: converts raw prop dicts → RawLeg → engine demontime_for_game → back to dict shape
that qualify_demons.py / routes.ts already expect.
"""
from __future__ import annotations

import json
import logging
import sys
from typing import Any, Dict, List, Optional

from gotit.gotit_engine import (
    GotitConfig, SlipType,
    prop_to_raw_leg, demontime_for_game,
    scored_to_dict,
)

log = logging.getLogger(__name__)

cfg = GotitConfig()


def run_demon_pipeline(
    props: List[Dict[str, Any]],
    game_id: str,
    slip_context: Optional[Dict] = None,
    sport: str = "MLB",
) -> Dict[str, Any]:
    """
    Run Demontime for a single game via GOTit Engine v1.
    Always returns top 2 demons when ≥2 allowed demon props exist.
    """
    # Convert props → RawLegs
    legs = [prop_to_raw_leg(p) for p in props]
    legs = [L for L in legs if L is not None and L.game_id == game_id]

    # Run engine
    result = demontime_for_game(legs, game_id, cfg, slip_type=SlipType.POWER, assumed_n_legs=5)

    # Convert ScoredLegs back to prop-dict shape routes.ts expects
    def to_prop(sl, rank: int) -> Dict:
        d = scored_to_dict(sl)
        raw = sl.raw
        return {
            # Original prop fields (pass through)
            "id":             raw.leg_id,
            "playerName":     raw.player_name,
            "statType":       raw.stat_type,
            "lineScore":      raw.line,
            "direction":      "over",
            "isDemon":        True,
            "isGoblin":       raw.is_goblin,
            "gameId":         raw.game_id,
            "league":         raw.meta.get("league", sport) if isinstance(raw.meta, dict) else sport,
            # Demontime scoring
            "rank":           rank,
            "tier":           d["tier"],
            "p_true":         d["p_true"],
            "p_need":         d["p_need"],
            "p_edge":         d["p_edge"],
            "p_hit":          d["p_true"],
            "propScore":      d["final_score"],
            "final_score":    d["final_score"],
            "confidenceLevel": max(1, min(5, round(d["p_true"] * 5))),
            "why":            d["why"],
            "flags":          d["flags"],
            "eligible":       True,
            "ineligible_reason": "",
        }

    picks  = [to_prop(s, i + 1) for i, s in enumerate(result.demons)]
    others = [to_prop(s, i + len(picks) + 1) for i, s in enumerate(
        [s for s in [] ]  # other_demons — engine doesn't expose rest, use rejected
    )]

    # Also expose raw other scored demons for UI
    other_props = []
    if hasattr(result, 'rejected_top'):
        for r in result.rejected_top:
            other_props.append({
                "playerName": r.get("player", ""),
                "statType": r.get("stat", ""),
                "lineScore": r.get("line", 0),
                "isDemon": True,
                "eligible": False,
                "ineligible_reason": r.get("reason", ""),
                "final_score": r.get("score", 0),
                "tier": r.get("tier", "STRETCH"),
            })

    return {
        "selected_demons":        picks,
        "post_relaxation_demons": picks,
        "other_demons":           other_props,
        "status":  "CLEAR" if picks else "NO-GO",
        "strategy": "Demontime",
        "mode": result.mode.value,
        "warnings": result.warnings,
        "rejected_top": result.rejected_top,
        "trace": {
            "game_id":           game_id,
            "sport":             sport,
            "total_demon_props": len([L for L in legs if L.is_demon]),
            "selected":          len(picks),
            "mode":              result.mode.value,
        },
        "error": None,
    }


def format_output(result: Dict[str, Any]) -> Dict[str, Any]:
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    try:
        payload = json.loads(sys.stdin.read())
        props   = payload if isinstance(payload, list) else payload.get("props", [])
        game_id = (props[0].get("gameId") or props[0].get("game_id") or "unknown") if props else "unknown"
        sport   = (props[0].get("league") or "MLB") if props else "MLB"
        result  = run_demon_pipeline(props, str(game_id), sport=str(sport))
        print(json.dumps(result))
    except Exception as exc:
        log.exception("[Demontime] fatal error")
        print(json.dumps({"error": str(exc), "selected_demons": [], "post_relaxation_demons": []}))
        sys.exit(1)
