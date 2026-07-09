"""
gotit_cli.py — GOTit command-line interface.

Commands:
  gotit pull [--league MLB|NBA|NFL|MMA]   Fetch live board, run selector, print all games.
  gotit slate <game_id> [--legs N]        Show N legs for a specific game from cache.
  gotit games                             List all cached game IDs.
  gotit clear                             Wipe the local prop cache.
"""
import asyncio
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import typer
from sqlalchemy import Column, Float, String, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from sqlalchemy.future import select

from gotit.leg_selector import (
    CalibrationParams,
    PPProp,
    SharpConsensus,
    Tier,
    select_legs_for_slate,
)
from gotit.odds_ingest import fetch_board
from gotit.rss_ingest import build_dnp_model, normalize
from gotit.sharp_consensus import build_sharp_consensus

# ─── paths ────────────────────────────────────────────────────────────────────

CLI_DIR    = Path(__file__).parent
REPO_ROOT  = CLI_DIR.parent.parent
CONFIG_DIR = REPO_ROOT / "config"
CAL_FILE   = CONFIG_DIR / "calibration_latest.json"
DB_PATH    = REPO_ROOT / "gotit.db"

# ─── DB setup ─────────────────────────────────────────────────────────────────

Base = declarative_base()


class PropCache(Base):
    __tablename__ = "prop_cache"

    prop_id         = Column(String, primary_key=True)
    game_id         = Column(String, index=True)
    player_id       = Column(String)
    player_name     = Column(String)
    stat_type       = Column(String)
    tier            = Column(String)
    line            = Column(Float)
    direction       = Column(String, default="OVER")
    hours_to_lock   = Column(Float)
    public_over_pct = Column(Float, nullable=True)
    dnp_prob        = Column(Float, default=0.0)
    pulled_at       = Column(String)


engine           = create_async_engine(f"sqlite+aiosqlite:///{DB_PATH}", echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# ─── calibration ──────────────────────────────────────────────────────────────

def load_calibration() -> CalibrationParams:
    if not CAL_FILE.exists():
        typer.echo(
            f"[ERROR] Calibration file not found: {CAL_FILE}\n"
            "Run:  python scripts/gen_calibration.py",
            err=True,
        )
        raise typer.Exit(1)
    with open(CAL_FILE) as f:
        return CalibrationParams(**json.load(f))


# ─── correlation proxy ────────────────────────────────────────────────────────

def build_correlation_proxy(props: List[PPProp]) -> Dict[tuple, float]:
    """Same-game proxy ρ map."""
    rho: Dict[tuple, float] = {}
    by_game: Dict[str, List[PPProp]] = {}
    for p in props:
        by_game.setdefault(p.game_id, []).append(p)

    for game_props in by_game.values():
        for i, p1 in enumerate(game_props):
            for p2 in game_props[i + 1:]:
                if p1.player_id == p2.player_id:
                    rho_val = 0.25   # same player, different stat
                elif p1.stat_type == p2.stat_type:
                    rho_val = 0.15   # same stat, different player same game
                else:
                    rho_val = 0.10   # same game default
                rho[(p1.prop_id, p2.prop_id)] = rho_val
                rho[(p2.prop_id, p1.prop_id)] = rho_val
    return rho


# ─── cache helpers ────────────────────────────────────────────────────────────

async def save_props(props: List[PPProp]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    async with AsyncSessionLocal() as db:
        for p in props:
            for tier in p.tiers_offered:
                row = PropCache(
                    prop_id=p.prop_id,
                    game_id=p.game_id,
                    player_id=p.player_id,
                    player_name=p.player_name,
                    stat_type=p.stat_type,
                    tier=tier.value,
                    line=p.lines[tier],
                    direction="OVER",
                    hours_to_lock=p.hours_to_lock,
                    public_over_pct=p.public_over_pct,
                    dnp_prob=p.dnp_prob,
                    pulled_at=now,
                )
                await db.merge(row)
        await db.commit()


async def load_props(game_id: Optional[str] = None) -> List[PPProp]:
    async with AsyncSessionLocal() as db:
        q = select(PropCache)
        if game_id:
            q = q.where(PropCache.game_id == game_id)
        result = await db.execute(q)
        rows = result.scalars().all()

    props: List[PPProp] = []
    for r in rows:
        try:
            tier = Tier(r.tier)
        except ValueError:
            tier = Tier.STANDARD
        props.append(
            PPProp(
                prop_id=r.prop_id,
                game_id=r.game_id,
                player_id=r.player_id,
                player_name=r.player_name,
                stat_type=r.stat_type,
                tiers_offered=[tier],
                lines={tier: r.line},
                hours_to_lock=r.hours_to_lock,
                public_over_pct=r.public_over_pct,
                dnp_prob=r.dnp_prob or 0.0,
                correlation_partners=[],
            )
        )
    return props


async def update_dnp(dnp: Dict[str, float]) -> None:
    if not dnp:
        return
    async with AsyncSessionLocal() as db:
        for pp_id, prob in dnp.items():
            await db.execute(
                text("UPDATE prop_cache SET dnp_prob = :prob WHERE player_id = :pid"),
                {"prob": prob, "pid": pp_id},
            )
        await db.commit()


async def list_game_ids() -> List[str]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text("SELECT DISTINCT game_id FROM prop_cache ORDER BY game_id")
        )
        return [r[0] for r in result.fetchall()]


async def clear_cache() -> int:
    async with AsyncSessionLocal() as db:
        result = await db.execute(text("DELETE FROM prop_cache"))
        await db.commit()
        return result.rowcount


# ─── print helpers ────────────────────────────────────────────────────────────

GOLD    = "\033[33m"
RED     = "\033[91m"
GREEN   = "\033[92m"
DIM     = "\033[2m"
RESET   = "\033[0m"
BOLD    = "\033[1m"


def _tier_color(tier: str) -> str:
    if tier == "Demon":  return RED
    if tier == "Goblin": return GREEN
    return ""


def print_leg(i: int | str, leg: Dict, *, is_demon: bool = False) -> None:
    color  = RED if is_demon else ""
    label  = f"  {BOLD}{RED}D.{RESET}" if is_demon else f"  {i}."
    name   = leg["player_name"]
    stat   = leg["stat_type"]
    dir_   = leg["direction"]
    line   = leg["line"]
    p      = leg["p_win"]
    tier   = leg.get("tier", "Standard")
    tc     = _tier_color(tier)
    typer.echo(
        f"{label} {color}{BOLD}{name}{RESET}  "
        f"{tc}{tier}{RESET}  "
        f"{GOLD}{dir_} {line}{RESET}  "
        f"{stat}  "
        f"{DIM}p={p:.3f}{RESET}"
    )


def print_game(game_id: str, data: Dict, legs: int) -> None:
    six   = data["six_legs"]
    demons = data["two_demons"]
    meta  = data.get("meta", {})

    typer.echo(f"\n{BOLD}{'─'*60}{RESET}")
    typer.echo(f"{BOLD}{GOLD}{game_id}{RESET}")
    ev = meta.get("portfolio_ev_per_$1", 0.0)
    typer.echo(f"  {DIM}EV/$ = {ev:.4f}  |  cal v{meta.get('calibration_version','?')}{RESET}")
    typer.echo()

    displayed_props = six[:legs]
    for i, leg in enumerate(displayed_props, 1):
        if leg.get("tier") == "Demon":
            print_leg(i, leg, is_demon=True)
        else:
            print_leg(i, leg)

    # If demons weren't already in the six_legs slice, show separately
    demon_ids = {d["prop_id"] for d in demons}
    shown_ids = {lg["prop_id"] for lg in displayed_props}
    extra_demons = [d for d in demons if d["prop_id"] not in shown_ids]
    if extra_demons:
        typer.echo(f"  {DIM}─── Demons ───{RESET}")
        for d in extra_demons:
            print_leg("D", d, is_demon=True)


# ─── Typer app ────────────────────────────────────────────────────────────────

app = typer.Typer(
    name="gotit",
    help="GOTit — Prop Intelligence CLI",
    add_completion=False,
)

LEAGUE_IDS = {"MLB": 2, "NBA": 7, "NFL": 1, "MMA": 12}


@app.command()
def pull(
    league: Optional[str] = typer.Option(
        None,
        "--league", "-l",
        help="Filter by league: MLB, NBA, NFL, MMA. Omit for full board.",
    ),
    legs: int = typer.Option(6, "--legs", "-n", min=2, max=6, help="Legs to show per game."),
    no_rss: bool = typer.Option(False, "--no-rss", help="Skip RSS injury ingest (faster)."),
    json_out: bool = typer.Option(False, "--json", help="Output raw JSON instead of formatted table."),
) -> None:
    """Fetch live PrizePicks board, run selector, print all games."""

    async def _run() -> None:
        await init_db()
        calibration = load_calibration()

        league_upper = league.upper() if league else None
        league_id    = LEAGUE_IDS.get(league_upper) if league_upper else None

        typer.echo(f"Pulling board{f' — {league_upper}' if league_upper else ''}…")
        props = await fetch_board(league_id=league_id)
        if not props:
            typer.echo("Board unchanged (304) or no props returned.")
            props = await load_props()
            if not props:
                typer.echo("Cache is also empty. Try again.")
                return
            typer.echo(f"Using {len(props)} cached props.")
        else:
            typer.echo(f"Fetched {len(props)} props.")
            await save_props(props)

        # RSS DNP ingest
        if not no_rss:
            typer.echo("Building DNP model from RSS feeds…")
            name_to_ppid = {normalize(p.player_name): p.player_id for p in props}
            dnp = await build_dnp_model(name_to_ppid)
            if dnp:
                typer.echo(f"  {len(dnp)} players flagged with injury risk.")
                await update_dnp(dnp)
                # Reload props with updated DNP
                props = await load_props()
        else:
            dnp = {}

        typer.echo("Building sharp consensus…")
        sharp = await build_sharp_consensus(props)

        rho = build_correlation_proxy(props)

        typer.echo("Running leg selector…")
        result = select_legs_for_slate(props, sharp, calibration, {}, rho, dnp)

        if not result:
            typer.echo("\nNo games with feasible solutions. Board may be too thin.")
            return

        if json_out:
            typer.echo(json.dumps(result, indent=2))
            return

        typer.echo(f"\n{BOLD}{GREEN}GOTit Slate — {len(result)} games{RESET}")
        for game_id, data in sorted(result.items()):
            print_game(game_id, data, legs=legs)

        typer.echo(f"\n{DIM}Pulled at {datetime.now().strftime('%I:%M %p CT')}{RESET}\n")

    asyncio.run(_run())


@app.command()
def slate(
    game_id: str = typer.Argument(..., help="Game ID to show (from 'gotit games')."),
    legs: int = typer.Option(6, "--legs", "-n", min=2, max=6, help="Number of legs to show."),
    json_out: bool = typer.Option(False, "--json", help="Output raw JSON."),
) -> None:
    """Show N legs for a specific game from the local cache."""

    async def _run() -> None:
        await init_db()
        calibration = load_calibration()

        props = await load_props(game_id=game_id)
        if not props:
            typer.echo(f"No cached props for game: {game_id}")
            typer.echo("Run 'gotit games' to list available game IDs.")
            raise typer.Exit(1)

        sharp = await build_sharp_consensus(props)
        dnp   = {p.player_id: p.dnp_prob for p in props if p.dnp_prob > 0}
        rho   = build_correlation_proxy(props)

        result = select_legs_for_slate(props, sharp, calibration, {}, rho, dnp)

        if game_id not in result:
            typer.echo(f"No feasible solution for game: {game_id}")
            typer.echo(f"  ({len(props)} props in cache — need at least 6 candidates)")
            raise typer.Exit(1)

        if json_out:
            typer.echo(json.dumps(result[game_id], indent=2))
            return

        print_game(game_id, result[game_id], legs=legs)
        typer.echo()

    asyncio.run(_run())


@app.command()
def games() -> None:
    """List all cached game IDs."""

    async def _run() -> None:
        await init_db()
        ids = await list_game_ids()
        if not ids:
            typer.echo("Cache is empty. Run 'gotit pull' first.")
            return
        typer.echo(f"\n{BOLD}Cached games ({len(ids)}):{RESET}")
        for gid in ids:
            typer.echo(f"  {gid}")
        typer.echo()

    asyncio.run(_run())


@app.command()
def clear() -> None:
    """Wipe the local prop cache (gotit.db)."""

    async def _run() -> None:
        await init_db()
        n = await clear_cache()
        typer.echo(f"Cleared {n} rows from cache.")

    asyncio.run(_run())


if __name__ == "__main__":
    app()
