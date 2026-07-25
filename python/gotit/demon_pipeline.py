"""
demon_pipeline.py — Demontime v2

Rank every demon in the game by exploit closeness (fair P, edge vs break-even,
bump/boost, role, context); return the best two — exact matches when they exist,
nearest possible when they don't. Never return zero when ≥2 demon tiles exist.

One-liner: score every PP demon individually on composite edge score; always
take top 2; never joint-pair optimize; never fear-filter.
"""
from __future__ import annotations

import json
import logging
import math
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# MLB demon allowlist — only these stats enter Demontime for MLB
# ─────────────────────────────────────────────────────────────────────────────
MLB_DEMON_ALLOWLIST = {
    "Total Bases",
    "Hits+Runs+RBIs",
    "Hitter Fantasy Score",
    "Singles",          # only allowed at line == 0.5
}
MLB_SINGLES_MAX_LINE = 0.5

# ─────────────────────────────────────────────────────────────────────────────
# Config (all tunable)
# ─────────────────────────────────────────────────────────────────────────────
CFG = {
    "output_count":    2,
    "weights": {
        "p_true":  0.35,
        "p_edge":  0.25,
        "gap":     0.15,
        "bump":    0.10,
        "boost":   0.10,
        "role":    0.03,
        "ctx":     0.02,
    },
    "pass": {
        "p_edge_min": 0.00,
        "role_min":   0.70,
    },
    "close": {
        "p_band": 0.08,
    },
    "correlation_max":  0.65,
    "market_weight":    0.60,
    "proj_weight":      0.40,
    "lottery_stats":    ["Home Runs"],
    "lottery_penalty":  0.05,
    "kill_excludes_pass": True,
}

# Break-even per leg by slip type (5-flex default when context missing)
P_NEED_TABLE = {
    "5_flex":  0.543,
    "6_flex":  0.542,
    "4_flex":  0.557,
    "3_flex":  0.571,
    "2_power": 0.577,
    "3_power": 0.550,
    "4_power": 0.560,
}
DEFAULT_P_NEED = P_NEED_TABLE["5_flex"]

# Default bump by sport when standard_line missing
DEFAULT_BUMP = {
    "MLB": 1.0,
    "NBA": 2.5,
    "NFL": 5.0,
    "MMA": 1.0,
}


# ─────────────────────────────────────────────────────────────────────────────
# Math helpers
# ─────────────────────────────────────────────────────────────────────────────

def _phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _estimate_sigma(line: float, stat_type: str) -> float:
    ratios = {
        "Total Bases": 0.38, "Hits": 0.55, "Hits+Runs+RBIs": 0.40,
        "Pitcher Strikeouts": 0.32, "Pitches Thrown": 0.14,
        "Pitching Outs": 0.28, "Hits Allowed": 0.42,
        "Earned Runs Allowed": 0.70, "Walks Allowed": 0.75,
        "Hitter Fantasy Score": 0.40, "Points": 0.26,
        "Rebounds": 0.40, "Assists": 0.45,
        "Points+Rebounds+Assists": 0.28,
        "Rushing Yards": 0.40, "Passing Yards": 0.22,
        "Receiving Yards": 0.40, "Receptions": 0.45,
        "Takedowns": 0.55, "Fight Time (Mins)": 0.30,
        "Significant Strikes": 0.22, "Total Strikes": 0.25,
        "Knockdowns": 0.90, "Submission Attempts": 0.85,
        "Singles": 0.80, "Doubles": 0.90, "Home Runs": 1.20,
        "RBIs": 0.70, "Runs": 0.70, "Walks": 0.80,
        "Stolen Bases": 0.90,
    }
    return max(0.5, line * ratios.get(stat_type, 0.40))


def _normalize(val: float, lo: float = 0.0, hi: float = 1.0) -> float:
    """Clamp val into [lo, hi] and normalize to [0, 1]."""
    if hi <= lo:
        return 0.5
    return max(0.0, min(1.0, (val - lo) / (hi - lo)))


# ─────────────────────────────────────────────────────────────────────────────
# p_true — fair probability More hits at demon_line
# ─────────────────────────────────────────────────────────────────────────────

def _p_true(demon: Dict[str, Any]) -> float:
    """
    Priority:
    1. sharpFairLine from MoneyLine DK/FD → CDF at demon_line
    2. mu/sigma from model → CDF
    3. hitRate from DB
    4. lineScore-based CDF with sigma estimate (no mu*1.05 fake signal)
    """
    demon_line = float(demon.get("lineScore", demon.get("demon_line", 0)) or 0)
    stat_type  = str(demon.get("statType", "") or "")
    sigma      = _estimate_sigma(demon_line, stat_type)

    # 1. Sharp fair line (MoneyLine DK/FD)
    sharp_fair = demon.get("sharpFairLine") or demon.get("sharp_fair_line")
    if sharp_fair is not None:
        try:
            mu = float(sharp_fair)
            p_market = _phi((mu - demon_line) / sigma) if sigma > 0 else (1.0 if mu > demon_line else 0.0)
            # Blend with projection if available
            mu_proj = float(demon.get("mu", 0) or 0)
            if mu_proj > 0:
                sigma_proj = float(demon.get("sigma", sigma) or sigma)
                p_proj = _phi((mu_proj - demon_line) / sigma_proj) if sigma_proj > 0 else 0.5
                return CFG["market_weight"] * p_market + CFG["proj_weight"] * p_proj
            return p_market
        except (ValueError, TypeError):
            pass

    # 2. Model mu/sigma
    mu_raw    = float(demon.get("mu", 0) or 0)
    sigma_raw = float(demon.get("sigma", 0) or 0)
    if mu_raw > 0 and sigma_raw > 0:
        return _phi((mu_raw - demon_line) / sigma_raw)

    # 3. Hit rate from history
    hit_rate = demon.get("hitRate") or demon.get("hit_rate")
    if hit_rate is not None:
        try:
            return float(hit_rate)
        except (ValueError, TypeError):
            pass

    # 4. No signal — return neutral 0.50 (not fabricated edge)
    return 0.50


# ─────────────────────────────────────────────────────────────────────────────
# p_need — slip break-even for this demon
# ─────────────────────────────────────────────────────────────────────────────

def _p_need(slip_context: Optional[Dict]) -> float:
    if not slip_context:
        return DEFAULT_P_NEED
    slip_type = slip_context.get("slip_type") or slip_context.get("power_or_flex", "5_flex")
    n         = int(slip_context.get("pick_count", 5) or 5)
    key       = f"{n}_{slip_type.replace('flex','flex').replace('power','power')}"
    return P_NEED_TABLE.get(key, DEFAULT_P_NEED)


# ─────────────────────────────────────────────────────────────────────────────
# Component scores
# ─────────────────────────────────────────────────────────────────────────────

def _bump_quality(demon: Dict, sport: str) -> float:
    """Smaller bump → higher quality (easier bar vs standard line)."""
    demon_line    = float(demon.get("lineScore", 0) or 0)
    standard_line = demon.get("standardLine") or demon.get("standard_line")
    if standard_line is not None:
        try:
            bump = demon_line - float(standard_line)
        except (ValueError, TypeError):
            bump = DEFAULT_BUMP.get(sport, 1.0)
    else:
        bump = DEFAULT_BUMP.get(sport, 1.0)
    # Invert: bump=0 → 1.0, bump=3 → ~0
    return max(0.0, 1.0 - (bump / 3.0))


def _boost_quality(demon: Dict) -> float:
    """Multiplier impact → larger is better."""
    boost = demon.get("multiplierImpact") or demon.get("multiplier_impact") or 0
    try:
        return min(1.0, float(boost) / 2.0)   # 2x boost → 1.0
    except (ValueError, TypeError):
        return 0.5   # unknown boost → neutral


def _role_score(demon: Dict) -> float:
    """Confirmed full workload → 1.0. Risk factors reduce."""
    dnp = float(demon.get("dnpProb") or demon.get("dnp_prob") or 0)
    if dnp >= 0.25:
        return 0.1
    return max(0.0, 1.0 - dnp * 2.0)


def _ctx_score(demon: Dict) -> float:
    """Context score from DB or neutral."""
    ctx = demon.get("ctxScore") or demon.get("ctx_score")
    if ctx is not None:
        try:
            return float(ctx)
        except (ValueError, TypeError):
            pass
    return 0.5


def _kill_flags(demon: Dict) -> List[str]:
    """Hard kills that push tier ≤ CLOSE."""
    kills = []
    dnp = float(demon.get("dnpProb") or demon.get("dnp_prob") or 0)
    if dnp >= 0.40:
        kills.append("high_dnp_risk")
    recent = demon.get("recentStats") or demon.get("recent_stats") or []
    if isinstance(recent, list) and len(recent) >= 3:
        zeros = sum(1 for v in recent if v == 0 or v is None)
        if zeros / len(recent) >= 0.80:
            kills.append("zero_heavy_history")
    return kills


# ─────────────────────────────────────────────────────────────────────────────
# Composite score + tier
# ─────────────────────────────────────────────────────────────────────────────

def _score_one(demon: Dict[str, Any], sport: str, slip_context: Optional[Dict]) -> Dict[str, Any]:
    demon_line = float(demon.get("lineScore", 0) or 0)
    stat_type  = str(demon.get("statType", "") or "")

    p_true  = _p_true(demon)
    p_need  = _p_need(slip_context)
    p_edge  = p_true - p_need

    proj_mean = float(demon.get("mu") or demon.get("projMean") or 0)
    gap       = proj_mean - demon_line if proj_mean > 0 else 0.0

    bump_q  = _bump_quality(demon, sport)
    boost_q = _boost_quality(demon)
    role_s  = _role_score(demon)
    ctx_s   = _ctx_score(demon)
    kills   = _kill_flags(demon)

    W = CFG["weights"]
    score = (
        W["p_true"]  * _normalize(p_true, 0.3, 0.9)
      + W["p_edge"]  * _normalize(p_edge, -0.2, 0.3)
      + W["gap"]     * _normalize(gap, -2.0, 2.0)
      + W["bump"]    * bump_q
      + W["boost"]   * boost_q
      + W["role"]    * role_s
      + W["ctx"]     * ctx_s
    )

    # Penalties
    if stat_type in CFG["lottery_stats"]:
        score -= CFG["lottery_penalty"]
    if kills:
        score -= 0.10 * len(kills)

    score = max(0.0, min(1.0, score))

    # Tier
    p_cfg  = CFG["pass"]
    c_cfg  = CFG["close"]
    if (p_edge >= p_cfg["p_edge_min"]
            and role_s >= p_cfg["role_min"]
            and not kills):
        tier = "PASS"
    elif p_edge >= -c_cfg["p_band"] and not any(k in kills for k in ["high_dnp_risk"]):
        tier = "CLOSE"
    else:
        tier = "STRETCH"

    # Force ≤ CLOSE if kills present and config says so
    if kills and CFG["kill_excludes_pass"] and tier == "PASS":
        tier = "CLOSE"

    standard_line = demon.get("standardLine") or demon.get("standard_line")
    bump_val = (demon_line - float(standard_line)) if standard_line is not None else DEFAULT_BUMP.get(sport, 1.0)

    return {
        **demon,
        # Demontime scoring fields
        "rank":         None,
        "side":         "MORE",
        "tier":         tier,
        "p_true":       round(p_true, 4),
        "p_need":       round(p_need, 4),
        "p_edge":       round(p_edge, 4),
        "proj_mean":    round(proj_mean, 3),
        "gap":          round(gap, 3),
        "bump":         round(bump_val, 2),
        "bump_quality": round(bump_q, 3),
        "boost_quality":round(boost_q, 3),
        "role_score":   round(role_s, 3),
        "ctx_score":    round(ctx_s, 3),
        "final_score":  round(score, 4),
        "kill_flags":   kills,
        "flags":        kills,
        # Compat
        "p_hit":        round(p_true, 4),
        "propScore":    round(score, 4),
        "confidenceLevel": max(1, min(5, round(p_true * 5))),
        "eligible":     True,
        "ineligible_reason": "",
        "direction":    "over",
        "isDemon":      True,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Correlation check
# ─────────────────────────────────────────────────────────────────────────────

def _correlated(a: Dict, b: Dict) -> bool:
    """Simple correlation signals — same pitcher dependency, same player, both HR."""
    if a.get("playerName") == b.get("playerName"):
        return True
    if a.get("statType") == "Home Runs" and b.get("statType") == "Home Runs":
        return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Pair selection
# ─────────────────────────────────────────────────────────────────────────────

def _pick_pair(scored: List[Dict]) -> Tuple[Dict, Dict, bool]:
    """Pick A=rank[0], then first B with low correlation. Fall back to rank[1]."""
    a = scored[0]
    high_corr = False
    for b in scored[1:]:
        if not _correlated(a, b):
            return a, b, False
    # All correlated — still take top 2, flag it
    high_corr = True
    return a, scored[1], high_corr


def _pair_mode(a: Dict, b: Dict) -> str:
    tiers = {a["tier"], b["tier"]}
    if tiers == {"PASS"}:
        return "EXACT_PAIR"
    if "PASS" in tiers:
        return "MIXED"
    return "CLOSEST_AVAILABLE"


# ─────────────────────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────────────────────

def run_demon_pipeline(
    props: List[Dict[str, Any]],
    game_id: str,
    slip_context: Optional[Dict] = None,
    sport: str = "MLB",
) -> Dict[str, Any]:
    """
    Run Demontime for a single game.
    Always returns top 2 demons (when ≥2 exist). Never returns zero on purpose.
    """
    demons = [p for p in props if p.get("isDemon") or p.get("is_demon")]
    demons = [p for p in demons if not p.get("isSynthetic")]
    demons = [p for p in demons if str(p.get("direction", "over")).lower() != "under"]

    # Infer sport from first demon if not passed
    if demons and sport == "MLB":
        sport = str(demons[0].get("league", "MLB") or "MLB").upper()

    # MLB allowlist — filter before scoring
    if sport == "MLB":
        def _mlb_allowed(p: Dict) -> bool:
            st   = str(p.get("statType", "") or "")
            line = float(p.get("lineScore", 0) or 0)
            if st not in MLB_DEMON_ALLOWLIST:
                return False
            if st == "Singles" and line > MLB_SINGLES_MAX_LINE:
                return False
            return True
        demons = [p for p in demons if _mlb_allowed(p)]

    if not demons:
        return {
            "selected_demons":        [],
            "post_relaxation_demons": [],
            "other_demons":           [],
            "status":  "NO-GO",
            "strategy": "Demontime",
            "mode": "NO_DEMONS",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "trace": {"game_id": game_id, "reason": "no demon props"},
            "warnings": [],
            "rejected_top": [],
            "error": None,
        }

    if len(demons) == 1:
        scored = [_score_one(demons[0], sport, slip_context)]
        scored[0]["rank"] = 1
        return {
            "selected_demons":        scored,
            "post_relaxation_demons": scored,
            "other_demons":           [],
            "status":  "CLEAR",
            "strategy": "Demontime",
            "mode": "INSUFFICIENT_INVENTORY",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "trace": {"game_id": game_id, "total_demon_props": 1, "selected": 1},
            "warnings": ["only_1_demon_in_game"],
            "rejected_top": [],
            "error": None,
        }

    # Score all
    scored = [_score_one(d, sport, slip_context) for d in demons]
    scored.sort(key=lambda x: x["final_score"], reverse=True)

    # Pick pair
    a, b, high_corr = _pick_pair(scored)
    a["rank"] = 1
    b["rank"] = 2

    mode  = _pair_mode(a, b)
    picks = [a, b]

    # Others — everything not picked
    picked_ids = {a.get("id"), b.get("id")}
    others     = [d for d in scored if d.get("id") not in picked_ids]

    # Rejected top notes
    rejected_top = [
        {"player_name": d.get("playerName", ""), "stat_type": d.get("statType", ""),
         "demon_line": d.get("lineScore", 0), "final_score": d.get("final_score", 0),
         "reason": "correlated_with_1" if _correlated(a, d) else "lower_score"}
        for d in scored[2:4]
    ]

    warnings = []
    if high_corr:
        warnings.append("pair_high_correlation")
    warnings.append("payout_verify_on_submit")

    return {
        "selected_demons":        picks,
        "post_relaxation_demons": picks,
        "other_demons":           others,
        "status":  "CLEAR",
        "strategy": "Demontime",
        "mode": mode,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "trace": {
            "game_id":           game_id,
            "sport":             sport,
            "total_demon_props": len(demons),
            "selected":          len(picks),
            "mode":              mode,
        },
        "warnings":     warnings,
        "rejected_top": rejected_top,
        "error": None,
    }


def format_output(result: Dict[str, Any]) -> Dict[str, Any]:
    return result


# ─────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    try:
        payload = json.loads(sys.stdin.read())
        props   = payload if isinstance(payload, list) else payload.get("props", [])
        game_id = (props[0].get("gameId") or props[0].get("game_id") or "unknown") if props else "unknown"
        sport   = (props[0].get("league") or "MLB") if props else "MLB"
        result  = run_demon_pipeline(props, str(game_id), sport=str(sport))
        print(json.dumps(result))
    except Exception as exc:
        log.exception("[Demontime] fatal error")
        print(json.dumps({"error": str(exc), "selected_demons": [], "post_relaxation_demons": []}))
        sys.exit(1)
