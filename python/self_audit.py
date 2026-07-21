#!/usr/bin/env python3
"""
GOTit Self-Audit — runs automatically after every slip settles.

What it does:
  1. Reads ALL settled legs from Supabase
  2. Detects miss patterns by stat type, line range, direction, and player
  3. Writes a JSON adjustment file (config/audit_adjustments.json)
     that optimize.py reads BEFORE scoring — dynamically suppressing
     props that GOTit has learned to lose on
  4. Logs a human-readable audit summary to Supabase (audit_log table)

This runs with zero human intervention. Every loss makes the next slip smarter.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import urllib.request
import urllib.parse
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

logging.basicConfig(level=logging.INFO, format='[self_audit] %(message)s')
log = logging.getLogger(__name__)

SUPABASE_URL = os.environ.get('SUPABASE_URL', 'https://iikjgxnjmyzlivaukabc.supabase.co')
SUPABASE_KEY = os.environ.get('SUPABASE_ANON_KEY', 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imlpa2pneG5qbXl6bGl2YXVrYWJjIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODM1NDg1NjgsImV4cCI6MjA5OTEyNDU2OH0.IFY9ocTpySWvyGXyUt615bkpwDs634T1wRUu97WbyTg')
ADJUSTMENTS_PATH = Path(__file__).parent / 'config' / 'audit_adjustments.json'

HEADERS = {
    'apikey': SUPABASE_KEY,
    'Authorization': f'Bearer {SUPABASE_KEY}',
    'Content-Type': 'application/json',
}

# ── Thresholds ────────────────────────────────────────────────────────────────
MIN_SAMPLE      = 3    # need at least N settled legs before suppressing
MISS_RATE_BLOCK = 0.70 # suppress if miss rate >= 70% with enough sample
MISS_RATE_WARN  = 0.55 # warn (score penalty) if miss rate >= 55%
LINE_BUCKET_SZ  = 1.0  # bucket line scores into 1.0-wide ranges

def _get(path: str) -> list:
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())

def _post(path: str, body: dict) -> dict:
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={**HEADERS, 'Prefer': 'return=minimal'}, method='POST')
    with urllib.request.urlopen(req, timeout=10) as r:
        return {}

def _patch(path: str, body: dict) -> dict:
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={**HEADERS, 'Prefer': 'return=minimal'}, method='PATCH')
    with urllib.request.urlopen(req, timeout=10) as r:
        return {}

# ── Load all settled legs ─────────────────────────────────────────────────────
def load_settled_legs() -> List[dict]:
    """Fetch all slip_legs that belong to settled slips."""
    # Get settled slip IDs
    slips = _get('slips?status=in.(settled_win,settled_loss)&select=id,league,status')
    slip_ids = {s['id']: s for s in slips}
    if not slip_ids:
        return []

    # Get legs for those slips
    id_list = ','.join(str(i) for i in slip_ids)
    legs = _get(f'slip_legs?slip_id=in.({id_list})&select=*')

    # Attach league to each leg
    for leg in legs:
        slip = slip_ids.get(leg['slip_id'], {})
        leg['league'] = slip.get('league', 'MLB')
        leg['slip_status'] = slip.get('status', '')

    return [l for l in legs if l['status'] in ('hit', 'miss')]

# ── Pattern analysis ──────────────────────────────────────────────────────────
def analyze(legs: List[dict]) -> dict:
    """
    Returns:
    {
      by_stat: { stat_type: { hits, misses, miss_rate } },
      by_line_bucket: { "stat_type::1.5-2.5": { hits, misses, miss_rate } },
      by_player: { "player::stat": { hits, misses, miss_rate } },
      by_league_stat: { "league::stat": { hits, misses, miss_rate } },
    }
    """
    by_stat:         Dict[str, Dict] = defaultdict(lambda: {'hits': 0, 'misses': 0})
    by_line_bucket:  Dict[str, Dict] = defaultdict(lambda: {'hits': 0, 'misses': 0})
    by_player:       Dict[str, Dict] = defaultdict(lambda: {'hits': 0, 'misses': 0})
    by_league_stat:  Dict[str, Dict] = defaultdict(lambda: {'hits': 0, 'misses': 0})

    for leg in legs:
        stat   = leg.get('stat_type', '')
        line   = float(leg.get('line_score') or 0)
        player = leg.get('player_name', '')
        league = leg.get('league', '')
        is_hit = leg['status'] == 'hit'

        # Bucket line into 1.0-wide ranges
        bucket_lo = int(line // LINE_BUCKET_SZ) * LINE_BUCKET_SZ
        bucket_hi = bucket_lo + LINE_BUCKET_SZ
        bkey = f"{stat}::{bucket_lo:.1f}-{bucket_hi:.1f}"
        pkey = f"{player}::{stat}"
        lkey = f"{league}::{stat}"

        field = 'hits' if is_hit else 'misses'
        by_stat[stat][field]            += 1
        by_line_bucket[bkey][field]     += 1
        by_player[pkey][field]          += 1
        by_league_stat[lkey][field]     += 1

    def miss_rate(d): 
        t = d['hits'] + d['misses']
        return round(d['misses'] / t, 3) if t else 0.0

    def enrich(d):
        return {k: {**v, 'total': v['hits']+v['misses'], 'miss_rate': miss_rate(v)} for k, v in d.items()}

    return {
        'by_stat':        enrich(by_stat),
        'by_line_bucket': enrich(by_line_bucket),
        'by_player':      enrich(by_player),
        'by_league_stat': enrich(by_league_stat),
    }

# ── Generate adjustments ──────────────────────────────────────────────────────
def generate_adjustments(patterns: dict) -> dict:
    """
    Returns an adjustments dict that optimize.py reads before scoring.

    Format:
    {
      "blocked_stats": ["stat_type", ...],           # drop these entirely
      "penalized_stats": {"stat_type": 0.85, ...},   # multiply p_win by factor
      "blocked_line_buckets": ["stat::lo-hi", ...],  # drop specific line ranges
      "blocked_players": ["player::stat", ...],      # drop specific player+stat combos
      "stat_floor_overrides": {"stat_type": 3.5, ...}, # raise line floor dynamically
      "generated_at": "ISO timestamp",
      "summary": [...]
    }
    """
    adjustments = {
        'blocked_stats':        [],
        'penalized_stats':      {},
        'blocked_line_buckets': [],
        'blocked_players':      [],
        'stat_floor_overrides': {},
        'generated_at':         datetime.now(timezone.utc).isoformat(),
        'summary':              [],
    }

    summary = adjustments['summary']

    # ── By stat type ──────────────────────────────────────────────────────────
    for stat, data in patterns['by_stat'].items():
        n, mr = data['total'], data['miss_rate']
        if n < MIN_SAMPLE:
            continue
        if mr >= MISS_RATE_BLOCK:
            adjustments['blocked_stats'].append(stat)
            summary.append(f"BLOCK stat={stat} miss_rate={mr:.0%} n={n}")
        elif mr >= MISS_RATE_WARN:
            # Penalize: reduce effective p_win proportionally
            penalty = round(1.0 - (mr - MISS_RATE_WARN) * 1.5, 2)
            adjustments['penalized_stats'][stat] = max(penalty, 0.70)
            summary.append(f"PENALIZE stat={stat} miss_rate={mr:.0%} n={n} factor={adjustments['penalized_stats'][stat]}")

    # ── By line bucket ────────────────────────────────────────────────────────
    for bkey, data in patterns['by_line_bucket'].items():
        n, mr = data['total'], data['miss_rate']
        if n < MIN_SAMPLE:
            continue
        if mr >= MISS_RATE_BLOCK:
            adjustments['blocked_line_buckets'].append(bkey)
            # Also raise the floor for that stat past the bucket's high end
            stat = bkey.split('::')[0]
            try:
                bucket_hi = float(bkey.split('::')[1].split('-')[1])
                current_floor = adjustments['stat_floor_overrides'].get(stat, 0.0)
                if bucket_hi > current_floor:
                    adjustments['stat_floor_overrides'][stat] = bucket_hi
            except Exception:
                pass
            summary.append(f"BLOCK line_bucket={bkey} miss_rate={mr:.0%} n={n}")

    # ── By player + stat ──────────────────────────────────────────────────────
    for pkey, data in patterns['by_player'].items():
        n, mr = data['total'], data['miss_rate']
        if n < MIN_SAMPLE:
            continue
        if mr >= MISS_RATE_BLOCK:
            adjustments['blocked_players'].append(pkey)
            summary.append(f"BLOCK player={pkey} miss_rate={mr:.0%} n={n}")

    return adjustments

# ── Write adjustments file ────────────────────────────────────────────────────
def write_adjustments(adj: dict) -> None:
    ADJUSTMENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(ADJUSTMENTS_PATH, 'w') as f:
        json.dump(adj, f, indent=2)
    log.info(f"Wrote {ADJUSTMENTS_PATH}")

# ── Log to Supabase audit_log table ──────────────────────────────────────────
def log_to_supabase(adj: dict, patterns: dict) -> None:
    """Write a summary row to audit_log so the Results > Script Audit tab can show it."""
    try:
        summary_text = '\n'.join(adj['summary']) if adj['summary'] else 'No adjustments needed'
        row = {
            'generated_at':   adj['generated_at'],
            'blocked_stats':  json.dumps(adj['blocked_stats']),
            'penalized_stats': json.dumps(adj['penalized_stats']),
            'blocked_players': json.dumps(adj['blocked_players']),
            'floor_overrides': json.dumps(adj['stat_floor_overrides']),
            'summary':         summary_text,
            'total_legs_analyzed': sum(d['total'] for d in patterns['by_stat'].values()),
        }
        _post('audit_log', row)
    except Exception as e:
        log.warning(f"Could not log to audit_log: {e}")

# ── Main ──────────────────────────────────────────────────────────────────────
def run_audit() -> dict:
    log.info("Starting self-audit…")

    try:
        legs = load_settled_legs()
    except Exception as e:
        log.error(f"Could not load settled legs: {e}")
        return {}

    if not legs:
        log.info("No settled legs yet — nothing to audit")
        return {}

    log.info(f"Loaded {len(legs)} settled legs")
    patterns   = analyze(legs)
    adj        = generate_adjustments(patterns)

    log.info(f"Adjustments: {len(adj['blocked_stats'])} blocked stats, "
             f"{len(adj['penalized_stats'])} penalized, "
             f"{len(adj['blocked_line_buckets'])} blocked buckets, "
             f"{len(adj['blocked_players'])} blocked players")

    for line in adj['summary']:
        log.info(line)

    write_adjustments(adj)
    log_to_supabase(adj, patterns)

    return adj

if __name__ == '__main__':
    adj = run_audit()
    print(json.dumps(adj, indent=2))
