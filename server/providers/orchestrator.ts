/**
 * GOTit Pull Orchestrator — Resilient Cache-First Strategy
 *
 * Priority chain per league:
 *   1. Serve DB cache IMMEDIATELY (always — never blank the slate)
 *   2. Attempt PrizePicks if PP cooldown has expired
 *   3. If PP succeeds → write to DB, mark source = prizepicks
 *   4. If PP fails (403/captcha) → attempt SGO immediately
 *   5. If SGO succeeds → write to DB, mark source = sportsgameodds
 *   6. If both fail → keep serving DB cache (never blank)
 *   7. Demo seed: only if DB is completely empty after all providers fail
 *
 * Cooldowns (per league, per provider):
 *   PP normal:           15 min between attempts
 *   PP after 403:        60 min backoff
 *   PP after 3+ failures: 4 hours
 *   SGO after 429:       30 min backoff
 *   MoneyLine after 429: 30 min backoff
 *   Hard minimum (any):  5 min between force-refresh attempts
 *
 * Updated priority chain:
 *   1. Cache (immediate)
 *   2. PrizePicks
 *   3. MoneyLine (PP lines preferred within ML feed)
 *   4. SGO (legacy, no player props on free tier)
 *   5. Cache fallback
 *   6. Demo (empty DB only)
 */

import { pullPrizePicks } from './prizepicks';
import { pullSGO } from './sportsgameodds';
import { pullMoneyLine } from './moneyline';
import type { RawCanonicalProp, DataSource } from './canonical';
import { storage } from '../storage';

export interface PullResult {
  props: RawCanonicalProp[];
  providerUsed: DataSource;
  isFallback: boolean;
  fallbackReason: string | null;
  pulledAt: string;
  propCount: number;
  fromCache: boolean;
  cooldownMs: number | null;
  rateLimited: boolean;
}

interface ProviderCooldown {
  lastAttemptAt: number | null;
  consecutiveFailures: number;
  rateLimited: boolean;
  lastError: string | null;
}

interface LeagueState {
  pp: ProviderCooldown;
  sgo: ProviderCooldown;
  ml: ProviderCooldown;
  currentProvider: DataSource | null;
  lastSuccessAt: number | null;
  fallbackUsed: boolean;
}

const leagueState = new Map<string, LeagueState>();

function makeCD(): ProviderCooldown {
  return { lastAttemptAt: null, consecutiveFailures: 0, rateLimited: false, lastError: null };
}

function getState(league: string): LeagueState {
  if (!leagueState.has(league)) {
    leagueState.set(league, { pp: makeCD(), sgo: makeCD(), ml: makeCD(), currentProvider: null, lastSuccessAt: null, fallbackUsed: false });
  }
  return leagueState.get(league)!;
}

function ppCooldownMs(cd: ProviderCooldown): number {
  // partner-api.prizepicks.com works from datacenter IPs — keep cooldowns minimal
  // Only back off if we get a genuine IP block (1015/403), not on transient errors
  if (cd.rateLimited && cd.consecutiveFailures >= 3) return 10 * 60 * 1000; // 10min only if actually blocked
  if (cd.rateLimited && cd.consecutiveFailures >= 1) return  3 * 60 * 1000; // 3min
  if (cd.consecutiveFailures >= 3) return 2 * 60 * 1000;  // 2min for non-block failures
  if (cd.consecutiveFailures >= 1) return 1 * 60 * 1000;  // 1min
  return 0; // no cooldown on first attempt / after success
}

function sgoCooldownMs(cd: ProviderCooldown): number {
  if (cd.consecutiveFailures >= 2) return 60 * 60 * 1000; // 1h
  if (cd.consecutiveFailures >= 1) return 30 * 60 * 1000; // 30min
  return 0;
}

function mlCooldownMs(cd: ProviderCooldown): number {
  if (cd.consecutiveFailures >= 2) return 60 * 60 * 1000; // 1h
  if (cd.consecutiveFailures >= 1) return 30 * 60 * 1000; // 30min
  return 0;
}

function isCoolingDown(cd: ProviderCooldown, cdFn: (cd: ProviderCooldown) => number): boolean {
  if (!cd.lastAttemptAt) return false;
  return (Date.now() - cd.lastAttemptAt) < cdFn(cd);
}

function remainingMs(cd: ProviderCooldown, cdFn: (cd: ProviderCooldown) => number): number | null {
  if (!isCoolingDown(cd, cdFn)) return null;
  return cdFn(cd) - (Date.now() - cd.lastAttemptAt!);
}

function dbRowsToCanonical(rows: any[], leagueName: string): RawCanonicalProp[] {
  return rows.map(row => ({
    id: row.id,
    sourcePropId: row.id,
    source: (row.source ?? 'cache') as DataSource,
    isFallback: true,
    league: leagueName,
    gameId: row.gameId ?? '',
    gameMatchup: row.gameMatchup ?? '',
    gameStartTime: row.gameStartTime ?? null,
    playerName: row.playerName ?? 'Unknown',
    teamAbbr: row.teamAbbr ?? '',
    statType: row.statType ?? '',
    lineScore: row.lineScore ?? 0,
    direction: (row.direction ?? 'over') as 'over' | 'under',
    isDemon: !!row.isDemon,
    isGoblin: !!row.isGoblin,
    tier: row.isDemon ? 'demon' : row.isGoblin ? 'goblin' : 'standard',
    confidenceLevel: row.confidenceLevel,
    propScore: row.propScore,
    rejectReason: row.rejectReason,
    scriptLabel: row.scriptLabel,
    ppDisplayMatchup: row.ppDisplayMatchup ?? null,
    ppDisplayPlayer: row.ppDisplayPlayer ?? null,
    ppDisplayStat: row.ppDisplayStat ?? null,
    ppDisplayTeam: row.ppDisplayTeam ?? null,
    ppEventTitle: row.ppEventTitle ?? null,
    pulledAt: row.pulledAt ?? new Date().toISOString(),
  }));
}

const HARD_MIN_MS = 5 * 60 * 1000; // 5 min absolute minimum between any force-refresh

export async function orchestratePull(league: string, forceRefresh = false): Promise<PullResult> {
  const now = Date.now();
  const nowIso = new Date(now).toISOString();
  const state = getState(league);

  // ── Load cache first — always available as fallback ─────────────────────
  const cachedRows = await storage.getProps(league);
  const lastPullRecord = await storage.getLastPull(league);
  const hasCachedData = cachedRows.length > 0;

  const sinceLastAttemptPP = state.pp.lastAttemptAt ? now - state.pp.lastAttemptAt : Infinity;
  const underHardMin = sinceLastAttemptPP < HARD_MIN_MS && !forceRefresh;

  // Manual pull: clear accumulated failure state so PP gets a clean shot
  if (forceRefresh && !state.pp.rateLimited) {
    state.pp.consecutiveFailures = 0;
    state.pp.lastError = null;
  }

  // ── Decide whether to attempt PP ────────────────────────────────────────
  const ppCooling = isCoolingDown(state.pp, ppCooldownMs);
  const shouldTryPP = !underHardMin && (forceRefresh || !ppCooling);

  // ── Decide whether to attempt SGO ───────────────────────────────────────
  const sgoCooling = isCoolingDown(state.sgo, sgoCooldownMs);
  const SGO_LEAGUES = new Set(['MLB', 'NBA', 'NFL']);
  const sgoAvailable = SGO_LEAGUES.has(league) && !!process.env.SGO_API_KEY;

  // ── If PP is cooling AND MoneyLine is also cooling AND we have cache → serve cache immediately
  // Do NOT short-circuit if MoneyLine is available — always let ML attempt before falling back to cache.
  const mlAvailableEarly = !!process.env.ML_API_KEY;
  const mlCoolingEarly = isCoolingDown(state.ml, mlCooldownMs);
  if (!shouldTryPP && (sgoCooling || !sgoAvailable) && (mlCoolingEarly || !mlAvailableEarly) && hasCachedData) {
    const cdLeft = remainingMs(state.pp, ppCooldownMs);
    console.log(`[Orchestrator] ${league}: all providers cooling — serving cache (${cachedRows.length} props)`);
    return {
      props: dbRowsToCanonical(cachedRows, league),
      providerUsed: 'cache',
      isFallback: true,
      fallbackReason: state.pp.rateLimited ? 'PP rate-limited' : 'Cooldown active',
      pulledAt: lastPullRecord?.pulledAt ?? nowIso,
      propCount: cachedRows.length,
      fromCache: true,
      cooldownMs: cdLeft,
      rateLimited: state.pp.rateLimited,
    };
  }

  // ── Attempt PrizePicks ───────────────────────────────────────────────────
  let ppError: string | null = null;
  if (shouldTryPP) {
    state.pp.lastAttemptAt = now;
    try {
      console.log(`[Orchestrator] ${league}: attempting PrizePicks`);
      const ppPropsRaw = await pullPrizePicks(league);
      // ── Line floor gate — applied at ingest so trash props never hit DB ──
      // Stats blocked entirely from standard picks — proven losers or no-edge stats.
      // Goblins bypass this (discount lines are fine). Demons are separate pipeline.
      const BLOCKED_STANDARD_STATS = new Set([
        'Home Runs', 'Doubles', 'RBIs', 'Singles', 'Walks',
        'Triples', 'Stolen Bases', 'Hitter Strikeouts',
        'Plate Appearances', 'Pitcher Fantasy Score', 'Runs',
      ]);

      // Mirrors _STANDARD_LINE_FLOOR in leg_selector.py. Demons/goblins exempted
      // because their line is set by PP and we still want to show them.
      const LINE_FLOORS: Record<string, number> = {
        'Total Bases':          2.5,
        'Hits+Runs+RBIs':       2.5,
        'Pitcher Strikeouts':   3.5,
        'Pitches Thrown':       70.0,
        'Pitcher Fantasy Score': 25.0,
        'Hitter Fantasy Score':  7.0,
        'Significant Strikes':  25.0,
        'Round 1 Significant Strikes': 10.0,
        'R1 Significant Strikes': 10.0,
        'Takedowns':             1.5,
        'Fight Time (Mins)':     8.0,
      };
      // Demon line floor gate is handled entirely by demon_pipeline.py.
      // All isDemon=true props pass through here unchanged.
      const ppProps = ppPropsRaw
        .map((p: any) => {
          // Goblins: ALWAYS keep as-is — they are discount lines by design,
          // line floors do NOT apply to goblins.
          if (p.isGoblin) return p;
          // Demons: pass ALL demon props through to the pipeline with isDemon=true.
          // The demon_pipeline.py applies its own line floor gate (DEMON_LINE_FLOOR)
          // with relaxation tiers. Stripping demons here starves the pipeline and
          // breaks the always-2 guarantee.
          if (p.isDemon) {
            return p;
          }
          // Standard props: block trash stats entirely, then apply line floor
          if (BLOCKED_STANDARD_STATS.has(p.statType)) return null;
          const floor = LINE_FLOORS[p.statType] ?? 1.0;
          if (p.lineScore < floor) return null;
          return p;
        })
        .filter(Boolean);

      console.log(`[Orchestrator] ${league}: floor filter — ${ppPropsRaw.length} raw → ${ppProps.length} kept`);
      if (ppProps.length > 0) {
        state.pp.consecutiveFailures = 0;
        state.pp.rateLimited = false;
        state.pp.lastError = null;
        state.lastSuccessAt = now;
        state.currentProvider = 'prizepicks';
        state.fallbackUsed = false;
        console.log(`[Orchestrator] ${league}: PP success — ${ppProps.length} props`);
        return { props: ppProps, providerUsed: 'prizepicks', isFallback: false, fallbackReason: null, pulledAt: nowIso, propCount: ppProps.length, fromCache: false, cooldownMs: null, rateLimited: false };
      }
      ppError = `PP returned 0 props for ${league}`;
      state.pp.consecutiveFailures++;
    } catch (err: any) {
      ppError = err.message ?? `PP pull failed`;
      const is403 = ppError.includes('403') || ppError.includes('1015') || ppError.includes('rate limit') || ppError.includes('captcha') || ppError.includes('IP block') || ppError.includes('Cloudflare') || ppError.includes('error code');
      state.pp.consecutiveFailures++;
      state.pp.rateLimited = is403;
      state.pp.lastError = ppError;
      console.warn(`[Orchestrator] ${league}: PP failed (#${state.pp.consecutiveFailures}): ${ppError}`);
    }
  } else {
    ppError = ppCooling ? `PP cooldown (${Math.round((remainingMs(state.pp, ppCooldownMs) ?? 0) / 60000)}m left)` : 'PP skipped (hard min)';
  }

  // ── Attempt SGO (active fallback when PP fails) ──────────────────────────
  if (sgoAvailable && !sgoCooling) {
    state.sgo.lastAttemptAt = now;
    try {
      console.log(`[Orchestrator] ${league}: PP unavailable — trying SGO`);
      const sgoProps = await pullSGO(league);
      if (sgoProps.length > 0) {
        state.sgo.consecutiveFailures = 0;
        state.sgo.rateLimited = false;
        state.sgo.lastError = null;
        state.lastSuccessAt = now;
        state.currentProvider = 'sportsgameodds';
        state.fallbackUsed = true;
        console.log(`[Orchestrator] ${league}: SGO success — ${sgoProps.length} props`);
        return { props: sgoProps, providerUsed: 'sportsgameodds', isFallback: true, fallbackReason: ppError, pulledAt: nowIso, propCount: sgoProps.length, fromCache: false, cooldownMs: null, rateLimited: false };
      }
      state.sgo.consecutiveFailures++;
    } catch (err: any) {
      const sgoErr = err.message ?? 'SGO failed';
      const is429 = sgoErr.includes('429') || sgoErr.includes('rate limit') || sgoErr.includes('Rate limit');
      state.sgo.consecutiveFailures++;
      state.sgo.rateLimited = is429;
      state.sgo.lastError = sgoErr;
      console.warn(`[Orchestrator] ${league}: SGO failed: ${sgoErr}`);
    }
  }

  // ── Attempt MoneyLine (real player props, PP lines when available) ────────
  const mlAvailable = mlAvailableEarly;
  const mlCooling = isCoolingDown(state.ml, mlCooldownMs);
  if (mlAvailable && !mlCooling) {
    state.ml.lastAttemptAt = now;
    try {
      console.log(`[Orchestrator] ${league}: PP unavailable — trying MoneyLine`);
      const mlProps = await pullMoneyLine(league);
      if (mlProps.length > 0) {
        state.ml.consecutiveFailures = 0;
        state.ml.rateLimited = false;
        state.ml.lastError = null;
        state.lastSuccessAt = now;
        state.currentProvider = 'sportsgameodds'; // reuse existing DataSource type
        state.fallbackUsed = true;
        console.log(`[Orchestrator] ${league}: MoneyLine success — ${mlProps.length} props`);
        return { props: mlProps, providerUsed: 'sportsgameodds', isFallback: true, fallbackReason: ppError, pulledAt: nowIso, propCount: mlProps.length, fromCache: false, cooldownMs: null, rateLimited: false };
      }
      state.ml.consecutiveFailures++;
    } catch (err: any) {
      const mlErr = err.message ?? 'MoneyLine failed';
      const is429 = mlErr.includes('429') || mlErr.includes('rate limit') || mlErr.includes('Rate limit');
      state.ml.consecutiveFailures++;
      state.ml.rateLimited = is429;
      state.ml.lastError = mlErr;
      console.warn(`[Orchestrator] ${league}: MoneyLine failed: ${mlErr}`);
    }
  }

  // ── Both providers failed — serve cache ─────────────────────────────────
  if (hasCachedData) {
    const cdLeft = remainingMs(state.pp, ppCooldownMs);
    console.log(`[Orchestrator] ${league}: all providers failed — serving cache (${cachedRows.length} props)`);
    return {
      props: dbRowsToCanonical(cachedRows, league),
      providerUsed: 'cache',
      isFallback: true,
      fallbackReason: ppError,
      pulledAt: lastPullRecord?.pulledAt ?? nowIso,
      propCount: cachedRows.length,
      fromCache: true,
      cooldownMs: cdLeft,
      rateLimited: state.pp.rateLimited,
    };
  }

  // ── Completely empty ─────────────────────────────────────────────────────
  console.warn(`[Orchestrator] ${league}: all providers failed and DB empty`);
  return { props: [], providerUsed: 'demo', isFallback: true, fallbackReason: ppError ?? 'No data from any provider', pulledAt: nowIso, propCount: 0, fromCache: false, cooldownMs: null, rateLimited: state.pp.rateLimited };
}

// ── Public accessors ─────────────────────────────────────────────────────────
export interface ProviderState {
  lastSuccessfulProvider: DataSource | null;
  lastSuccessfulPullAt: string | null;
  lastPullError: string | null;
  currentProvider: DataSource | null;
  fallbackUsed: boolean;
  rateLimited: boolean;
  consecutiveFailures: number;
  cooldownRemainingMs: number | null;
}

export function getProviderState(league: string): ProviderState {
  const s = getState(league);
  return {
    lastSuccessfulProvider: s.lastSuccessAt ? (s.currentProvider ?? null) : null,
    lastSuccessfulPullAt: s.lastSuccessAt ? new Date(s.lastSuccessAt).toISOString() : null,
    lastPullError: s.pp.lastError,
    currentProvider: s.currentProvider,
    fallbackUsed: s.fallbackUsed,
    rateLimited: s.pp.rateLimited,
    consecutiveFailures: s.pp.consecutiveFailures,
    cooldownRemainingMs: remainingMs(s.pp, ppCooldownMs),
  };
}

export function updateProviderState(_league: string, _result: PullResult): void {
  // no-op — state managed internally
}

export function resetAllCooldowns(): void {
  leagueState.clear();
  console.log('[Orchestrator] All provider cooldowns reset');
}
