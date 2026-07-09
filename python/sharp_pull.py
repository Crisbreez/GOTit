#!/usr/bin/env python3
"""
sharp_pull.py — Subprocess called by Express after /api/pull completes.

Input  (stdin): JSON { "league": "MLB", "props": [...webapp prop dicts...] }
Output (stdout): JSON { "ok": true, "matched": N, "total": M, "league": "MLB" }
                 or   { "ok": false, "error": "..." }

Express calls this with:
    python3 python/sharp_pull.py < {"league":..., "props":[...]}
"""
import sys
import json
import logging
from pathlib import Path

logging.basicConfig(level=logging.WARNING)
sys.path.insert(0, str(Path(__file__).parent))

from gotit.sharp_consensus import pull_sharp_consensus
from optimize import build_pp_prop


def main():
    raw = sys.stdin.read().strip()
    if not raw:
        print(json.dumps({"ok": False, "error": "empty input"}))
        sys.exit(1)

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": f"json parse: {e}"}))
        sys.exit(1)

    league = payload.get("league", "MLB").upper()
    props_data = payload.get("props", [])

    if not props_data:
        print(json.dumps({"ok": False, "error": "no props in payload"}))
        sys.exit(1)

    # Convert to PPProp objects for matching
    pp_props = []
    for d in props_data:
        try:
            pp_props.append(build_pp_prop(d))
        except Exception as e:
            logging.warning(f"skip prop {d.get('playerName','?')}: {e}")

    if not pp_props:
        print(json.dumps({"ok": False, "error": "no valid PPProps"}))
        sys.exit(1)

    # Pull sharp consensus from SGO, write to sharp_store.json
    try:
        consensus = pull_sharp_consensus(league, pp_props)
    except Exception as e:
        print(json.dumps({"ok": False, "error": str(e)}))
        sys.exit(1)

    matched = sum(
        1 for sc in consensus.values()
        if sc.freshness_sec < 9999.0
    )

    print(json.dumps({
        "ok": True,
        "league": league,
        "matched": matched,
        "total": len(pp_props),
    }))


if __name__ == "__main__":
    main()
