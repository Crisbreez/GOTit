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
import re
from pathlib import Path

logging.basicConfig(level=logging.WARNING)
sys.path.insert(0, str(Path(__file__).parent))

from gotit.sharp_consensus import pull_sharp_consensus
from gotit.sharp_consensus import PPProp, Tier
from mlb_projections import run as build_mlb_projections, _normalize as _norm_name

# Module-level projection cache (built once per process)
_PROJ_CACHE: dict | None = None

def _get_projections(league: str) -> dict:
    """Lazy-load projection cache for the given league."""
    global _PROJ_CACHE
    if league.upper() != "MLB":
        return {}  # Only MLB projections wired for now
    if _PROJ_CACHE is None:
        try:
            _PROJ_CACHE = build_mlb_projections()
            logging.info("Projection cache built: %d entries", len(_PROJ_CACHE))
        except Exception as e:
            logging.warning("Projection build failed: %s", e)
            _PROJ_CACHE = {}
    return _PROJ_CACHE


def _lookup_projection(projections: dict, player_name: str, stat_type: str) -> dict | None:
    """Look up a projection by player+stat, with fuzzy name matching."""
    # Exact key first
    key = f"{player_name}||{stat_type}"
    if key in projections:
        return projections[key]
    # Fuzzy: normalize player name
    norm = _norm_name(player_name)
    for k, v in projections.items():
        if v["stat_type"] == stat_type and _norm_name(v["player_name"]) == norm:
            return v
    return None


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

    # Build projections for MLB (lazy cache)
    projections = _get_projections(league)

    # Fetch confirmed starters for lineup_ok stamping (MLB only)
    confirmed_starters: set = set()
    if league == 'MLB':
        try:
            from gotit.lineup_check import fetch_confirmed_starters
            confirmed_starters = fetch_confirmed_starters()
        except Exception as _le:
            import logging
            logging.getLogger(__name__).warning(f'[lineup_check] skipped: {_le}')

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
    # Express will stamp sharpFairLine / ppShadeSignal / projMu / projSigma on each prop.
    prop_enrichments = []
    proj_matched = 0
    for d in props_data:
        prop_id   = d.get('id', '')
        player    = str(d.get('playerName') or d.get('player_name') or '')
        stat_type = str(d.get('statType')   or d.get('stat_type')   or '')
        pp_line   = float(d.get('lineScore') or d.get('line_score') or 0)
        sc        = consensus.get(prop_id)

        enrichment: dict = {'id': prop_id}

        # Sharp fair line
        if sc and sc.freshness_sec < 9999.0:
            fair_line = sc.median
            delta     = pp_line - fair_line
            if delta > 0.3:
                shade = 'lean_under'
            elif delta < -0.3:
                shade = 'lean_over'
            else:
                shade = 'neutral'
            enrichment['sharpFairLine'] = round(fair_line, 3)
            enrichment['ppShadeSignal'] = shade
            enrichment['marketDelta']   = round(delta, 3)
        else:
            enrichment['ppShadeSignal'] = 'no_data'

        # Real projection mu/sigma
        proj = _lookup_projection(projections, player, stat_type)
        if proj:
            enrichment['projMu']    = proj['mu']
            enrichment['projSigma'] = proj['sigma']
            enrichment['projNGames']= proj['n_games']
            enrichment['projSource']= proj['source']
            proj_matched += 1

        # Script tag + matchup tag
        try:
            from gotit.script_tag import compute_script_tag, compute_matchup_tag
            # Merge enrichment so far into prop dict for tag computation
            merged = {**d, **enrichment}
            enrichment['scriptTag']   = compute_script_tag(merged)
            enrichment['matchupTag']  = compute_matchup_tag(merged)
        except Exception:
            enrichment['scriptTag']  = 'BLIND'
            enrichment['matchupTag'] = 'NEUTRAL'

        # Lineup ok — MLB only
        if league == 'MLB':
            try:
                from gotit.lineup_check import is_lineup_ok
                enrichment['lineupOk'] = is_lineup_ok(player, confirmed_starters)
            except Exception:
                enrichment['lineupOk'] = True
        else:
            enrichment['lineupOk'] = True

        prop_enrichments.append(enrichment)

    print(json.dumps({
        "ok": True,
        "league": league,
        "matched": matched,
        "total": len(pp_props),
        "enrichments": prop_enrichments,
    }))


if __name__ == "__main__":
    main()
