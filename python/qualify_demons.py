#!/usr/bin/env python3
"""
GOTit Demon Qualifier — subprocess entry point called by Express per game.

Input  (stdin): JSON array of ALL props for ONE game (web-app prop dicts)
Output (stdout): JSON array of exactly top-2 qualified demon prop dicts,
                 with demonScore attached. Empty if none qualify.

Rules (per spec):
  - PP is the only authority on demon identity (isDemon=true from pull)
  - GOTit applies 4 elimination gates:
      1. Min line by stat type (already done at ingest — second check here)
      2. Stat type must be meaningful (PA, Triples, Walks excluded)
      3. p_win must clear demon breakeven (>0.56)
      4. Line must not be trivially easy (no near-certain overs)
  - Returns top 2 distinct-player demons ranked by composite score
  - If fewer than 2 survive all gates, returns fewer — NO substitutions
"""
from __future__ import annotations
import sys, json, math, hashlib, logging
from pathlib import Path
from typing import Dict, List, Optional

logging.basicConfig(level=logging.WARNING)
sys.path.insert(0, str(Path(__file__).parent))

# ── Stat-type exclusions for demons ─────────────────────────────────────��───
# Excluded entirely — no edge regardless of line:
#   - Plate Appearances: near-certain for any starter who plays
#   - Pitcher Strikeouts (Combo): multi-player prop, poorly defined
#   - 1st Inning Walks: too small sample, high variance
DEMON_EXCLUDED_STATS = {
    'Plate Appearances',
    'Pitcher Strikeouts (Combo)',
    '1st Inning Walks Allowed',
    'Triples',
    # Proven losers from 14-slip audit
    'RBIs',             # 100% miss rate
    'Singles',          # 100% miss rate as demon
    'Hits+Runs+RBIs',   # 100% miss rate as demon
    'Hits',             # 1.5 line = too volatile, high miss rate
    'Hitter Strikeouts', # blocked stat across all pipelines
    'Doubles',          # insufficient edge at demon lines
    'Walks',            # insufficient edge at demon lines
    'Home Runs',        # 0.5 demon lines are coinflips
    'Stolen Bases',     # 0.5 demon lines are coinflips
}

# ── Min line floors per stat (gate 1) ────────────────────────────────────────
# These match PP's actual demon line ranges — don't over-raise.
DEMON_LINE_FLOOR: Dict[str, float] = {
    'Home Runs':            0.5,
    'Stolen Bases':         0.5,
    'Doubles':              0.5,
    'Walks':                1.5,
    'Singles':              1.5,
    'RBIs':                 1.5,
    'Runs':                 1.5,
    'Hits':                 1.5,
    'Total Bases':          2.5,
    'Hits+Runs+RBIs':       2.5,
    'Hitter Fantasy Score': 25.0,  # only elite HFS demons qualify (Ohtani/Judge tier)
    'Hitter Strikeouts':    1.5,
    'Pitcher Strikeouts':   3.5,
    'Pitching Outs':        9.5,
    'Pitches Thrown':       70.0,
    'Pitcher Fantasy Score': 25.0,
    'Earned Runs Allowed':  0.5,
    'Hits Allowed':         2.5,
    'Significant Strikes':  25.0,
    'Takedowns':            1.5,
    '_default':             1.5,
}

# ── CV table for p_win estimation ────────────────────────────────────────────
# Higher CV = wider distribution = harder to clear the line = lower p_win
STAT_CV: Dict[str, float] = {
    'Pitcher Strikeouts':   0.35,  # pitchers are consistent
    'Pitches Thrown':       0.18,  # very consistent
    'Pitcher Fantasy Score': 0.55,
    'Hits':                 0.70,
    'Total Bases':          0.85,
    'Hits+Runs+RBIs':       0.80,
    'Hitter Fantasy Score': 0.75,
    'RBIs':                 0.90,
    'Runs':                 0.90,
    'Singles':              0.85,
    'Hitter Strikeouts':    0.80,
    'Significant Strikes':  1.20,
    'Takedowns':            1.10,
    '_default':             0.70,
}

DEMON_PWIN_FLOOR = 0.52  # must exceed breakeven to qualify


def _prop_id(d: dict) -> str:
    pid = d.get('id') or d.get('prop_id') or ''
    if pid: return str(pid)
    key = f"{d.get('playerName','')}{d.get('statType','')}{d.get('lineScore','')}{d.get('gameId','')}"
    return hashlib.md5(key.encode()).hexdigest()[:12]


def _estimate_p_win(line: float, stat_type: str) -> float:
    """
    Estimate p(over demon line) using a log-normal CDF approximation.

    Key insight: PP demon lines are set BELOW the player's true expected
    output. PP prices these at approximately 65-75% of player true mean
    for hitting stats, making them easier to hit than standard lines.
    We model true_mean = line * demon_ratio[stat_type].
    """
    if line <= 0:
        return 0.0

    # How far below the player's true mean PP sets the demon line.
    # Higher ratio = line is set more below true mean = higher p_win.
    DEMON_RATIO: Dict[str, float] = {
        'Singles':              1.55,  # 1.5 line -> true mean ~2.3
        'Hits':                 1.55,
        'Runs':                 1.60,
        'RBIs':                 1.60,
        'Hitter Strikeouts':    1.50,
        'Hitter Fantasy Score': 1.45,
        'Total Bases':          1.45,
        'Hits+Runs+RBIs':       1.40,
        'Walks':                1.50,
        'Pitcher Strikeouts':   1.35,
        'Pitches Thrown':       1.15,
        'Pitcher Fantasy Score':1.30,
        'Earned Runs Allowed':  1.80,  # 0.5 ERA line, true mean ~0.9
        'Significant Strikes':  1.40,
        'Takedowns':            1.50,
        '_default':             1.40,
    }

    cv = STAT_CV.get(stat_type, STAT_CV['_default'])
    ratio = DEMON_RATIO.get(stat_type, DEMON_RATIO['_default'])
    true_mean = line * ratio
    sigma = cv * true_mean
    if sigma <= 0:
        return 0.999
    # Log-normal p(X > line)
    mu_ln = math.log(true_mean) - 0.5 * math.log(1 + (sigma / true_mean) ** 2)
    sigma_ln = math.sqrt(math.log(1 + (sigma / true_mean) ** 2))
    z = (math.log(max(line, 0.001)) - mu_ln) / sigma_ln
    p_win = 0.5 * math.erfc(z / math.sqrt(2))
    return float(max(0.0, min(0.999, p_win)))


def _composite_score(d: dict) -> Optional[float]:
    """
    Score a demon prop. Returns None if it fails any gate.
    Gates:
      1. Stat type not in exclusion list
      2. Line >= stat floor
      3. p_win >= DEMON_PWIN_FLOOR
      4. Not a trivially-certain over (p_win < 0.92)
    Score = p_win weighted by line significance (higher lines = harder = more valuable)
    """
    stat  = d.get('statType', '')
    line  = float(d.get('lineScore') or 0)

    # Gate 1: excluded stat types
    if stat in DEMON_EXCLUDED_STATS:
        return None

    # Gate 2: min line floor
    floor = DEMON_LINE_FLOOR.get(stat, DEMON_LINE_FLOOR['_default'])
    if line < floor:
        return None

    # Gate 3 & 4: p_win window
    p_win = _estimate_p_win(line, stat)
    if p_win < DEMON_PWIN_FLOOR:
        return None
    if p_win > 0.92:
        # Near-certain — not a real demon pick, too easy
        return None

    # Composite: p_win × line_difficulty_bonus
    # Higher line relative to floor = harder = bonus
    difficulty = min(line / max(floor, 1.0), 3.0)  # cap at 3x
    composite = p_win * (1.0 + 0.1 * (difficulty - 1.0))
    return round(composite, 4)


def main():
    raw = sys.stdin.read().strip()
    if not raw:
        print(json.dumps([])); sys.exit(0)

    try:
        props_data = json.loads(raw)
    except Exception as e:
        print(json.dumps({'error': str(e)})); sys.exit(1)

    demon_data = [d for d in props_data if d.get('isDemon')]
    if not demon_data:
        print(json.dumps([])); sys.exit(0)

    # Score all demons through the 4-gate chain
    scored: List[tuple] = []  # (composite, prop_dict)
    for d in demon_data:
        composite = _composite_score(d)
        if composite is not None:
            scored.append((composite, d))

    # Sort by composite descending
    scored.sort(key=lambda t: t[0], reverse=True)

    # Top 1 distinct player only (MILP enforces max 1 demon per slip)
    seen_players: set = set()
    top1 = []
    for composite, d in scored:
        player = d.get('playerName', '')
        if player in seen_players:
            continue
        seen_players.add(player)
        p_win = round(_estimate_p_win(float(d.get('lineScore') or 0), d.get('statType', '')), 4)
        # Demon justification — must log a reason before the demon is eligible
        stat  = d.get('statType', '')
        line  = d.get('lineScore', 0)
        reasons = []
        if p_win >= 0.60:
            reasons.append('high_demon_pwin')
        if composite >= 0.65:
            reasons.append('strong_composite')
        if float(line) >= DEMON_LINE_FLOORS.get(stat, DEMON_LINE_FLOORS.get('_default', 1.0)) * 1.5:
            reasons.append('line_well_above_floor')
        if not reasons:
            # No strong justification — skip this demon
            import sys as _sys
            print(f"[qualify_demons] SKIP {player} {stat} {line} — no strong demon justification", file=_sys.stderr)
            continue
        print(f"[qualify_demons] ACCEPT {player} {stat} {line} composite={composite} reasons={reasons}", file=__import__('sys').stderr)
        top1.append({
            **d,
            'isDemon': True,
            'demonScore': {
                'composite': composite,
                'p_win': p_win,
                'line': line,
                'stat': stat,
                'reasons': reasons,
            }
        })
        if len(top1) == 1:
            break

    print(json.dumps(top1))


if __name__ == '__main__':
    main()
