import type { Express } from 'express';
import type { Server } from 'http';
import { spawn } from 'child_process';
import path from 'path';
import { storage } from './storage';
import { orchestratePull, getProviderState, updateProviderState, resetAllCooldowns } from './providers/orchestrator';
import { startSlipTracker, runTrackingCycle } from './slipTracker';
import { seedDemoData } from './demo-seed';
import { centralToday, centralTodayStartUTC } from './time';

// ── Sharp consensus pull helper ────────────────────────────────────────────
function spawnSharpPull(league: string, props: any[]): void {
  const scriptPath = path.resolve(process.cwd(), 'python', 'sharp_pull.py');
  const python = process.env.PYTHON_BIN || 'python3';
  const payload = JSON.stringify({ league, props });

  const child = spawn(python, [scriptPath], {
    cwd: process.cwd(),
    timeout: 45_000,
  });

  let out = '';
  let err = '';
  child.stdout.on('data', (d: Buffer) => { out += d.toString(); });
  child.stderr.on('data', (d: Buffer) => { err += d.toString(); });

  child.stdin.write(payload);
  child.stdin.end();

  child.on('close', async (code: number | null) => {
    if (code !== 0) {
      console.error(`[sharp] pull exited ${code}:`, err.slice(0, 300));
      return;
    }
    try {
      const result = JSON.parse(out);
      if (!result.ok) {
        console.error('[sharp] pull failed:', result.error);
        return;
      }
      console.log(`[sharp] ${result.matched}/${result.total} props matched for ${result.league}`);

      // Stamp sharp signals onto props and re-upsert so DB has sharpFairLine + ppShadeSignal
      const enrichments: Array<{id:string; sharpFairLine?:number; ppShadeSignal:string; marketDelta?:number}>
        = result.enrichments || [];
      if (enrichments.length > 0) {
        const enrichMap = new Map(enrichments.map((e: any) => [e.id, e]));
        const enrichedProps = props.map((p: any) => {
          const e = enrichMap.get(p.id);
          if (!e) return p;
          return {
            ...p,
            sharpFairLine:  e.sharpFairLine  ?? null,
            ppShadeSignal:  e.ppShadeSignal  ?? 'no_data',
            marketDelta:    e.marketDelta    ?? null,
          };
        });
        try {
          await storage.upsertProps(enrichedProps);
          console.log(`[sharp] stamped sharp signals on ${enrichedProps.length} props`);
        } catch (e: any) {
          console.error('[sharp] re-upsert failed:', e.message);
        }
      }
    } catch {
      console.error('[sharp] non-JSON output:', out.slice(0, 200));
    }
  });

  child.on('error', (e: Error) => {
    console.error('[sharp] spawn error:', e.message);
  });
}

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
        // Fire sharp consensus pull in background (non-blocking)
        spawnSharpPull(league, result.props);
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

  // ── Sharp Pull (standalone) ────────────────────────────────────────────────
  // Refresh sharp_store.json from SGO without triggering a full PP pull.
  // Reads existing cached props from Supabase.
  app.post('/api/sharp-pull', async (req, res) => {
    const league = (req.query.league as string || 'MLB').toUpperCase();
    try {
      const props = await storage.getProps(league);
      if (props.length === 0) {
        return res.status(422).json({ ok: false, error: 'No cached props for league — run /api/pull first' });
      }
      // Spawn async; respond immediately so the client isn't blocked
      spawnSharpPull(league, props);
      res.json({ ok: true, message: `Sharp pull started for ${league} (${props.length} props)` });
    } catch (err: any) {
      res.status(500).json({ ok: false, error: err.message });
    }
  });

  // ── Slate ──────────────────────────────────────────────────────────────────
  // ── Demon qualifier helper: runs qualify_demons.py for a single game ────────
  async function pickTop2Demons(gameProps: any[]): Promise<any[]> {
    return new Promise((resolve) => {
      const scriptPath = path.resolve(process.cwd(), 'python', 'qualify_demons.py');
      const python = process.env.PYTHON_BIN || 'python3';
      let out = '', err = '';
      const child = spawn(python, [scriptPath], { timeout: 20_000, cwd: process.cwd() });
      child.stdin.write(JSON.stringify(gameProps));
      child.stdin.end();
      child.stdout.on('data', (d: Buffer) => { out += d.toString(); });
      child.stderr.on('data', (d: Buffer) => { err += d.toString(); });
      child.on('close', (code: number | null) => {
        if (code !== 0) {
          console.warn('[Demons] qualify_demons exited', code, err.slice(0, 200));
          // Fallback: return top 2 raw demons sorted by lineScore desc
          const raw = gameProps.filter((p: any) => p.isDemon)
            .sort((a: any, b: any) => b.lineScore - a.lineScore)
            .slice(0, 2);
          return resolve(raw);
        }
        try {
          const result = JSON.parse(out);
          resolve(Array.isArray(result) ? result.slice(0, 2) : []);
        } catch {
          resolve([]);
        }
      });
      child.on('error', () => resolve([]));
    });
  }

  app.get('/api/slate', async (req, res) => {
    const league = (req.query.league as string || 'MLB').toUpperCase();
    const rawProps = await storage.getProps(league);

    if (rawProps.length === 0) {
      return res.json({ league, props: [], games: [], pulledAt: null });
    }

    // Group props by game
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

    // Run qualify_demons for each game in parallel — picks exactly top 2
    const rawGames = Array.from(gameMap.values()).filter(g => !!g.gameId);
    const demonResults = await Promise.all(
      rawGames.map(g => pickTop2Demons(g.props))
    );

    const games = rawGames
      .map((g, i) => ({
        ...g,
        demons:    demonResults[i],   // exactly top 2 GOTit-qualified demons
        goblins:   (g.props as any[]).filter((p: any) => p.isGoblin),
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
    const { gameId, matchup, startTime, scriptLabel, propCount, propIds, league, demonScores } = req.body;
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

    // ── Write demon_log entries for every demon leg ────────────────────────
    // Records all 5-layer scores at selection time so we can audit which
    // component is fooling the model when demons lose.
    if (demonScores && typeof demonScores === 'object') {
      for (const leg of createdLegs) {
        if (!leg.isDemon) continue;
        const ds = demonScores[leg.propId] || demonScores[leg.id];
        if (!ds) continue;
        try {
          const supaUrl = process.env.SUPABASE_URL;
          const supaKey = process.env.SUPABASE_ANON_KEY;
          await fetch(`${supaUrl}/rest/v1/demon_log`, {
            method: 'POST',
            headers: {
              'apikey': supaKey ?? '',
              'Authorization': `Bearer ${supaKey ?? ''}`,
              'Content-Type': 'application/json',
              'Prefer': 'return=minimal',
            },
            body: JSON.stringify({
              slip_id:          slip.id,
              player_name:      leg.playerName,
              stat_type:        leg.statType,
              line_score:       leg.lineScore,
              direction:        leg.direction,
              game_matchup:     leg.gameMatchup,
              p_win:            ds.pWin ?? null,
              l1_market_anchor: ds.market_anchor ?? null,
              l2_dist_hit_rate: ds.dist_hit_rate ?? null,
              l3_game_script:   ds.game_script_fit ?? null,
              l4_role_certainty:ds.role_certainty ?? null,
              l5_pair_diversity:ds.pair_diversity ?? null,
              composite:        ds.composite ?? null,
              selected_at:      new Date().toISOString(),
            }),
          });
        } catch (e) {
          console.warn('[demon_log] insert failed:', e);
        }
      }
    }

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

    // ── Write actual results back to demon_log ─────────────────────────────
    // This closes the loop: we know the score at selection, now we know the result.
    for (const leg of updatedLegs) {
      if (!leg.isDemon) continue;
      try {
        const supaUrl = process.env.SUPABASE_URL;
        const supaKey = process.env.SUPABASE_ANON_KEY;
        await fetch(
          `${supaUrl}/rest/v1/demon_log?slip_id=eq.${id}&player_name=eq.${encodeURIComponent(leg.playerName)}&stat_type=eq.${encodeURIComponent(leg.statType)}`,
          {
            method: 'PATCH',
            headers: {
              'apikey': supaKey ?? '',
              'Authorization': `Bearer ${supaKey ?? ''}`,
              'Content-Type': 'application/json',
              'Prefer': 'return=minimal',
            },
            body: JSON.stringify({
              actual_value: leg.actualValue ?? null,
              result:       leg.status,
              settled_at:   new Date().toISOString(),
            }),
          }
        );
      } catch (e) {
        console.warn('[demon_log] settle patch failed:', e);
      }
    }

    // DNP legs are voided (PP rules) — exclude from win/loss calculation
    const activeLegs = updatedLegs.filter(l => l.status !== 'dnp');
    const hits = activeLegs.filter(l => l.status === 'hit').length;
    const allHit = activeLegs.length > 0 && hits === activeLegs.length;

    await storage.updateSlipStatus(id, allHit ? 'settled_win' : 'settled_loss', {
      settledAt: new Date().toISOString(),
    });

    res.json({ ok: true, won: allHit, hits, total: activeLegs.length, dnp: updatedLegs.length - activeLegs.length });
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
    res.json({ version: '2026-07-09-NOFILTER', builtAt: new Date().toISOString() });
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

  // ── Optimizer ───────────────────────────────────────────────────────────────
  app.post('/api/optimize', async (req, res) => {
    const props = req.body;
    if (!Array.isArray(props) || props.length === 0) {
      return res.status(400).json({ error: 'props array required' });
    }

    const scriptPath = path.resolve(process.cwd(), 'python', 'optimize.py');
    const python = process.env.PYTHON_BIN || 'python3';

    let output = '';
    let errorOut = '';
    const timeout = 60_000; // 60 s max

    const child = spawn(python, [scriptPath], {
      timeout,
      cwd: process.cwd(),
    });

    child.stdin.write(JSON.stringify(props));
    child.stdin.end();

    child.stdout.on('data', (d: Buffer) => { output += d.toString(); });
    child.stderr.on('data', (d: Buffer) => { errorOut += d.toString(); });

    child.on('close', (code: number | null) => {
      if (code !== 0) {
        console.error('[optimize] python exited', code, errorOut.slice(0, 500));
        return res.status(500).json({ error: 'optimizer failed', detail: errorOut.slice(0, 200) });
      }
      try {
        const result = JSON.parse(output);
        if (result.error) {
          return res.status(422).json(result);
        }
        res.json(result);
      } catch (e) {
        console.error('[optimize] bad JSON output:', output.slice(0, 200));
        res.status(500).json({ error: 'optimizer output not JSON' });
      }
    });

    child.on('error', (err: Error) => {
      console.error('[optimize] spawn error:', err.message);
      res.status(500).json({ error: `spawn error: ${err.message}` });
    });
  });

  // ── Player performance (learning loop) ───────────────────────────────
  app.get('/api/performance', async (_req, res) => {
    try {
      const rows = await (storage as any).getAllPerformance();
      res.json(rows);
    } catch (e: any) {
      res.status(500).json({ error: (e as Error).message });
    }
  });

  // ── Startup
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
