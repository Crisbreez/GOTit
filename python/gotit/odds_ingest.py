"""
odds_ingest.py — Fetch PrizePicks projections board and convert to PPProp objects.
Uses the partner API endpoint which works from any IP (no auth required).
"""
import asyncio
import time
from datetime import datetime, timezone
from typing import List

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from gotit.leg_selector import PPProp, Tier

# Partner API — no auth, works from datacenter IPs
PP_API = "https://partner-api.prizepicks.com/projections"

HEADERS = {
    "Accept": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}

# PP stat_type (from attributes.stat_type) → canonical internal label
STAT_MAP = {
    # MLB
    "Hits":                    "Hits",
    "Strikeouts":              "Strikeouts",
    "Pitcher Strikeouts":      "Strikeouts",
    "Total Bases":             "Total Bases",
    "Hits+Runs+RBIs":          "Hits+Runs+RBIs",
    "Home Runs":               "Home Runs",
    "RBIs":                    "RBIs",
    "Walks":                   "Walks",
    "Walks Allowed":           "Walks Allowed",
    "Hitter Fantasy Score":    "Fantasy Score",
    "Pitcher Fantasy Score":   "Fantasy Score",
    "Earned Runs Allowed":     "Earned Runs Allowed",
    "Innings Pitched":         "Innings Pitched",
    # NBA
    "Points":                  "Points",
    "Rebounds":                "Rebounds",
    "Assists":                 "Assists",
    "Blocked Shots":           "Blocked Shots",
    "Steals":                  "Steals",
    "3-Pt Made":               "3-Pt Made",
    "Pts+Reb+Ast":             "Pts+Reb+Ast",
    "Fantasy Score":           "Fantasy Score",
    # NFL
    "Passing Yards":           "Passing Yards",
    "Rushing Yards":           "Rushing Yards",
    "Receiving Yards":         "Receiving Yards",
    "Receptions":              "Receptions",
    "Touchdowns":              "Touchdowns",
    "Pass Attempts":           "Pass Attempts",
    "Pass Completions":        "Pass Completions",
    # MMA
    "Significant Strikes":     "Significant Strikes",
    "Takedowns":               "Takedowns",
    "Total Strikes":           "Total Strikes",
}

TIER_MAP = {
    "standard": [Tier.STANDARD],
    "goblin":   [Tier.GOBLIN],
    "demon":    [Tier.DEMON],
}

ETAG_CACHE: dict[str, str] = {}
_last_call: float = 0.0
_MIN_INTERVAL = 3.0   # seconds between calls — respect rate limits


@retry(
    wait=wait_exponential_jitter(initial=2, max=30),
    stop=stop_after_attempt(3),
    retry=retry_if_exception_type(httpx.HTTPStatusError),
)
async def _safe_get(
    client: httpx.AsyncClient,
    url: str,
    params: dict,
    extra_headers: dict,
) -> httpx.Response:
    global _last_call
    # Throttle — wait if we called recently
    elapsed = time.monotonic() - _last_call
    if elapsed < _MIN_INTERVAL:
        await asyncio.sleep(_MIN_INTERVAL - elapsed)

    r = await client.get(url, params=params, headers=extra_headers)
    _last_call = time.monotonic()

    if r.status_code == 429:
        retry_after = int(r.headers.get("Retry-After", "10"))
        await asyncio.sleep(retry_after)
        raise httpx.HTTPStatusError("rate limited", request=r.request, response=r)
    r.raise_for_status()
    return r


async def fetch_board(league_id: int | None = None) -> List[PPProp]:
    """
    Fetch the PrizePicks projections board.
    Returns [] if board is unchanged (304 ETag hit).
    League IDs: MLB=2, NBA=7, NFL=1, MMA=12
    """
    etag_key = f"projections_{league_id or 'all'}"
    extra_headers = {"If-None-Match": ETAG_CACHE.get(etag_key, "")}

    params: dict = {"per_page": 2000}
    if league_id is not None:
        params["league_id"] = league_id

    async with httpx.AsyncClient(timeout=20.0, headers=HEADERS) as client:
        r = await _safe_get(client, PP_API, params, extra_headers)

    if r.status_code == 304:
        return []

    ETAG_CACHE[etag_key] = r.headers.get("ETag", "")
    data = r.json()

    if "error" in data:
        raise RuntimeError(f"PrizePicks API error: {data['error']}")

    # Build lookup maps from included objects
    players: dict[str, dict] = {}
    games:   dict[str, dict] = {}
    for inc in data.get("included", []):
        t = inc.get("type")
        if t == "new_player":
            players[inc["id"]] = inc
        elif t == "game":
            games[inc["id"]] = inc

    props: List[PPProp] = []
    seen: set[str] = set()

    for proj in data.get("data", []):
        a   = proj.get("attributes", {})
        pid = proj["id"]

        # Only pre-game props
        if a.get("status") not in ("pre_game", None, ""):
            continue

        # Resolve player
        player_rel = proj["relationships"].get("new_player", {}).get("data")
        if not player_rel:
            continue
        player = players.get(player_rel["id"])
        if not player:
            continue

        # Resolve stat
        raw_stat = a.get("stat_type", "")
        stat = STAT_MAP.get(raw_stat)
        if not stat:
            continue

        # Resolve tier
        odds_type = (a.get("odds_type") or "standard").lower()
        tiers = TIER_MAP.get(odds_type, [Tier.STANDARD])

        line = float(a["line_score"])

        # Parse start_time — PP uses local offset, convert to UTC
        raw_start = a.get("start_time") or a.get("board_time", "")
        try:
            start = datetime.fromisoformat(raw_start)
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
            start_utc = start.astimezone(timezone.utc)
        except (ValueError, AttributeError):
            start_utc = datetime.now(timezone.utc)

        hours_to_lock = max(
            0.0,
            (start_utc - datetime.now(timezone.utc)).total_seconds() / 3600,
        )

        # Build game_id from PP game_id field (stable across pulls)
        pp_game_id = a.get("game_id", "")
        pa = player["attributes"]
        team = pa.get("team", "UNK")
        league_label = pa.get("league", f"L{league_id or 0}")
        game_id = pp_game_id or f"{league_label}_{start_utc.date()}_{team}"

        # Dedup: one PPProp per (prop_id + odds_type)
        dedup_key = f"{pid}_{odds_type}"
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        props.append(
            PPProp(
                prop_id=pid,
                game_id=game_id,
                player_id=player_rel["id"],
                player_name=pa.get("name") or pa.get("display_name", "Unknown"),
                stat_type=stat,
                tiers_offered=tiers,
                lines={t: line for t in tiers},
                hours_to_lock=hours_to_lock,
                public_over_pct=None,
                dnp_prob=0.0,
                correlation_partners=[],
            )
        )

    return props
