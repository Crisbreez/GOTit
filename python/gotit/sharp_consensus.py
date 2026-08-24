"""
sharp_consensus.py — Real SharpConsensus from SportsGameOdds fairOdds.

SportsGameOdds already computes de-vigged fair odds (`fairOdds`) and a fair
line (`fairOverUnder`) for each player prop. This is better than raw Pinnacle
XML — SGO aggregates multiple sharp books and removes vig for us.

Key fields we use:
    fairOdds        — American odds string (e.g. "+113", "-131")
                      converted → p_win_over = 1 / (1 + 1.13) = 0.469
    fairOverUnder   — The fair/sharp consensus line number (e.g. "4.5")
                      used as synthetic_median in the distribution model
    playerID        — e.g. "BRYCE_ELDER_1_MLB"
    players[id].name — "Bryce Elder"  (matched to PP playerName)
    statID          — e.g. "pitching_strikeouts"  (mapped to PP stat_type)

Matching strategy:
    PP prop → SGO prop via (normalized_player_name, mapped_stat_type)
    Returns SharpConsensus keyed by PP prop_id.
    Unmatched props get a tier-aware fallback (no real sharp data).

Store:
    Results cached to python/config/sharp_store.json
    Refreshed on each /api/pull call for the same league.
"""
from __future__ import annotations

import json
import logging
import math
import re
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    import httpx
    _HAS_HTTPX = True
except ImportError:
    _HAS_HTTPX = False

try:
    import requests as _requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

# PPProp, SharpConsensus, Tier — defined inline (leg_selector was rewritten)
from dataclasses import dataclass, field as dc_field

class Tier:
    STANDARD = "standard"
    GOBLIN   = "goblin"
    DEMON    = "demon"

@dataclass
class PPProp:
    prop_id:       str
    player_name:   str
    stat_type:     str
    lines:         dict   # {tier: line_score}
    tiers_offered: list

@dataclass
class SharpConsensus:
    prop_id:          str
    median:           float
    shape_params:     dict
    timestamp:        str
    books_used:       list
    freshness_sec:    float  = 9999.0
    # Direct de-vigged probabilities from DK/FD (None = not available)
    fair_p_win_over:  Optional[float] = None
    fair_p_win_under: Optional[float] = None

log = logging.getLogger("gotit.sharp_consensus")

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────
SGO_BASE = "https://api.sportsgameodds.com/v2"
SGO_KEY  = "cdcc1cf23a44326b7e4d190616787462"


# ─────────────────────────────────────────────────────────────────────────────
# Match PP props → SGO props
# ─────────────────────────────────────────────────────────────────────────────
def _match_props(
    pp_props: List[PPProp],
    sgo_props: List[dict],
) -> Dict[str, SharpConsensus]:
    """
    Match each PP prop to the best SGO prop by:
      1. Normalized player name (exact)
      2. PP stat type == SGO pp_stat_type
      3. Line proximity: |pp_line - sgo_fair_line| ≤ 1.5

    Special handling for Strikeouts: batting_strikeouts (line≤2.5) and
    pitching_strikeouts (line≥3.0) both map to PP "Strikeouts" but have
    completely different medians. We partition the SGO pool by line range
    so a PP pitcher Ks prop (line=4.5) only matches SGO pitcher Ks props.

    Returns dict: prop_id → SharpConsensus
    """
    # Build SGO lookup: (norm_name, stat_type_pp) → list of sgo props
    # stat_type_pp already uses PP-exact names ("Hitter Strikeouts", "Pitcher Strikeouts",
    # "Hitter Fantasy Score") so no bucket disambiguation needed anymore.
    sgo_lookup: Dict[Tuple[str, str], List[dict]] = {}
    for sp in sgo_props:
        key = (_normalize_name(sp["player_name"]), sp["stat_type_pp"])
        sgo_lookup.setdefault(key, []).append(sp)

    result: Dict[str, SharpConsensus] = {}

    for pp in pp_props:
        # Get the prop's line (first tier offered)
        tier = pp.tiers_offered[0] if pp.tiers_offered else Tier.STANDARD
        pp_line = pp.lines.get(tier, list(pp.lines.values())[0])

        norm_name = _normalize_name(pp.player_name)
        key = (norm_name, pp.stat_type)
        candidates = sgo_lookup.get(key, [])

        # Line proximity tolerance:
        #   - fantasy score stats: wider (SGO uses ~half the PP scale)
        #   - all others: 1.5 for main-line props, but PP also posts alt-line props
        #     at much higher/lower lines for the same stat. When no candidate is
        #     within 1.5, fall back to the nearest SGO line anyway (use it as the
        #     sharp median anchor; the PP alt-line is still bet against that median).
        line_tol = _FANTASY_SCORE_LINE_TOL if pp.stat_type in _FANTASY_SCORE_STATS else _DEFAULT_LINE_TOL

        # Find closest line match
        best: Optional[dict] = None
        best_diff = float("inf")
        for sp in candidates:
            diff = abs(sp["fair_line"] - pp_line)
            if diff < best_diff:
                best_diff = diff
                best = sp

        # Accept within tolerance OR accept the nearest SGO entry if it exists
        # (alt-line props: PP line may be far from the main SGO line, but the
        # SGO sharp median is still the best available oracle for this player/stat).
        use_best = best and (best_diff <= line_tol or (best_diff <= 8.0 and len(candidates) > 0))
        if use_best:
            # Real sharp data.
            # fair_p_win_over/under: direct de-vigged probability from DK+FD books.
            # This is the ground truth — use it directly in the blend instead of
            # re-deriving via CDF. When the PP line != book line (alt-line demons)
            # we still store the de-vigged p at the book's line as a directional signal.
            result[pp.prop_id] = SharpConsensus(
                prop_id=pp.prop_id,
                median=best["fair_line"],   # sharp consensus line → CDF anchor
                shape_params={},
                timestamp=best["fetched_at"],
                books_used=best["books_used"],
                freshness_sec=0.0,
                fair_p_win_over=best.get("fair_p_win_over"),
                fair_p_win_under=best.get("fair_p_win_under"),
            )
            log.debug(
                f"[sharp] matched {pp.player_name} {pp.stat_type} "
                f"PP_line={pp_line} SGO_line={best['fair_line']} "
                f"SGO_fair_line={best['fair_line']}"
            )
        else:
            # Fallback: calibration tier-delta (name not in SGO or stat not covered)
            result[pp.prop_id] = _fallback_sc(pp.prop_id, pp_line, tier, pp.stat_type)
            log.debug(f"[sharp] no SGO data for {pp.player_name} {pp.stat_type} line={pp_line:.1f}")

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Store read/write
# ─────────────────────────────────────────────────────────────────────────────
def _load_store() -> Dict[str, dict]:
    """Load sharp_store.json from disk. Returns {} if missing/corrupt."""
    if STORE_PATH.exists():
        try:
            with open(STORE_PATH) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_store(store: Dict[str, dict]) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(STORE_PATH, "w") as f:
        json.dump(store, f, indent=2)


def _sc_to_dict(sc: SharpConsensus) -> dict:
    return {
        "prop_id":           sc.prop_id,
        "median":            sc.median,
        "shape_params":      sc.shape_params,
        "timestamp":         sc.timestamp,
        "books_used":        sc.books_used,
        "freshness_sec":     sc.freshness_sec,
        "fair_p_win_over":   sc.fair_p_win_over,
        "fair_p_win_under":  sc.fair_p_win_under,
    }


def _sc_from_dict(d: dict) -> SharpConsensus:
    return SharpConsensus(
        prop_id=d["prop_id"],
        median=d["median"],
        shape_params=d.get("shape_params", {}),
        timestamp=d["timestamp"],
        books_used=d.get("books_used", []),
        freshness_sec=d.get("freshness_sec", FALLBACK_SEC),
        fair_p_win_over=d.get("fair_p_win_over"),
        fair_p_win_under=d.get("fair_p_win_under"),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────
def pull_sharp_consensus(league: str, pp_props: List[PPProp]) -> Dict[str, SharpConsensus]:
    """
    Fetch fresh SGO sharp data for a league, match to PP props, persist to disk.
    Called by the /api/pull route (same time as PP prop pull).

    Returns: prop_id → SharpConsensus
    """
    log.info(f"[sharp] pulling SGO for {league} ({len(pp_props)} PP props)")

    # Sharp source: SportsGameOdds (Pinnacle/Bovada)
    ml_props = _fetch_sgo_props(league)
    consensus = _match_props(pp_props, ml_props)

    # Merge into persistent store
    store = _load_store()
    for prop_id, sc in consensus.items():
        store[prop_id] = _sc_to_dict(sc)

    _save_store(store)

    matched = sum(1 for sc in consensus.values() if sc.freshness_sec == 0.0)
    # Per-stat match breakdown printed to stderr so Express logs see it
    import sys as _sys
    print(f"[sharp] {league}: {matched}/{len(pp_props)} props matched to real SGO data",
          file=_sys.stderr)
    # Sample unmatched for debugging
    unmatched = [pp.player_name + " " + pp.stat_type
                 for pp in pp_props
                 if consensus.get(pp.prop_id) and consensus[pp.prop_id].freshness_sec >= 9999.0][:10]
    if unmatched:
        print(f"[sharp] first unmatched: {unmatched}", file=_sys.stderr)
    return consensus


def load_sharp_consensus(pp_props: List[PPProp]) -> Dict[str, SharpConsensus]:
    """
    Load sharp consensus from disk store.
    For any PP prop not in the store (or stale), returns a tier-aware fallback.

    Called by optimize.py — does NOT fetch from network.
    """
    store = _load_store()
    now_ts = time.time()

    result: Dict[str, SharpConsensus] = {}
    for pp in pp_props:
        tier = pp.tiers_offered[0] if pp.tiers_offered else Tier.STANDARD
        pp_line = pp.lines.get(tier, list(pp.lines.values())[0])

        stored = store.get(pp.prop_id)
        if stored:
            sc = _sc_from_dict(stored)
            # Check freshness: if stored timestamp is recent, use it
            try:
                stored_ts = datetime.fromisoformat(sc.timestamp.replace("Z", "+00:00")).timestamp()
                age_sec = now_ts - stored_ts
                if age_sec < FRESH_SEC:
                    result[pp.prop_id] = sc
                    continue
            except Exception:
                pass

        # Fallback
        result[pp.prop_id] = _fallback_sc(pp.prop_id, pp_line, tier, pp.stat_type)

    fresh_count = sum(1 for sc in result.values() if sc.freshness_sec < FALLBACK_SEC)
    log.info(f"[sharp] loaded {fresh_count}/{len(pp_props)} fresh sharp entries from store")
    return result
