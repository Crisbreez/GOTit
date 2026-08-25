#!/usr/bin/env python3
"""
run_matchup_fit.py — standalone daily matchup fit pull.

Reads today's active MLB props straight from Supabase, pulls platoon splits
(vs-L / vs-R) for every batter vs tonight's probable pitcher via the MLB
Stats API, writes one row per player/game into matchup_fit_scores, and
stamps matchup_fit_score onto each matching props row so scoring and the
Analysis view read real values.

Invoked two ways:
  1. Daily scheduler in server/index.ts (~8:30am ET)
  2. Manually: python3 python/run_matchup_fit.py
"""
import json
import logging
import os
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gotit.matchup_fit import pull_matchup_fits, _normalize, SUPABASE_URL, SUPABASE_KEY  # noqa: E402

log = logging.getLogger("run_matchup_fit")
logging.basicConfig(level=logging.INFO, format="%(message)s")

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}


def _get(path: str):
    req = urllib.request.Request(f"{SUPABASE_URL}/rest/v1/{path}", headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _patch(path: str, body: dict) -> None:
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/{path}",
        data=json.dumps(body).encode(),
        headers={**HEADERS, "Prefer": "return=minimal"},
        method="PATCH",
    )
    urllib.request.urlopen(req, timeout=30).read()


def fetch_active_props() -> list:
    """Fetch all active MLB props from Supabase (paged past the 1000 cap)."""
    props, offset = [], 0
    while True:
        page = _get(
            "props?select=id,player_name,stat_type,team_abbr,line_score,"
            "is_demon,is_goblin,game_id,game_matchup"
            f"&league=eq.MLB&limit=1000&offset={offset}"
        )
        props.extend(page)
        if len(page) < 1000:
            break
        offset += 1000
    # Shape to what pull_matchup_fits expects
    return [
        {
            "id": p["id"],
            "playerName": p["player_name"],
            "statType": p["stat_type"],
            "teamAbbr": p.get("team_abbr"),
            "gameId": p.get("game_id"),
            "gameMatchup": p.get("game_matchup"),
        }
        for p in props
    ]


def main() -> int:
    props = fetch_active_props()
    log.info("[run_matchup_fit] %d active MLB props", len(props))
    if not props:
        print(json.dumps({"ok": True, "props": 0, "fits": 0, "stamped": 0}))
        return 0

    fits = pull_matchup_fits(props, "MLB")
    log.info("[run_matchup_fit] %d batter fits computed", len(fits))

    # Stamp matchup_fit_score onto props — one PATCH per batter (covers all
    # of that batter's prop rows in a single call)
    stamped = 0
    seen_players = set()
    for p in props:
        norm = _normalize(p["playerName"])
        if norm in seen_players:
            continue
        fit = fits.get(norm)
        if not fit or fit.get("matchup_fit_score") is None:
            continue
        seen_players.add(norm)
        try:
            _patch(
                f"props?player_name=eq.{urllib.parse.quote(p['playerName'])}&league=eq.MLB",
                {"matchup_fit_score": fit["matchup_fit_score"]},
            )
            stamped += 1
        except Exception as e:
            log.warning("[run_matchup_fit] stamp failed for %s: %s", p["playerName"], e)

    print(json.dumps({"ok": True, "props": len(props), "fits": len(fits), "stamped": stamped}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
