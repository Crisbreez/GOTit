/**
 * GOTit Slip Tracker
 *
 * Runs on a 90-second interval and:
 *  1. Promotes pending slips → live when game start time has passed
 *  2. Fetches real MLB stats for each live leg and updates actualValue + hit/miss
 *  3. Settles live slips → settled_win / settled_loss when all legs are resolved
 *
 * Only runs for MLB right now. NBA/NFL/MMA stay pending until real tracking added.
 *
 * Game narrowing fix: each leg now stores its own game_matchup column.
 * The tracker passes leg.gameMatchup to getPlayerStat so the correct game is
 * identified even when multiple games are active simultaneously.
 */

import { storage } from './storage';
import { getPlayerStat } from './mlbTracker';
import { getMMAFighterStat } from './mmaTracker';

const TRACK_INTERVAL_MS = 90_000; // 90 seconds

// ── Settle a single MMA leg against ESPN data ───────────────────────────────
async function trackMMALeg(leg: any): Promise<void> {
  if (leg.status === 'dnp') return;
  const alreadySettled = leg.status === 'hit' || leg.status === 'miss';

  console.log(`[SlipTracker] MMA Leg ${leg.id}: fighter="${leg.playerName}" stat="${leg.statType}" line=${leg.lineScore} dir=${leg.direction} currentStatus=${leg.status}`);

  const result = await getMMAFighterStat(
    leg.playerName,
    leg.statType,
    leg.gameMatchup ?? undefined,
  );

  if (!result) {
    console.log(`[SlipTracker] MMA Leg ${leg.id}: no result yet`);
    return;
  }

  const actual = result.actualValue;
  const line   = leg.lineScore;
  const dir    = (leg.direction ?? 'over').toLowerCase();

  console.log(`[SlipTracker] MMA Leg ${leg.id}: actual=${actual} vs line=${line} (${dir}) status=${result.gameStatus}`);

  if (result.gameStatus === 'live') {
    if (alreadySettled) {
      console.log(`[SlipTracker] MMA Leg ${leg.id}: was ${leg.status} but fight still live — reverting to live, actual=${actual}`);
    }
    await storage.updateLegStatus(leg.id, 'live', actual);
    return;
  }

  // Fight is final — settle definitively
  const hit = dir === 'over' ? actual > line : actual < line;
  await storage.updateLegStatus(leg.id, hit ? 'hit' : 'miss', actual);
  console.log(`[SlipTracker] MMA Leg ${leg.id}: SETTLED → ${hit ? 'HIT' : 'MISS'} (actual=${actual} ${dir} ${line})`);
}

// ── Settle a single MLB leg against real data ─────────────────────────────────
async function trackMLBLeg(leg: any): Promise<void> {
  // DNP legs are permanently voided — never re-check
  if (leg.status === 'dnp') return;
  // hit/miss legs are ONLY skipped if the game is confirmed final.
  // If they were settled prematurely (game was still live), we re-check
  // and correct the actual value. We detect this by letting the stat
  // fetch run — if it returns gameStatus='live' we revert to live.
  // If gameStatus='final', the settled value stays.
  const alreadySettled = leg.status === 'hit' || leg.status === 'miss';

  console.log(`[SlipTracker] Leg ${leg.id}: player="${leg.playerName}" stat="${leg.statType}" line=${leg.lineScore} dir=${leg.direction} matchup="${leg.gameMatchup || 'unknown'}" currentStatus=${leg.status}`);

  const result = await getPlayerStat(
    leg.playerName,
    leg.statType,
    leg.gameMatchup ?? undefined,
  );

  if (!result) {
    console.log(`[SlipTracker] Leg ${leg.id}: no result yet (game not started or boxscore not ready)`);
    return;
  }

  const actual = result.actualValue;
  const line = leg.lineScore;
  const dir = (leg.direction ?? 'over').toLowerCase();

  console.log(`[SlipTracker] Leg ${leg.id}: actual=${actual} vs line=${line} (${dir}) — gameStatus=${result.gameStatus}`);

  if (result.gameStatus === 'live') {
    // Game still in progress — always update actual value and revert to 'live'
    // even if a previous cycle prematurely settled this leg
    if (alreadySettled) {
      console.log(`[SlipTracker] Leg ${leg.id}: was ${leg.status} but game is still live — reverting to live, actual=${actual}`);
    } else {
      console.log(`[SlipTracker] Leg ${leg.id}: status=live, actualValue=${actual}`);
    }
    await storage.updateLegStatus(leg.id, 'live', actual);
    return;
  }

  // Game is final — settle definitively
  const hit = dir === 'over' ? actual > line : actual < line;
  await storage.updateLegStatus(leg.id, hit ? 'hit' : 'miss', actual);
  console.log(`[SlipTracker] Leg ${leg.id}: SETTLED → ${hit ? 'HIT' : 'MISS'} (actual=${actual} ${dir} ${line})`);
}

// ── Try to settle an entire slip ──────────────────────────────────────────────
async function trackSlip(slip: any): Promise<void> {
  const legs = await storage.getLegsBySlip(slip.id);
  if (!legs.length) return;

  console.log(`[SlipTracker] Slip ${slip.id} (${slip.league}): status=${slip.status} legs=${legs.length} matchup="${slip.gameMatchup}" start=${slip.gameStartTime}`);

  // Promote pending → live if ANY leg's game has started (supports cross-game slips)
  if (slip.status === 'pending') {
    const now = Date.now();
    const legs = await storage.getLegsBySlip(slip.id);
    // For cross-game slips, use the earliest leg game start time
    const legStartTimes = legs
      .map(l => (l as any).gameStartTime ? new Date((l as any).gameStartTime).getTime() : null)
      .filter((t): t is number => t !== null);
    const slipStart = slip.gameStartTime ? new Date(slip.gameStartTime).getTime() : null;
    const earliestStart = legStartTimes.length
      ? Math.min(...legStartTimes)
      : slipStart;
    if (earliestStart && now >= earliestStart) {
      console.log(`[SlipTracker] Slip ${slip.id}: promoting pending → live (earliest game started)`);
      await storage.updateSlipStatus(slip.id, 'live');
    } else {
      const waitMs = earliestStart ? earliestStart - now : null;
      console.log(`[SlipTracker] Slip ${slip.id}: still pending — game starts in ${waitMs ? Math.round(waitMs / 60000) + 'min' : 'unknown time'}`);
      return;
    }
  }

  // Route each league to the correct tracker
  if (slip.league !== 'MLB' && slip.league !== 'MMA') {
    console.log(`[SlipTracker] Slip ${slip.id}: skipping stat settlement (${slip.league} tracking not yet implemented)`);
    return;
  }

  // Track each unresolved leg
  for (const leg of legs) {
    if (leg.status !== 'hit' && leg.status !== 'miss' && leg.status !== 'dnp') {
      if (slip.league === 'MMA') {
        await trackMMALeg(leg);
      } else {
        await trackMLBLeg(leg);
      }
    }
  }

  // Re-fetch updated legs
  const updatedLegs = await storage.getLegsBySlip(slip.id);

  // DNP legs are voided — exclude from win/loss calculation (PrizePicks behavior)
  const activeLeg = updatedLegs.filter(l => l.status !== 'dnp');
  const resolved = activeLeg.filter(l => l.status === 'hit' || l.status === 'miss');
  const allResolved = resolved.length === activeLeg.length && activeLeg.length > 0;
  const dnpCount = updatedLegs.length - activeLeg.length;

  console.log(`[SlipTracker] Slip ${slip.id}: ${resolved.length}/${activeLeg.length} active legs resolved, ${dnpCount} DNP voided`);

  if (allResolved) {
    const allHit = activeLeg.every(l => l.status === 'hit');
    await storage.updateSlipStatus(slip.id, allHit ? 'settled_win' : 'settled_loss', {
      settledAt: new Date().toISOString(),
    });
    console.log(`[SlipTracker] Slip ${slip.id} SETTLED → ${allHit ? 'WIN 🌟' : 'LOSS'} (${dnpCount} leg(s) voided DNP)`);
  }
}

// ── Main tracking loop ────────────────────────────────────────────────────────
export async function runTrackingCycle(): Promise<void> {
  const activeSlips = await storage.getSlips(['pending', 'live']);
  if (!activeSlips.length) return;

  console.log(`[SlipTracker] Tracking ${activeSlips.length} active slip(s)…`);

  for (const slip of activeSlips) {
    try {
      await trackSlip(slip);
    } catch (e: any) {
      console.warn(`[SlipTracker] Error tracking slip ${slip.id}: ${e.message}`);
    }
  }
}

// ── Kick off the interval ─────────────────────────────────────────────────────
let trackingTimer: ReturnType<typeof setInterval> | null = null;

export function startSlipTracker(): void {
  if (trackingTimer) return; // already running
  console.log(`[SlipTracker] Started — tracking every ${TRACK_INTERVAL_MS / 1000}s`);

  // Run once immediately after a short delay (let server finish booting)
  setTimeout(() => runTrackingCycle().catch(() => {}), 5000);

  trackingTimer = setInterval(() => {
    runTrackingCycle().catch(() => {});
  }, TRACK_INTERVAL_MS);
}

export function stopSlipTracker(): void {
  if (trackingTimer) {
    clearInterval(trackingTimer);
    trackingTimer = null;
  }
}
