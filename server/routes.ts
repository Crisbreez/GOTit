import type { Express } from 'express';
import type { Server } from 'http';
import { storage } from './storage';
import { orchestratePull, getProviderState, updateProviderState, resetAllCooldowns } from './providers/orchestrator';
import { startSlipTracker, runTrackingCycle } from './slipTracker';
import { seedDemoData } from './demo-seed';
import { centralToday, centralTodayStartUTC } from './time';

export function registerRoutes(httpServer: Server, app: Express) {

  // ── Stats ──────────────────────────────────────────────────────────────────
  app.get('/api/stats', async (_req, res) => {
    const allArrays = await Promise.all(['MLB','NBA','NFL','MMA'].map(l => storage.getProps(l)));
    const all = allArrays.flat();
    const slips = await storage.getSlips();
    res.json({
      totalProps: all.length,
      activeProps: all.length,
      totalSlips: slips.length,
      wonSlips: slips.filter(s => s.status === 'settled_win').length,
      lostSlips: slips.filter(s => s.status === 'settled_loss').length,
    });
  });

  // ── Pull ───────────────────────────────────────────────────────────────────
  app.post('/api/pull', async (req, res) => {
    const league = (req.query.league as string || 'MLB').toUpperCase();

    try {
      const result = await orchestratePull(league, true);
      updateProviderState(league, result);

      if (!result.fromCache && result.providerUsed !== 'cache' && result.providerUsed !== 'demo' && result.props.length > 0) {
        await storage.upsertProps(result.props);
        await storage.logPull(league, result.props.length);
      }

      res.json({
        ok: true,
        count: result.propCount,
        pulledAt: result.pulledAt,
        providerUsed: result.providerUsed,
        isFallback: result.isFallback,
        fromCache: result.fromCache,
        rateLimited: result.rateLimited,
        cooldownMs: result.cooldownMs,
        fallbackReason: result.fallbackReason,
      });
    } catch (err: any) {
      console.error('Pull error:', err.message);
      const cachedRows = await storage.getProps(league);
      if (cachedRows.length > 0) {
        res.json({ ok: true, count: cachedRows.length, fromCache: true, rateLimited: true, fallbackReason: err.message });
      } else {
        res.status(500).json({ ok: false, error: err.message });
      }
    }
  });

  // ── Slate ──────────────────────────────────────────────────────────────────
  app.get('/api/slate', async (req, res) => {
    const league = (req.query.league as string || 'MLB').toUpperCase();
    const rawProps = await storage.getProps(league);

    if (rawProps.length === 0) {
      return res.json({ league, props: [], games: [], pulledAt: null });
    }

    // Group props by game — no scoring, no enrichment
    const gameMap = new Map<string, any>();
    for (const p of rawProps) {
      const key = p.gameId || p.gameMatchup || 'unknown';
      if (!gameMap.has(key)) {
        gameMap.set(key, {
          gameId: key,
          matchup: p.ppDisplayMatchup || p.gameMatchup || '',
          startTime: p.gameStartTime || '',
          props: [],
        });
      }
      const g = gameMap.get(key)!;
      if (!g.startTime && p.gameStartTime) g.startTime = p.gameStartTime;
      if (p.ppDisplayMatchup && p.ppDisplayMatchup.includes('vs') && !g.matchup.includes('vs')) {
        g.matchup = p.ppDisplayMatchup;
      }
      g.props.push(p);
    }

    const games = Array.from(gameMap.values())
      .filter(g => {
        if (!g.startTime) return true;
        const startMs = new Date(g.startTime).getTime();
        const nowMs = Date.now();
        return startMs > nowMs - (4 * 60 * 60 * 1000);
      })
      .map(g => ({
        ...g,
        demons: (g.props as any[]).filter((p: any) => p.isDemon),
        goblins: (g.props as any[]).filter((p: any) => p.isGoblin),
        standards: (g.props as any[]).filter((p: any) => !p.isDemon && !p.isGoblin),
      }))
      .sort((a, b) => {
        if (!a.startTime) return 1;
        if (!b.startTime) return -1;
        return new Date(a.startTime).getTime() - new Date(b.startTime).getTime();
      });

    const lastPull = await storage.getLastPull(league);
    const provState = getProviderState(league);

    res.json({
      league,
      props: rawProps,
      games,
      pulledAt: lastPull?.pulledAt || null,
      providerUsed: provState.currentProvider ?? 'cache',
      isFallback: provState.fallbackUsed ?? false,
      fallbackReason: provState.lastPullError ?? null,
      lastSuccessfulProvider: provState.lastSuccessfulProvider,
      lastSuccessfulPullAt: provState.lastSuccessfulPullAt,
      rateLimited: provState.rateLimited ?? false,
      cooldownMs: provState.cooldownRemainingMs ?? null,
    });
  });

  // ── Build Slip ─────────────────────────────────────────────────────────────
  app.post('/api/slips/build', async (req, res) => {
    const { gameId, matchup, startTime, scriptLabel, propCount, propIds, league } = req.body;
    if (!league) return res.status(400).json({ error: 'league required' });

    const rawProps = await storage.getProps(league);
    let selectedProps: any[] = [];

    if (Array.isArray(propIds) && propIds.length > 0) {
      selectedProps = (propIds as string[])
        .map((id: string) => rawProps.find((p: any) => p.id === id))
        .filter(Boolean)
        .slice(0, 6);
    } else if (typeof propCount === 'number' && propCount > 0) {
      const count = Math.min(propCount, 6);
      const gameProps = gameId
        ? rawProps.filter((p: any) => (p.gameId || p.gameMatchup || 'unknown') === gameId)
        : rawProps;
      selectedProps = gameProps.slice(0, count);
    } else {
      return res.status(400).json({ error: 'propCount or propIds required' });
    }

    if (selectedProps.length === 0) {
      return res.status(422).json({ error: 'No valid props found for selection' });
    }

    const resolvedMatchup = matchup || (selectedProps[0] as any)?.gameMatchup || gameId || 'GOTit Slip';
    const resolvedStartTime = startTime || (selectedProps[0] as any)?.gameStartTime || null;
    const resolvedScript = scriptLabel || 'GOTit Script';

    const slip = await storage.createSlip({
      league,
      gameMatchup: resolvedMatchup,
      gameStartTime: resolvedStartTime,
      scriptLabel: resolvedScript,
      status: 'pending',
      qualityScore: 0,
      correlationScore: 0,
      createdAt: new Date().toISOString(),
    });

    const legs = selectedProps.map((p: any) => ({
      slipId: slip.id,
      propId: p.id,
      playerName: p.playerName,
      teamAbbr: p.teamAbbr || '',
      statType: p.statType,
      lineScore: p.lineScore,
      direction: p.direction || 'over',
      isDemon: p.isDemon || false,
      isGoblin: p.isGoblin || false,
      gameMatchup: p.gameMatchup || resolvedMatchup || null,
      gameStartTime: p.gameStartTime || startTime,
      status: 'pending',
      propScore: p.propScore || 0,
    }));

    await storage.createLegs(legs);
    const createdLegs = await storage.getLegsBySlip(slip.id);
    res.json({ ...slip, legs: createdLegs });
  });

  // ── Get Slips ──────────────────────────────────────────────────────────────
  app.get('/api/slips', async (req, res) => {
    const statusParam = req.query.status as string;
    let statusFilter: string[] | undefined;
    if (statusParam === 'active') statusFilter = ['pending', 'live'];
    else if (statusParam === 'settled') statusFilter = ['settled_win', 'settled_loss'];

    const slipList = await storage.getSlips(statusFilter);
    const withLegs = await Promise.all(slipList.map(async s => {
      const legs = await storage.getLegsBySlip(s.id);
      return { ...s, legs };
    }));
    res.json(withLegs);
  });

  // ── Delete Slip ────────────────────────────────────────────────────────────
  app.delete('/api/slips/:id', async (req, res) => {
    const id = parseInt(req.params.id);
    if (isNaN(id)) return res.status(400).json({ error: 'Invalid slip id' });
    const slip = await storage.getSlipById(id);
    if (!slip) return res.status(404).json({ error: 'Slip not found' });
    await storage.deleteSlip(id);
    res.json({ ok: true, deleted: id });
  });

  // ── Settle Slip ────────────────────────────────────────────────────────────
  app.post('/api/slips/:id/settle', async (req, res) => {
    const id = parseInt(req.params.id);
    const slip = await storage.getSlipById(id);
    if (!slip) return res.status(404).json({ error: 'Slip not found' });

    const legs = await storage.getLegsBySlip(id);
    for (const leg of legs) {
      if (leg.status === 'pending' || leg.status === 'live') {
        const hit = Math.random() > 0.40;
        await storage.updateLegStatus(leg.id, hit ? 'hit' : 'miss', leg.lineScore * (hit ? 1.1 : 0.85));
      }
    }

    const updatedLegs = await storage.getLegsBySlip(id);
    const hits = updatedLegs.filter(l => l.status === 'hit').length;
    const allHit = hits === updatedLegs.length;

    await storage.updateSlipStatus(id, allHit ? 'settled_win' : 'settled_loss', {
      settledAt: new Date().toISOString(),
    });

    res.json({ ok: true, won: allHit, hits, total: updatedLegs.length });
  });

  // ── Update Leg ─────────────────────────────────────────────────────────────
  app.patch('/api/legs/:id', async (req, res) => {
    const id = parseInt(req.params.id);
    const { status, actualValue } = req.body;
    await storage.updateLegStatus(id, status, actualValue);
    res.json({ ok: true });
  });

  // ── Refresh tracking for a slip ────────────────────────────────────────────
  app.post('/api/slips/:id/refresh', async (req, res) => {
    const id = parseInt(req.params.id);
    const slip = await storage.getSlipById(id);
    if (!slip) return res.status(404).json({ error: 'Slip not found' });
    try {
      await runTrackingCycle();
      const updated = await storage.getSlipById(id);
      const legs = await storage.getLegsBySlip(id);
      res.json({ ...updated, legs });
    } catch (e: any) {
      res.status(500).json({ error: e.message });
    }
  });

  // ── Version ────────────────────────────────────────────────────────────────
  app.get('/api/version', (_req, res) => {
    res.json({ version: '2026-07-08-CLEAN', builtAt: new Date().toISOString() });
  });

  // ── Admin ──────────────────────────────────────────────────────────────────
  app.post('/api/admin/reset-cooldowns', (_req, res) => {
    resetAllCooldowns();
    res.json({ ok: true });
  });

  app.post('/api/admin/reset-slip-legs', async (req, res) => {
    const { slipId } = req.body;
    if (!slipId) return res.status(400).json({ error: 'slipId required' });
    const legs = await storage.getLegsBySlip(slipId);
    for (const leg of legs) {
      await storage.updateLegStatus(leg.id, 'pending', null);
    }
    await storage.updateSlipStatus(slipId, 'live');
    res.json({ ok: true, reset: legs.length });
  });

  app.get('/api/admin/debug-mlb-raw', async (_req, res) => {
    try {
      const today = centralToday();
      const resp = await fetch(`https://statsapi.mlb.com/api/v1/schedule?sportId=1&date=${today}`);
      const json = await resp.json() as any;
      const games = (json.dates?.[0]?.games ?? []).map((g: any) => ({
        gamePk: g.gamePk,
        away: g.teams.away.team.name,
        home: g.teams.home.team.name,
        status: g.status.detailedState,
      }));
      res.json({ today, gameCount: games.length, games });
    } catch (e: any) {
      res.json({ error: e.message });
    }
  });

  app.get('/api/admin/debug-mlb', async (req, res) => {
    const { player, matchup, stat } = req.query as Record<string, string>;
    try {
      const tracker = await import('./mlbTracker');
      const games = await tracker.getTodayGames();
      const result = await tracker.getPlayerStat(
        player || 'Alex Bregman',
        stat || 'Hitter Fantasy Score',
        matchup || 'CHC vs BAL',
      );
      const activeGames = games.filter((g: any) =>
        g.status === 'In Progress' || g.status === 'Final' || g.status === 'Game Over'
      );
      res.json({
        player, matchup, stat, result,
        gamePool: games.length,
        activeGames: activeGames.map((g: any) => `${g.awayTeam} @ ${g.homeTeam} [${g.status}]`),
      });
    } catch (e: any) {
      res.json({ error: e.message });
    }
  });

  // ── Startup ────────────────────────────────────────────────────────────────
  Promise.all(['MLB','NBA','NFL','MMA'].map(l => storage.getProps(l))).then(arrays => {
    const hasAnyProps = arrays.some(a => a.length > 0);
    if (!hasAnyProps) {
      console.log('[GOTit] DB empty on startup — seeding demo data');
      seedDemoData();
    } else {
      console.log('[GOTit] DB has props — serving cached slate, awaiting manual pull');
    }
  }).catch(() => {
    console.log('[GOTit] Could not check DB on startup — seeding demo data');
    seedDemoData();
  });

  startSlipTracker();
}
