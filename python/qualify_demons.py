#!/usr/bin/env python3
"""
GOTit Demon Qualifier — subprocess entry point called by Express per game.

Input  (stdin): JSON array of ALL props for ONE game (web-app prop dicts)
Output (stdout): JSON object:
  {
    "selected_demons":        [...],   // top-2 survivors after pipeline + post_relaxation fallback
    "post_relaxation_demons": [...],   // ALL demons that survived any relaxation tier
    "trace":                  {...},   // full pipeline_trace from 8 stages
    "error":                  null     // non-null if pipeline crashed
  }

Route pattern (in routes.ts):
  pipeline = runDemonPipeline(game)
  selected = pipeline.selected_demons
  if selected.empty and pipeline.post_relaxation_demons.not_empty:
      selected = top2(pipeline.post_relaxation_demons)
  game.demons = selected
  game.demon_pipeline_trace = pipeline.trace
"""
from __future__ import annotations
import sys, json, logging, os
from pathlib import Path

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
sys.path.insert(0, str(Path(__file__).parent))

from gotit.demon_pipeline import run_demon_pipeline, run_demon_pipeline_full


def main() -> None:
    raw = sys.stdin.read().strip()
    if not raw:
        print(json.dumps({
            'selected_demons': [],
            'post_relaxation_demons': [],
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
            'trace': {},
            'error': str(e),
        }))
        sys.exit(1)

    # Build sharp_map from any sharpFairLine fields on props
    sharp_map = {}
    for d in props_data:
        prop_id = str(d.get('propId') or d.get('id') or d.get('sourcePropId') or '')
        if prop_id and (d.get('sharpFairLine') or d.get('sharp_fair_line')):
            sharp_map[prop_id] = {
                'fair_line': float(d.get('sharpFairLine') or d.get('sharp_fair_line'))
            }

    bypass_test = os.environ.get('DEMON_BYPASS_TEST', '').strip() == '1'

    pipeline = run_demon_pipeline_full(
        raw_props=props_data,
        sharp_map=sharp_map,
        max_demons=2,
        bypass_test=bypass_test,
    )

    print(json.dumps({
        'selected_demons':        pipeline['selected_demons'],
        'post_relaxation_demons': pipeline['post_relaxation_demons'],
        'trace':                  pipeline['trace'],
        'error':                  None,
    }))


if __name__ == '__main__':
    main()
