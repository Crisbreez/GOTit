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
  // ── Demon pipeline helper — exact route pattern ──────────────────────────────
  //
  //   pipeline = runDemonPipeline(game)
  //   selected = pipeline.selected_demons
  //   if selected.empty and pipeline.post_relaxation_demons.not_empty:
  //       selected = top2(pipeline.post_relaxation_demons)
  //   game.demons = selected
  //   game.demon_pipeline_trace = pipeline.trace
  //
  // Returns { demons: Prop[], trace: object }
  async function runDemonPipeline(gameProps: any[]): Promise<{ demons: any[]; otherDemons: any[]; trace: any }> {
    return new Promise((resolve) => {
      const scriptPath = path.resolve(process.cwd(), 'python', 'qualify_demons.py');
      const python = process.env.PYTHON_BIN || 'python3';
      let out = '', err = '';
      const child = spawn(python, [scriptPath], { timeout: 30_000, cwd: process.cwd() });
      child.stdin.write(JSON.stringify(gameProps));
      child.stdin.end();
      child.stdout.on('data', (d: Buffer) => { out += d.toString(); });
      child.stderr.on('data', (d: Buffer) => { err += d.toString(); });
      child.on('close', (code: number | null) => {
        if (code !== 0) {
          console.warn('[Demons] qualify_demons crashed code=' + code, err.slice(0, 300));
          // Process crash — fallback: raw demons by lineScore, distinct players
          const seen = new Set<string>();
          const raw = gameProps
            .filter((p: any) => p.isDemon)
            .sort((a: any, b: any) => (b.lineScore ?? 0) - (a.lineScore ?? 0))
            .filter((p: any) => { if (seen.has(p.playerName)) return false; seen.add(p.playerName); return true; })
            .slice(0, 2)
            .map((p: any) => ({ ...p, fallback_render_used: true, fallback_reason: 'process_crash' }));
          return resolve({ demons: raw, otherDemons: [], trace: { error: 'process_crash', exit_code: code } });
        }

        let pipeline: any = null;
        try {
          pipeline = JSON.parse(out);
        } catch (e) {
          console.warn('[Demons] JSON parse failed:', e);
          return resolve({ demons: [], otherDemons: [], trace: { error: 'json_parse_failed' } });
        }

        // ── Exact route pattern ────────────────────────────────────────────────
        let selected: any[] = Array.isArray(pipeline?.selected_demons) ? pipeline.selected_demons : [];
        const postRelaxation: any[] = Array.isArray(pipeline?.post_relaxation_demons) ? pipeline.post_relaxation_demons : [];
        const trace: any = pipeline?.trace ?? {};

        // Assertion: selected_demons must be a subset of post_relaxation_demons or empty
        if (selected.length > 0 && postRelaxation.length === 0) {
          console.warn('[Demons] ASSERT: selected non-empty but post_relaxation empty — unexpected');
        }

        // If selected is empty but post_relaxation has survivors → use top2(post_relaxation)
        if (selected.length === 0 && postRelaxation.length > 0) {
          const seen = new Set<string>();
          selected = postRelaxation
            .sort((a: any, b: any) => (b.demonScore?.composite ?? 0) - (a.demonScore?.composite ?? 0))
            .filter((d: any) => { if (seen.has(d.playerName)) return false; seen.add(d.playerName); return true; })
            .slice(0, 2)
            .map((d: any) => ({ ...d, fallback_render_used: true, fallback_reason: 'post_relaxation_bypass' }));
          console.warn('[Demons] selected_demons was empty — used top2(post_relaxation), count=' + selected.length);
        }

        // Assertion: demons.length must be 0..2
        if (selected.length > 2) {
          console.warn('[Demons] ASSERT: selected.length=' + selected.length + ' > 2 — trimming');
          selected = selected.slice(0, 2);
        }

        const otherDemons: any[] = Array.isArray(pipeline?.other_demons) ? pipeline.other_demons : [];
        resolve({ demons: selected, otherDemons, trace });
      });
      child.on('error', (e: Error) => {
        console.warn('[Demons] spawn error:', e.message);
        resolve({ demons: [], otherDemons: [], trace: { error: e.message } });
      });
    });
  }

  // ── Bypass test endpoint ───────────────────────────────────────────────────
  // GET /api/demons/bypass-test?league=MLB&gameId=<id>
  // Runs qualify_demons.py with bypass_test=true, returns pipeline_trace +
  // selected=top2(post_relaxation) directly without MILP path.
  // Compare this output to /api/slate demons to isolate wiring bugs.
  app.get('/api/demons/bypass-test', async (req, res) => {
    const league  = ((req.query.league  as string) || 'MLB').toUpperCase();
    const gameId  =  (req.query.gameId  as string) || '';
    const rawProps = await storage.getProps(league);
    const gameProps = gameId
      ? rawProps.filter((p: any) => p.gameId === gameId)
      : rawProps.filter((p: any) => p.isDemon);

    if (gameProps.length === 0) {
      return res.json({ error: 'no props found for gameId=' + gameId + ' league=' + league });
    }

    // Use runDemonPipeline with DEMON_BYPASS_TEST=1 via temp env override
    const scriptPath = path.resolve(process.cwd(), 'python', 'qualify_demons.py');
    const python = process.env.PYTHON_BIN || 'python3';
    let out = '', errBuf = '';
    await new Promise<void>((done) => {
      const child = spawn(python, [scriptPath], {
        timeout: 30_000,
        cwd: process.cwd(),
        env: { ...process.env, DEMON_BYPASS_TEST: '1' },
      });
      child.stdin.write(JSON.stringify(gameProps));
      child.stdin.end();
      child.stdout.on('data', (d: Buffer) => { out += d.toString(); });
      child.stderr.on('data', (d: Buffer) => { errBuf += d.toString(); });
      child.on('close', () => done());
      child.on('error', () => done());
    });

    let pipeline: any = {};
    try { pipeline = JSON.parse(out); } catch { /* ignore */ }

    const bypassSelected: any[] = Array.isArray(pipeline?.selected_demons) ? pipeline.selected_demons : [];
    const postRelaxation: any[] = Array.isArray(pipeline?.post_relaxation_demons) ? pipeline.post_relaxation_demons : [];
    const trace: any = pipeline?.trace ?? {};

    res.json({
      bypass_test:              true,
      league,
      gameId:                   gameId || '(all)',
      props_sent:               gameProps.length,
      selected_count:           bypassSelected.length,
      post_relaxation_count:    postRelaxation.length,
      selected_demons:          bypassSelected,
      post_relaxation_demons:   postRelaxation,
      pipeline_trace:           trace,
      stderr_tail:              errBuf.slice(-800),
    });
  });

  // Support both /api/slate/MLB and /api/slate?league=MLB
  app.get(['/api/slate', '/api/slate/:league'], async (req, res) => {
    const league = ((req.params as any).league || req.query.league as string || 'MLB').toUpperCase();
    const rawProps = await storage.getProps(league);

    if (rawProps.length === 0) {
      return res.json({ league, props: [], games: [], pulledAt: null });
    }

    // ── Stamp learning data from player_performance onto each prop ──────────
    // Closes the learning loop: Results → player_performance → scoring signals.
    // Fields stamped: hitRate (fraction), avgMargin, gamesPlayed, last5
    // The System reads these as: hit_rate → _has_real_signal, sample_factor, blend
    try {
      const allPerf = await storage.getAllPerformance();
      if (allPerf && allPerf.length > 0) {
        const perfMap = new Map<string, any>();
        for (const row of allPerf) {
          perfMap.set(`${row.playerName}||${row.statType}||${row.league}`, row);
        }
        for (const prop of rawProps as any[]) {
          const key = `${prop.playerName}||${prop.statType}||${prop.league?.toUpperCase() ?? league}`;
          const perf = perfMap.get(key);
          if (perf) {
            const total = (perf.hitCount ?? 0) + (perf.missCount ?? 0);
            if (total > 0) {
              prop.hitRate       = perf.hitCount / total;           // 0–1 fraction
              prop.hitRateSample = total;                           // n for min_hit_rate_sample gate
              prop.gamesPlayed   = total;                           // n for sample_factor
              prop.avgMargin     = perf.avgMargin ?? null;          // actual − line
              prop.last5         = perf.last5 ?? [];                // ring buffer
              // form_trend: L5 hit rate vs season hit rate
              const l5 = (perf.last5 ?? []) as string[];
              if (l5.length >= 3) {
                const l5Rate = l5.filter((x: string) => x === 'hit').length / l5.length;
                prop.l10_rate      = l5Rate;
                prop.baseline_rate = prop.hitRate;
                prop.form_trend    = (l5Rate - prop.hitRate) > 0.15 ? 1 :
                                     (l5Rate - prop.hitRate) < -0.15 ? -1 : 0;
              }
            }
          }
        }
        console.log(`[learn] stamped performance data on ${rawProps.length} props (${allPerf.length} records)`);
      }
    } catch (e: any) {
      console.warn('[learn] performance stamp failed:', e.message);
    }

    // ── Stamp demon_log history onto demon props ───────────────────────────
    // Demontime reads boost_score; we inject demonHitRate so prop_to_raw_leg
    // can set a more accurate boost_score for repeat demons.
    try {
      const supaUrl  = process.env.SUPABASE_URL;
      const supaKey  = process.env.SUPABASE_ANON_KEY;
      if (supaUrl && supaKey) {
        const dlogResp = await fetch(
          `${supaUrl}/rest/v1/demon_log?select=player_name,stat_type,result&result=neq.null&order=created_at.desc&limit=500`,
          { headers: { 'apikey': supaKey, 'Authorization': `Bearer ${supaKey}` } }
        );
        if (dlogResp.ok) {
          const dlog: any[] = await dlogResp.json();
          // Aggregate: demon hit rate per player+stat
          const dlogMap = new Map<string, { hits: number; total: number }>();
          for (const row of dlog) {
            const k = `${row.player_name}||${row.stat_type}`;
            const cur = dlogMap.get(k) ?? { hits: 0, total: 0 };
            cur.total++;
            if (row.result === 'hit') cur.hits++;
            dlogMap.set(k, cur);
          }
          for (const prop of rawProps as any[]) {
            if (!prop.isDemon) continue;
            const k = `${prop.playerName}||${prop.statType}`;
            const d = dlogMap.get(k);
            if (d && d.total >= 2) {
              prop.demonHitRate  = d.hits / d.total;
              prop.demonTotal    = d.total;
              // boost_score proxy: demon with > 65% hit rate gets a mild signal lift
              const baseBoost = 0.60;
              prop.sharp_action  = Math.min(0.95, baseBoost + (d.hits / d.total - 0.50) * 0.8);
            }
          }
          console.log(`[learn] demon_log stamped on demon props (${dlog.length} records)`);
        }
      }
    } catch (e: any) {
      console.warn('[learn] demon_log stamp failed:', e.message);
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

    // Run demon pipeline for each game in parallel — exact route pattern
    // Drop minor league games that leaked through PP MLB feed
    const MINOR_TEAM_NAMES = [
      'Tulo Toros', 'Toros', 'Drive', 'Crawdads', 'Mudcats', 'RubberDucks',
      'Biscuits', 'Barons', 'Shuckers', 'Lookouts', 'Travs', 'Tourists',
      'Woodpeckers', 'Pelicans', 'Jumbo Shrimp', 'Tides', 'IronPigs',
      'RailRiders', 'Knights', 'Clippers', 'Express', 'Sounds', 'Bisons',
      'Wings', 'Mud Hens', 'Redbirds', 'Storm Chasers', 'Rainiers',
    ];
    const isMinorLeagueGame = (g: any) => {
      const matchup = (g.matchup || '').toLowerCase();
      return MINOR_TEAM_NAMES.some(t => matchup.includes(t.toLowerCase()));
    };
    const rawGames = Array.from(gameMap.values()).filter(g => !!g.gameId && !isMinorLeagueGame(g));
    const demonResults = await Promise.all(
      rawGames.map(g => runDemonPipeline(g.props))
    );

    // Build games — pipeline.selected_demons is the ONLY source of truth for demons.
    // No re-filter, no rebuild, no override after this point.
    const games = rawGames
      .map((g, i) => {
        const pipeline = demonResults[i];
        // selected_demons is source of truth — assign directly, never re-derive
        const demons: any[] = pipeline.demons;  // already has post_relaxation fallback applied

        // ── Hard assertion: selected_demons must not be lost ─────────────────
        // If pipeline produced demons but the slot ended up empty, something
        // clobbered them between pipeline output and slate response.
        if (pipeline.demons.length > 0 && demons.length === 0) {
          console.error(
            '[Demons] ASSERTION_FAIL: selected_demons lost in slate route',
            'gameId=' + g.gameId,
            'pipeline.demons.length=' + pipeline.demons.length,
            'demons.length=' + demons.length,
          );
        }

        // Assertion: must be array, must be 0..2
        if (!Array.isArray(demons)) {
          console.error('[Demons] ASSERT: demons is not array for game', g.gameId);
        }
        if (demons.length > 2) {
          console.error('[Demons] ASSERT: demons.length > 2 for game', g.gameId, '— got', demons.length);
        }

        return {
          ...g,
          demons,                              // source of truth: pipeline.selected_demons
          other_demons: pipeline.otherDemons ?? [],  // remaining demons for individual selection
          demon_pipeline_trace: pipeline.trace, // full 8-stage trace
          goblins:   (g.props as any[]).filter((p: any) => p.isGoblin),
          standards: (g.props as any[]).filter((p: any) => !p.isDemon && !p.isGoblin),
        };
      })
      .sort((a, b) => {
        if (!a.startTime) return 1;
        if (!b.startTime) return -1;
        return new Date(a.startTime).getTime() - new Date(b.startTime).getTime();
      });

    // ── Debug: log game object shape for each game ────────────────────────────
    games.forEach(g => {
      console.log(
        '[SLATE_DEBUG] game=' + g.gameId +
        ' | standards(raw)=' + (g.standards?.length ?? 0) +
        ' | demons(pipeline)=' + (g.demons?.length ?? 0) +
        ' | goblins=' + (g.goblins?.length ?? 0) +
        ' | demon_players=' + (g.demons?.map((d: any) => d.playerName) || []).join(',')
      );
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

  // ── Debug: dump optimizer result vs game object for a specific game ──────────
  // Usage: GET /api/debug/game?league=MLB&gameId=<gameId>
  // Returns: { game_standards_raw, game_demons_pipeline, optimizer_six_legs, optimizer_two_demons, mismatches }
  app.get('/api/debug/game', async (req, res) => {
    const league  = (req.query.league  as string || 'MLB').toUpperCase();
    const gameId  = req.query.gameId   as string;

    // 1. Fetch raw props from DB
    const rawProps = await storage.getProps(league);
    const gameProps = gameId
      ? rawProps.filter((p: any) => (p.gameId || p.gameMatchup || 'unknown') === gameId)
      : rawProps;

    if (gameProps.length === 0) {
      return res.status(404).json({ error: 'no props found for gameId', gameId, league });
    }

    // 2. Run demon pipeline on this game's props
    const demonResult = await runDemonPipeline(gameProps);

    // 3. Run optimizer on non-demon props only
    const nonDemonProps = gameProps.filter((p: any) => !p.isDemon);
    let optimizerResult: any = null;
    let optimizerError:  any = null;

    if (nonDemonProps.length >= 6) {
      await new Promise<void>((done) => {
        const scriptPath = path.resolve(process.cwd(), 'python', 'optimize.py');
        const python     = process.env.PYTHON_BIN || 'python3';
        let out = '', err = '';
        const child = spawn(python, [scriptPath], { timeout: 30_000, cwd: process.cwd() });
        child.stdin.write(JSON.stringify(nonDemonProps));
        child.stdin.end();
        child.stdout.on('data', (d: Buffer) => { out += d.toString(); });
        child.stderr.on('data', (d: Buffer) => { err += d.toString(); });
        child.on('close', () => {
          try { optimizerResult = JSON.parse(out); } catch { optimizerError = out.slice(0, 300); }
          if (!optimizerResult) optimizerError = (optimizerError || '') + ' | stderr: ' + err.slice(0, 300);
          done();
        });
        child.on('error', (e: Error) => { optimizerError = e.message; done(); });
      });
    } else {
      optimizerError = `only ${nonDemonProps.length} non-demon props — need 6`;
    }

    const gameOptOut = optimizerResult?.[gameId] || null;
    const sixLegs    = gameOptOut?.six_legs   || [];
    const twoDemonsOpt = gameOptOut?.two_demons || [];

    // 4. Build game object exactly as /api/slate does
    const gameStandardsRaw = gameProps.filter((p: any) => !p.isDemon && !p.isGoblin);
    const gameGoblins      = gameProps.filter((p: any) => p.isGoblin);
    const gameDemonsPipeline = demonResult.demons;

    // 5. Check for mismatches
    const mismatches: string[] = [];

    // six_legs player uniqueness
    const sixLegPlayers = sixLegs.map((l: any) => l.player_name);
    if (new Set(sixLegPlayers).size !== sixLegs.length) {
      mismatches.push('DUPLICATE_PLAYERS_IN_SIX_LEGS: ' + sixLegPlayers.join(', '));
    }
    if (sixLegs.length !== 6 && sixLegs.length !== 0) {
      mismatches.push('SIX_LEGS_COUNT=' + sixLegs.length + ' (expected 6)');
    }

    // demon leaks into six_legs
    const demonLeaks = sixLegs.filter((l: any) => l.tier === 'demon');
    if (demonLeaks.length > 0) {
      mismatches.push('DEMON_LEAK_IN_SIX_LEGS: ' + demonLeaks.map((l: any) => l.player_name).join(', '));
    }

    // non-demon leaks into two_demons
    const nonDemonLeaks = twoDemonsOpt.filter((l: any) => l.tier !== 'demon');
    if (nonDemonLeaks.length > 0) {
      mismatches.push('NON_DEMON_IN_TWO_DEMONS: ' + nonDemonLeaks.map((l: any) => l.player_name + '/' + l.tier).join(', '));
    }

    // pipeline demons count
    if (gameDemonsPipeline.length < 1) {
      mismatches.push('PIPELINE_DEMONS_COUNT=' + gameDemonsPipeline.length + ' (expected 1-2)');
    }

    res.json({
      gameId,
      league,
      mismatches,
      game_standards_raw_count:    gameStandardsRaw.length,
      game_goblins_count:          gameGoblins.length,
      game_demons_pipeline_count:  gameDemonsPipeline.length,
      game_demons_pipeline:        gameDemonsPipeline.map((d: any) => ({
        playerName: d.playerName, statType: d.statType, lineScore: d.lineScore,
        isDemon: d.isDemon, tier: d.tier, bucket: d.demonScore?.bucket,
      })),
      optimizer_six_legs_count:    sixLegs.length,
      optimizer_six_legs:          sixLegs.map((l: any) => ({
        player_name: l.player_name, stat_type: l.stat_type, tier: l.tier,
        line: l.line, direction: l.direction, p_win: l.p_win,
      })),
      optimizer_two_demons_count:  twoDemonsOpt.length,
      optimizer_two_demons:        twoDemonsOpt.map((l: any) => ({
        player_name: l.player_name, stat_type: l.stat_type, tier: l.tier, p_win: l.p_win,
      })),
      optimizer_error: optimizerError,
      pipeline_trace_summary: demonResult.trace?.stages
        ? Object.fromEntries(
            Object.entries(demonResult.trace.stages).map(([k, v]: [string, any]) => [
              k, { count: v.survivors ?? v.count ?? v.selected ?? '?', note: v.note }
            ])
          )
        : demonResult.trace,
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
    // req.body may contain { results: [{ legId, actualValue }] } from manual settle
    const manualResults: Array<{ legId: number; actualValue: number }> = req.body?.results ?? [];
    const manualMap = new Map(manualResults.map((r: any) => [r.legId, r.actualValue]));

    for (const leg of legs) {
      if (leg.status === 'pending' || leg.status === 'live') {
        const manualActual = manualMap.get(leg.id);

        let hit: boolean;
        let actualValue: number | null;

        if (manualActual != null) {
          // Real settle: user provided the actual stat value
          actualValue = manualActual;
          hit = actualValue > leg.lineScore;  // More wins if actual > line
        } else {
          // No actual value provided — mark as pending (not settled yet)
          // Do NOT use Math.random() — never fake results
          continue;
        }

        await storage.updateLegStatus(leg.id, hit ? 'hit' : 'miss', actualValue);

        // ── Learning loop: write outcome to player_performance ──────────────
        try {
          await storage.updatePlayerPerformance(
            leg.playerName,
            leg.statType,
            leg.league ?? 'MLB',
            hit ? 'hit' : 'miss',
            actualValue,
            leg.lineScore,
          );
        } catch (e: any) {
          console.warn('[learn] player_performance update failed:', e.message);
        }
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
