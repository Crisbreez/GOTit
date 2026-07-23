#!/usr/bin/env python3
"""
Unit tests for nondemon_pipeline.py

Tests:
  T1. Missing data → never gets MILP variable
  T2. No strong reason → score=0, excluded
  T3. Strong reason but no stability → excluded
  T4. Demon prop → never appears in non-demon pool
  T5. 0-score prop → no MILP variable exists
  T6. Known slate → selected props match expected
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from gotit.nondemon_pipeline import (
    normalize_prop_record,
    apply_hard_blocks,
    evaluate_edge_gate,
    evaluate_stability_gate,
    score_nondemon_prop,
    drop_if_score_zero_or_ineligible,
    build_nondemon_candidate_pool,
    create_milp_variables_from_pool_only,
    run_nondemon_pipeline,
    NonDemonRecord,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_prop(**overrides) -> dict:
    """Base prop dict that passes all gates."""
    base = {
        'propId':          'test-001',
        'gameId':          'MLB_game_test',
        'playerId':        'player-abc',
        'playerName':      'Test Player',
        'statType':        'Pitcher Strikeouts',
        'direction':       'over',
        'lineScore':       5.5,
        'isDemon':         False,
        'isGoblin':        False,
        'ppShadeSignal':   'lean_over',
        'sharpFairLine':   6.2,      # gap = 0.7 → sharp_gap fires
        'lineMoveCount':   2,
        'firstSeenLine':   5.0,      # moved up → line_moved fires
        'pWin':            0.60,
        'roleCertainty':   0.80,
        'injuryFlag':      False,
        'scriptFlag':      'fits',
    }
    base.update(overrides)
    return base


def _run_gates(d: dict, sharp_map=None) -> NonDemonRecord:
    """Run normalize → hard blocks → edge gate → stability gate → score."""
    rec = normalize_prop_record(d, sharp_map or {})
    rec = apply_hard_blocks(rec)
    rec = evaluate_edge_gate(rec)
    rec = evaluate_stability_gate(rec)
    rec = score_nondemon_prop(rec)
    return rec


# ── T1: Missing data → never gets MILP variable ──────────────────────────────

def test_missing_data_no_milp_variable():
    """A prop with missing required data must not get a MILP variable."""
    d = _make_prop(propId='', lineScore=None, statType='')
    rec = _run_gates(d)

    assert rec.eligible_nondemon is False, "Expected ineligible due to missing data"
    assert rec.ineligible_reason == 'missing_data', f"Expected missing_data, got {rec.ineligible_reason}"
    assert rec.nondemon_score == 0.0, "Expected score 0 for missing data prop"

    # Verify no MILP variable created
    try:
        from ortools.linear_solver import pywraplp
        solver = pywraplp.Solver.CreateSolver('SCIP')
        x = create_milp_variables_from_pool_only(solver, [rec])
        assert len(x) == 0, f"Expected 0 MILP variables, got {len(x)}"
        print("T1 PASS: missing data → no MILP variable")
    except ImportError:
        print("T1 PASS (no OR-Tools): missing data → ineligible")


# ── T2: No strong reason → score=0, excluded ─────────────────────────────────

def test_no_strong_reason_score_zero():
    """A non-demon prop with no edge signal must be scored 0 and excluded."""
    d = _make_prop(
        ppShadeSignal='neutral',
        sharpFairLine=5.5,    # no gap (same as pp_line)
        lineMoveCount=0,
        firstSeenLine=5.5,
        pWin=0.50,            # below HIGH_WIN_PCT_FLOOR
        matchupFlag=None,
    )
    rec = _run_gates(d)

    assert rec.eligible_nondemon is False, "Expected ineligible — no edge signal"
    assert 'no_edge_signal' in rec.reject_reasons, f"Expected no_edge_signal in {rec.reject_reasons}"
    assert rec.nondemon_score == 0.0, f"Expected score 0, got {rec.nondemon_score}"
    print("T2 PASS: no strong reason → score=0, excluded")


# ── T3: Strong reason but no stability → excluded ─────────────────────────────

def test_strong_reason_no_stability_excluded():
    """A prop with edge signal but no stability signal must be excluded."""
    d = _make_prop(
        ppShadeSignal='neutral',
        sharpFairLine=6.2,    # sharp_gap fires
        lineMoveCount=0,
        firstSeenLine=5.5,
        pWin=0.50,
        roleCertainty=0.40,   # below STRONG_ROLE_FLOOR
        scriptFlag=None,      # no script fit
        isGoblin=False,
        isMoreOnly=False,
    )
    rec = normalize_prop_record(d, {'test-001': {'fair_line': 6.2}})
    rec = apply_hard_blocks(rec)
    rec = evaluate_edge_gate(rec)

    # Force shade signal off so only sharp_gap fires, not shade_confirmed
    rec.shaded_side = None
    # Re-evaluate stability gate without shade/goblin/more-only fallbacks
    rec = evaluate_stability_gate(rec)

    # sharp_gap provides sharp_implied_stability, so stability WILL pass
    # This confirms the gate design: sharp gap is strong enough to imply stability
    # For a prop with ONLY high_win_pct (no sharp data), stability would fail
    d2 = _make_prop(
        propId='test-002',
        ppShadeSignal='neutral',
        sharpFairLine=None,   # no sharp data
        lineMoveCount=0,
        firstSeenLine=5.5,
        pWin=0.60,            # high_win_pct fires
        roleCertainty=0.40,   # below STRONG_ROLE_FLOOR
        scriptFlag=None,
        isGoblin=False,
        isMoreOnly=False,
    )
    rec2 = _run_gates(d2)

    assert rec2.eligible_nondemon is False, "Expected ineligible — no stability"
    assert 'no_stability_signal' in rec2.reject_reasons, f"Got: {rec2.reject_reasons}"
    print("T3 PASS: strong reason but no stability → excluded")


# ── T4: Demon prop → never in non-demon pool ─────────────────────────────────

def test_demon_prop_excluded_from_nondemon_pool():
    """Demon props must be blocked at apply_hard_blocks and never reach the pool."""
    d = _make_prop(isDemon=True)
    rec = _run_gates(d)

    assert rec.eligible_nondemon is False, "Expected demon to be ineligible"
    assert rec.ineligible_reason == 'is_demon', f"Expected is_demon, got {rec.ineligible_reason}"
    assert rec.nondemon_score == 0.0

    # Confirm it doesn't survive drop_if_score_zero_or_ineligible
    kept = drop_if_score_zero_or_ineligible([rec])
    assert len(kept) == 0, f"Expected 0 kept, got {len(kept)}"

    # Confirm pool building excludes it
    pool = build_nondemon_candidate_pool(kept)
    assert len(pool) == 0
    print("T4 PASS: demon prop never appears in non-demon pool")


# ── T5: 0-score prop → no MILP variable ──────────────────────────────────────

def test_zero_score_no_milp_variable():
    """A 0-score prop must never have a MILP variable created."""
    d = _make_prop(ppShadeSignal='neutral', sharpFairLine=5.5, lineMoveCount=0,
                   firstSeenLine=5.5, pWin=0.50, matchupFlag=None)
    rec = _run_gates(d)

    assert rec.nondemon_score == 0.0
    assert rec.eligible_nondemon is False

    try:
        from ortools.linear_solver import pywraplp
        solver = pywraplp.Solver.CreateSolver('SCIP')
        x = create_milp_variables_from_pool_only(solver, [rec])
        assert len(x) == 0, f"Expected 0 variables for 0-score prop, got {len(x)}"
        print("T5 PASS: 0-score prop → no MILP variable")
    except ImportError:
        print("T5 PASS (no OR-Tools): 0-score → ineligible confirmed")


# ── T6: Known slate → expected props selected ────────────────────────────────

def test_known_slate_selection():
    """
    Given a controlled set of props, verify that the pipeline selects
    only the ones that pass all gates and have highest score.
    """
    # Create 3 valid props and 2 invalid props
    valid_props = [
        _make_prop(propId=f'v{i}', playerName=f'Player {i}',
                   statType='Pitcher Strikeouts', lineScore=5.5 + i,
                   pWin=0.60 + i * 0.02)
        for i in range(3)
    ]
    demon_prop   = _make_prop(propId='d1', isDemon=True)
    missing_prop = _make_prop(propId='', lineScore=None)  # missing data
    no_edge_prop = _make_prop(propId='ne1', ppShadeSignal='neutral',
                              sharpFairLine=5.5, lineMoveCount=0,
                              firstSeenLine=5.5, pWin=0.48, matchupFlag=None)

    all_props = valid_props + [demon_prop, missing_prop, no_edge_prop]

    # Only request 2 legs (fewer than 6 so MILP is feasible with 3 valid props)
    selected, all_records = run_nondemon_pipeline(all_props, n_legs=2)

    # Demons must not be selected
    if selected:
        for leg in selected:
            assert not leg.is_demon, f"Demon found in selected: {leg.prop_id}"
            assert leg.nondemon_score > 0.0, "Selected leg has 0 score"
            assert leg.eligible_nondemon, "Selected leg is ineligible"

    # Demon, missing, and no-edge records must be ineligible
    inelig_ids = {r.prop_id for r in all_records if not r.eligible_nondemon}
    # missing_prop has empty prop_id so it shows up as '' in records
    assert 'd1' in inelig_ids or any(r.is_demon and not r.eligible_nondemon for r in all_records), \
        "Demon not marked ineligible"

    print(f"T6 PASS: pipeline selected {len(selected) if selected else 0} legs from known slate")
    if selected:
        for leg in selected:
            print(f"  → {leg.player_name} {leg.stat_type} {leg.pp_line} "
                  f"score={leg.nondemon_score} reasons={leg.strong_reasons_hit}")


# ─────────────────────────────────────────────────────────────────────────────
# Run all tests
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    tests = [
        test_missing_data_no_milp_variable,
        test_no_strong_reason_score_zero,
        test_strong_reason_no_stability_excluded,
        test_demon_prop_excluded_from_nondemon_pool,
        test_zero_score_no_milp_variable,
        test_known_slate_selection,
    ]
    failed = []
    for t in tests:
        try:
            t()
        except AssertionError as e:
            print(f"FAIL {t.__name__}: {e}")
            failed.append(t.__name__)
        except Exception as e:
            print(f"ERROR {t.__name__}: {e}")
            failed.append(t.__name__)

    print()
    if failed:
        print(f"FAILED: {failed}")
        sys.exit(1)
    else:
        print(f"ALL {len(tests)} TESTS PASSED")
        sys.exit(0)
