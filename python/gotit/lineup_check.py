"""
lineup_check.py — MLB lineup confirmation via MLB Stats API.

Fetches today's confirmed starters ~30 min before first pitch.
Returns a set of confirmed player IDs/names for lineup_ok stamping.

Called from sharp_pull.py at pull time — results cached for 30 min.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Optional, Set

log = logging.getLogger(__name__)

_CACHE_FILE = Path('/tmp/gotit_lineups.json')
_CACHE_TTL  = 30 * 60  # 30 minutes


def _load_cache() -> Optional[Dict]:
    try:
        if _CACHE_FILE.exists():
            data = json.loads(_CACHE_FILE.read_text())
            if time.time() - data.get('ts', 0) < _CACHE_TTL:
                return data
    except Exception:
        pass
    return None


def _save_cache(data: Dict) -> None:
    try:
        _CACHE_FILE.write_text(json.dumps(data))
    except Exception:
        pass


def fetch_confirmed_starters() -> Set[str]:
    """
    Returns a set of lowercase normalized player names confirmed in today's MLB lineups.
    Calls MLB Stats API /schedule with hydration=lineups.
    """
    cache = _load_cache()
    if cache:
        return set(cache.get('names', []))

    try:
        import urllib.request
        today = datetime.now(timezone(timedelta(hours=-5))).strftime('%Y-%m-%d')  # CT
        url = (
            f'https://statsapi.mlb.com/api/v1/schedule'
            f'?sportId=1&date={today}&hydrate=lineups'
        )
        with urllib.request.urlopen(url, timeout=10) as resp:
            data = json.loads(resp.read())

        names: Set[str] = set()
        for date_entry in data.get('dates', []):
            for game in date_entry.get('games', []):
                lineups = game.get('lineups', {})
                for side in ('homePlayers', 'awayPlayers'):
                    for player in lineups.get(side, []):
                        n = player.get('fullName', '')
                        if n:
                            names.add(_norm(n))

        _save_cache({'ts': time.time(), 'names': list(names)})
        log.info(f'[lineup_check] fetched {len(names)} confirmed starters')
        return names

    except Exception as e:
        log.warning(f'[lineup_check] fetch failed: {e}')
        return set()


def _norm(name: str) -> str:
    """Normalize player name for fuzzy match."""
    import unicodedata
    name = unicodedata.normalize('NFD', name)
    name = ''.join(c for c in name if unicodedata.category(c) != 'Mn')
    return name.lower().strip()


def is_lineup_ok(player_name: str, confirmed: Set[str]) -> bool:
    """
    Returns True if the player is in confirmed starters.
    Falls back to True (permissive) if confirmed set is empty (API unavailable).
    """
    if not confirmed:
        return True  # API unavailable — don't penalise
    return _norm(player_name) in confirmed
