/**
 * GOTit Slip Tracker — reconcile_leg pattern
 *
 * Every 90 seconds:
 *  1. Promotes pending slips → live when earliest leg game starts
 *  2. For each non-void leg, fetches the official feed stat
 *  3. Derives expected_status + expected_actual from the feed
 *  4. Writes an audit row (console log) comparing displayed vs expected
 *  5. Overwrites the leg if there is any mismatch (status OR actual)
 *  6. Settles slip when all active legs are resolved
 *
 * reconcile_leg is the single source of truth — nothing settles
 * a leg except reconcile_leg seeing gameStatus='final' from the feed.
 */

import { storage } from './storage';
import { getPlayerStat, PlayerGameStat } from './mlbTracker';
import { getMMAFighterStat } from './mmaTracker';

const TRACK_INTERVAL_MS = 90_000; // 90 seconds

// ─────────────────────────────────────────────────────────────────────────────
// 1. derive_expected_status
//    Maps (feed game status, actual value, line, direction) → leg status string
// ─────────────────────────────────────────────────────────────────────────────
function deriveExpectedStatus(
  sourceGameStatus: 'live' | 'final',
  sourceActual: number | null,
  lineScore: number,
  direction: string,
): string {
  if (sourceGameStatus === 'live') return 'live';
  // Game is final — settle
  if (sourceActual === null) return 'live'; // no data yet, stay live
  const dir = (direction ?? 'over').toLowerCase();
  const hit = dir === 'over' ? sourceActual > lineScore : sourceActual < lineScore;
  return hit ? 'hit' : 'miss';
}

// ─────────────────────────────────────────────────────────────────────────────
// 2. classify_mismatch
// ─────────────────────────────────────────────────────────────────────────────
function classifyMismatch(
  leg: any,
  expectedStatus: string,
  expectedActual: number | null,
): string {
  const statMismatch   = leg.actualValue !== expectedActual;
  const statusMismatch = leg.status      !== expectedStatus;
  if (statMismatch && statusMismatch) return 'status_and_actual';
  if (statusMismatch)                 return 'status_only';
  if (statMismatch)                   return 'actual_only';
  return 'none';
}

// ─────────────────────────────────────────────────────────────────────────────
// 3. reconcile_leg — the single settlement function
// ─────────────────────────────────────────────────────────────────────────────
async function reconcileLeg(leg: any, league: 'MLB' | 'MMA'): Promise<void> {
  // void / dnp legs are permanently settled — never touch them
  if (leg.status === 'dnp' || leg.status === 'void') return;

  // ── Fetch official feed ───────────────────────────────────────────────────
  let feedResult: PlayerGameStat | null = null;
  try {
    if (league === 'MMA') {
      feedResult = await getMMAFighterStat(leg.playerName, leg.statType, leg.gameMatchup ?? undefined);
    } else {
      feedResult = await getPlayerStat(leg.playerName, leg.statType, leg.gameMatchup ?? undefined);
    }
  } catch (e: any) {
    console.warn(`[reconcile] Leg ${leg.id}: feed fetch error — ${e.message}`);
    return;
  }

  if (!feedResult) {
    // No data yet — game hasn't started or boxscore not ready
    console.log(`[reconcile] Leg ${leg.id} ${leg.playerName} ${leg.statType}: no feed result yet`);
    return;
  }

  const sourceGameStatus = feedResult.gameStatus;   // 'live' | 'final'
  const sourceActual     = feedResult.actualValue;  // number

  // ── Derive what the leg SHOULD look like ─────────────────────────────────
  const expectedStatus = deriveExpectedStatus(
    sourceGameStatus,
    sourceActual,
    leg.lineScore,
    leg.direction ?? 'over',
  );
  const expectedActual = sourceActual;

  // ── Audit comparison ──────────────────────────────────────────────────────
  const isMatch = leg.status === expectedStatus && leg.actualValue === expectedActual;
  const mismatchType = isMatch ? 'none' : classifyMismatch(leg, expectedStatus, expectedActual);

  console.log(
    `[reconcile] Leg ${leg.id} | ${leg.playerName} | ${leg.statType} | ` +
    `feed=${sourceGameStatus} actual=${sourceActual} | ` +
    `displayed: status=${leg.status} actual=${leg.actualValue} | ` +
    `expected: status=${expectedStatus} actual=${expectedActual} | ` +
    `match=${isMatch}${isMatch ? '' : ' mismatch=' + mismatchType} action=${isMatch ? 'none' : 'overwrite'}`
  );

  // ── Write to DB ───────────────────────────────────────────────────────────
  const now = new Date().toISOString();

  if (!isMatch) {
    // Overwrite leg with correct values + flag tracking_error
    await (storage as any).reconcileLeg(leg.id, expectedStatus, expectedActual, now, true);
  } else {
    // Touch last_checked_at so we know it was verified this cycle
    await (storage as any).reconcileLeg(leg.id, leg.status, leg.actualValue, now, false);
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// 4. trackSlip — drives reconcile_leg for every leg in a slip
// ─────────────────────────────────────────────────────────────────────────────
async function trackSlip(slip: any): Promise<void> {
  const legs = await storage.getLegsBySlip(slip.id);
  if (!legs.length) return;

  console.log(`[SlipTracker] Slip ${slip.id} (${slip.league}): status=${slip.status} legs=${legs.length}`);

  // ── Promote pending → live when earliest game has started ────────────────
  if (slip.status === 'pending') {
    const now = Date.now();
    const legStartTimes = legs
      .map(l => (l as any).gameStartTime ? new Date((l as any).gameStartTime).getTime() : null)
      .filter((t): t is number => t !== null);
    const slipStart = slip.gameStartTime ? new Date(slip.gameStartTime).getTime() : null;
    const earliestStart = legStartTimes.length ? Math.min(...legStartTimes) : slipStart;
    if (earliestStart && now >= earliestStart) {
      console.log(`[SlipTracker] Slip ${slip.id}: promoting pending → live`);
      await storage.updateSlipStatus(slip.id, 'live');
    } else {
      const waitMin = earliestStart ? Math.round((earliestStart - now) / 60000) : null;
      console.log(`[SlipTracker] Slip ${slip.id}: still pending — starts in ${waitMin != null ? waitMin + 'min' : 'unknown'}`);
      return;
    }
  }

  // ── Only MLB and MMA have live tracking ──────────────────────────────────
  if (slip.league !== 'MLB' && slip.league !== 'MMA') {
    console.log(`[SlipTracker] Slip ${slip.id}: ${slip.league} tracking not yet implemented`);
    return;
  }

  // ── Reconcile every non-void leg ─────────────────────────────────────────
  for (const leg of legs) {
    await reconcileLeg(leg, slip.league as 'MLB' | 'MMA');
  }

  // ── Check for full settlement ─────────────────────────────────────────────
  const updatedLegs  = await storage.getLegsBySlip(slip.id);
  const activeLegs   = updatedLegs.filter(l => l.status !== 'dnp' && l.status !== 'void');
  const resolvedLegs = activeLegs.filter(l => l.status === 'hit' || l.status === 'miss');
  const dnpCount     = updatedLegs.length - activeLegs.length;
  const allResolved  = resolvedLegs.length === activeLegs.length && activeLegs.length > 0;

  console.log(`[SlipTracker] Slip ${slip.id}: ${resolvedLegs.length}/${activeLegs.length} resolved, ${dnpCount} voided`);

  if (allResolved) {
    const allHit = activeLegs.every(l => l.status === 'hit');
    await storage.updateSlipStatus(slip.id, allHit ? 'settled_win' : 'settled_loss', {
      settledAt: new Date().toISOString(),
    });
    console.log(`[SlipTracker] Slip ${slip.id} → ${allHit ? 'WIN 🌟' : 'LOSS'} (${dnpCount} voided)`);
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// 5. Main tracking loop
// ─────────────────────────────────────────────────────────────────────────────
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

let trackingTimer: ReturnType<typeof setInterval> | null = null;

export function startSlipTracker(): void {
  if (trackingTimer) return;
  console.log(`[SlipTracker] Started — every ${TRACK_INTERVAL_MS / 1000}s`);
  setTimeout(() => runTrackingCycle().catch(() => {}), 5000);
  trackingTimer = setInterval(() => { runTrackingCycle().catch(() => {}); }, TRACK_INTERVAL_MS);
}

export function stopSlipTracker(): void {
  if (trackingTimer) { clearInterval(trackingTimer); trackingTimer = null; }
}
