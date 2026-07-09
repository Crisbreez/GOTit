"""
sharp_consensus.py — Build a SharpConsensus map from Pinnacle XML feed.

Pinnacle's free guest API returns JSON (not XML despite the variable name).
We parse it and attempt to match player prop lines to PrizePicks props by
player name + stat type + line proximity.

Until a proper Pinnacle player-props endpoint is confirmed working, this
module returns empty SharpConsensus entries (freshness_sec=999) which causes
leg_selector to fall back to its internal CDF-based p_win estimate.
The TODO sections mark where you'd plug in real Pinnacle parsing.
"""
import asyncio
from datetime import datetime, timezone
from typing import Dict, List

import httpx

from gotit.leg_selector import PPProp, SharpConsensus

# Pinnacle guest API — returns league list / odds, not player props on free tier
PINNACLE_BASE = "https://guest.api.arcadia.pinnacle.com/0.1"

# Fallback: if Pinnacle has no match for a prop, we return this sentinel
# so leg_selector knows the sharp data is stale / unavailable
_STALE_SEC = 999.0


def _make_fallback(prop: PPProp) -> SharpConsensus:
    """
    Return a sentinel SharpConsensus with no real sharp data.

    Tier-aware median shift:
    - Demon: PP sets the line generously high (over-friendly). Shift median
      5% above the line so p_win(OVER) reflects the inherent demon edge.
    - Goblin: PP sets the line generously low (under-friendly). Shift median
      5% below the line so p_win(UNDER) reflects the goblin edge.
    - Standard: median = line (no edge assumed without real data).
    """
    line = list(prop.lines.values())[0]
    tier = prop.tiers_offered[0] if prop.tiers_offered else None

    from gotit.leg_selector import Tier as _Tier
    if tier == _Tier.DEMON:
        median = line * 1.05      # demon line is set below true median → p_win OVER > 0.5
    elif tier == _Tier.GOBLIN:
        median = line * 0.95      # goblin line is set above true median → p_win UNDER > 0.5
    else:
        median = line

    return SharpConsensus(
        prop_id=prop.prop_id,
        median=median,
        shape_params={},
        timestamp=datetime.now(timezone.utc).isoformat(),
        books_used=[],
        freshness_sec=_STALE_SEC,
    )


async def _fetch_pinnacle_player_props(league: str) -> dict:
    """
    Attempt to fetch Pinnacle player props for a league.
    Returns raw JSON dict on success, empty dict on any failure.

    TODO: Map GOTit league names (MLB, NBA, NFL, MMA) to Pinnacle league IDs
    and parse the response into a lookup keyed by player name + stat + line.
    """
    # Pinnacle league IDs (free tier):
    # NFL=889, NBA=487, MLB=246, UFC/MMA=686
    league_ids = {"NFL": 889, "NBA": 487, "MLB": 246, "MMA": 686}
    lid = league_ids.get(league.upper())
    if not lid:
        return {}

    url = f"{PINNACLE_BASE}/leagues/{lid}/matchups"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                url,
                headers={"Accept": "application/json", "User-Agent": "GOTit/1.0"},
            )
            r.raise_for_status()
            return r.json()
    except Exception:
        return {}


def _parse_pinnacle_into_lookup(raw: dict) -> Dict[str, tuple[float, dict]]:
    """
    Parse Pinnacle matchup JSON into a lookup:
        key:   "{normalized_player_name}|{stat_type}|{line}"
        value: (median_implied, shape_params_dict)

    TODO: Implement once Pinnacle player-prop endpoint structure is confirmed.
    For now returns empty dict so the fallback path is always used.
    """
    # ── STUB ──
    # When you have real Pinnacle player prop data, build the lookup here.
    # Example structure you'd parse:
    #   for matchup in raw.get("matchups", []):
    #       for participant in matchup.get("participants", []):
    #           ...extract player, line, juice...
    #           implied_prob = juice_to_prob(juice)
    #           median = line_from_prob(implied_prob, line)
    #           key = f"{normalize(player_name)}|{stat}|{line}"
    #           lookup[key] = (median, {"a": ..., "scale": ...})
    return {}


async def build_sharp_consensus(props: List[PPProp]) -> Dict[str, SharpConsensus]:
    """
    Build a {prop_id: SharpConsensus} map for a list of PPProps.

    Currently returns fallback sentinels (no real Pinnacle parsing).
    leg_selector checks freshness_sec and applies a staleness penalty
    automatically, so this is safe to ship — it degrades gracefully.
    """
    if not props:
        return {}

    # Determine which leagues are represented
    leagues = {p.game_id.split("_")[0] for p in props if "_" in p.game_id}

    # Fetch Pinnacle data for each league concurrently
    raw_by_league = await asyncio.gather(
        *[_fetch_pinnacle_player_props(league) for league in leagues],
        return_exceptions=True,
    )

    # Build merged lookup across leagues
    lookup: Dict[str, tuple[float, dict]] = {}
    for raw in raw_by_league:
        if isinstance(raw, dict):
            lookup.update(_parse_pinnacle_into_lookup(raw))

    # Match props to lookup entries
    sharp_map: Dict[str, SharpConsensus] = {}
    for prop in props:
        line = list(prop.lines.values())[0]
        key = f"{prop.player_name.lower().replace(' ', '')}|{prop.stat_type}|{line}"

        if key in lookup:
            median, shape = lookup[key]
            sharp_map[prop.prop_id] = SharpConsensus(
                prop_id=prop.prop_id,
                median=median,
                shape_params=shape,
                timestamp=datetime.now(timezone.utc).isoformat(),
                books_used=["Pinnacle"],
                freshness_sec=30.0,
            )
        else:
            sharp_map[prop.prop_id] = _make_fallback(prop)

    return sharp_map
