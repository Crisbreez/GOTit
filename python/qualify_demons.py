#!/usr/bin/env python3
"""
GOTit Demon Qualifier — subprocess entry point called by Express per game.

Input  (stdin): JSON array of ALL props for ONE game (web-app prop dicts)
Output (stdout): JSON array of top-1 qualified demon prop dict (max 1),
                 with demonScore attached. Empty if none survive.

Wires directly into demon_pipeline.run_demon_pipeline — all 5 gates:
  1. market_pass       — PP line ≤ sharp consensus + tolerance
  2. probability_pass  — estimated p_win >= 0.62
  3. role_pass         — player role is locked and stable
  4. matchup_pass      — opponent/game environment supports the stat
  5. normal_path_pass  — stat is reachable through ordinary volume

If any gate fails, the Demon is rejected and the reason is logged.
MILP only sees survivors.
"""
from __future__ import annotations
import sys, json, logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
sys.path.insert(0, str(Path(__file__).parent))

from gotit.demon_pipeline import run_demon_pipeline


def main() -> None:
    raw = sys.stdin.read().strip()
    if not raw:
        print(json.dumps([]))
        sys.exit(0)

    try:
        props_data = json.loads(raw)
    except Exception as e:
        print(json.dumps({'error': str(e)}))
        sys.exit(1)

    # Build sharp_map from any sharpFairLine fields present on props
    sharp_map = {}
    for d in props_data:
        prop_id = str(d.get('propId') or d.get('id') or d.get('sourcePropId') or '')
        if prop_id and (d.get('sharpFairLine') or d.get('sharp_fair_line')):
            sharp_map[prop_id] = {
                'fair_line': float(d.get('sharpFairLine') or d.get('sharp_fair_line'))
            }

    result = run_demon_pipeline(
        raw_props=props_data,
        sharp_map=sharp_map,
        max_demons=1,   # MILP enforces max 1 demon per slip
    )

    print(json.dumps(result))


if __name__ == '__main__':
    main()
