#!/usr/bin/env python3
"""
sharp_pull.py — Subprocess called by Express after /api/pull completes.

Input  (stdin): JSON { "league": "MLB", "props": [...webapp prop dicts...] }
Output (stdout): JSON { "ok": true, "matched": N, "total": M, "league": "MLB" }
                 or   { "ok": false, "error": "..." }

Express calls this with:
    python3 python/sharp_pull.py < {"league":..., "props":[...]}
"""
import sys
import json
import logging
from pathlib import Path

logging.basicConfig(level=logging.WARNING)
sys.path.insert(0, str(Path(__file__).parent))

from gotit.sharp_consensus import pull_sharp_consensus
from gotit.sharp_consensus import PPProp, Tier


def _make_pp_prop(d: dict):
    """Convert a webapp prop dict → PPProp for sharp matching."""
    prop_id    = str(d.get('id') or d.get('prizepicks_id') or '')
    player     = str(d.get('playerName') or d.get('player_name') or '')
    stat_type  = str(d.get('statType')   or d.get('stat_type')   or '')
    line       = float(d.get('lineScore') or d.get('line_score') or 0)
    tier_str   = Tier.DEMON if d.get('isDemon') else (Tier.GOBLIN if d.get('isGoblin') else Tier.STANDARD)
    return PPProp(
        prop_id=prop_id,
        player_name=player,
        stat_type=stat_type,
        lines={tier_str: line},
        tiers_offered=[tier_str],
    )


def main():
    raw = sys.stdin.read().strip()
    if not raw:
        print(json.dumps({"ok": False, "error": "empty input"}))
        sys.exit(1)

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"json parse: {e}"}))
        sys.exit(1)

    league = payload.get("league", "MLB").upper()
    props_data = payload.get("props", [])

    if not props_data:
        print(json.dumps({"ok": False, "error": "no props in payload"}))
        sys.exit(1)

    # Convert to PPProp objects for matching
    pp_props = []
    for d in props_data:
        try:
            pp_props.append(_make_pp_prop(d))
        except Exception as e:
            logging.warning(f"skip prop {d.get('playerName','?')}: {e}")

    if not pp_props:
        print(json.dumps({"ok": False, "error": "no valid PPProps"}))
        sys.exit(1)

    # Pull sharp consensus from SGO, write to sharp_store.json
    try:
        consensus = pull_sharp_consensus(league, pp_props)
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.exit(1)

    matched = sum(
        1 for sc in consensus.values()
        if sc.freshness_sec < 9999.0
    )

    # Build per-prop sharp enrichment to send back to Express.
    # Express will stamp sharpFairLine / ppShadeSignal on each prop before upsert.
    prop_enrichments = []
    for d in props_data:
        prop_id = d.get('id', '')
        sc = consensus.get(prop_id)
        if sc and sc.freshness_sec < 9999.0:
            pp_line = float(d.get('lineScore') or 0)
            fair_line = sc.median
            delta = pp_line - fair_line
            if delta > 0.3:
                shade = 'lean_under'
            elif delta < -0.3:
                shade = 'lean_over'
            else:
                shade = 'neutral'
            prop_enrichments.append({
                'id': prop_id,
                'sharpFairLine': round(fair_line, 3),
                'ppShadeSignal': shade,
                'marketDelta': round(delta, 3),
            })
        else:
            prop_enrichments.append({'id': prop_id, 'ppShadeSignal': 'no_data'})

    print(json.dumps({
        "ok": True,
        "league": league,
        "matched": matched,
        "total": len(pp_props),
        "enrichments": prop_enrichments,
    }))


if __name__ == "__main__":
    main()
