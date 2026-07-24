#!/usr/bin/env python3
"""
GOTit Demon Qualifier — subprocess entry point called by Express per game.

Input  (stdin): JSON array of ALL props for ONE game (web-app prop dicts)
Output (stdout): JSON object:
  {
    "selected_demons":        [...],   // 0 or 2 survivors (LOCK pair)
    "post_relaxation_demons": [...],   // same as selected_demons
    "status":                 "CLEAR" | "NO-GO"
    "p_joint":                float,
    "rho":                    float,
    "decision":               "LOCK" | "ABORT",
    "trace":                  {...},
    "error":                  null
  }
"""
from __future__ import annotations
import sys, json, logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
sys.path.insert(0, str(Path(__file__).parent))

from gotit.demon_pipeline import run_demon_pipeline, format_output


def main() -> None:
    raw = sys.stdin.read().strip()
    if not raw:
        print(json.dumps({
            'selected_demons': [],
            'post_relaxation_demons': [],
            'status': 'NO-GO',
            'p_joint': 0.0,
            'rho': 0.0,
            'decision': 'ABORT',
            'trace': {},
            'error': None,
        }))
        sys.exit(0)

    try:
        props_data = json.loads(raw)
    except Exception as e:
        print(json.dumps({
            'selected_demons': [],
            'post_relaxation_demons': [],
            'status': 'NO-GO',
            'p_joint': 0.0,
            'rho': 0.0,
            'decision': 'ABORT',
            'trace': {},
            'error': str(e),
        }))
        sys.exit(1)

    # Derive game_id from first prop
    game_id = str(
        (props_data[0] if props_data else {}).get('gameId') or
        (props_data[0] if props_data else {}).get('game_id') or
        'unknown'
    )

    result = run_demon_pipeline(props_data, game_id)
    output = format_output(result)
    print(json.dumps(output))


if __name__ == '__main__':
    main()
