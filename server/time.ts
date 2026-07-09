/**
 * Central Time utilities for GOTit.
 *
 * All date logic (schedule lookups, prop filtering, "today" boundaries) uses
 * US Central time (CDT = UTC-5, CST = UTC-6). We hard-code CDT offset (-5h)
 * because the MLB season runs in CDT and PrizePicks operates on Eastern/Central.
 *
 * Stored timestamps remain as UTC ISO Z strings in the DB (correct for ordering
 * and comparison). Only the "what day is today" boundary uses Central.
 */

// CDT = UTC-5. Covers the MLB season (Mar–Oct).
// In Nov–Mar (CST) this would be UTC-6, but GOTit is primarily a live-season app.
const CDT_OFFSET_MS = -5 * 60 * 60 * 1000;

/** Current date string in Central time: "2026-07-08" */
export function centralToday(): string {
  const centralNow = new Date(Date.now() + CDT_OFFSET_MS);
  return centralNow.toISOString().slice(0, 10);
}

/** Yesterday's date string in Central time: "2026-07-07" */
export function centralYesterday(): string {
  const centralNow = new Date(Date.now() + CDT_OFFSET_MS);
  const yest = new Date(centralNow.getTime() - 24 * 60 * 60 * 1000);
  return yest.toISOString().slice(0, 10);
}

/**
 * Start of today in Central time, as a UTC ISO string.
 * e.g. "2026-07-08" CDT midnight = "2026-07-08T05:00:00.000Z"
 */
export function centralTodayStartUTC(): string {
  const today = centralToday(); // "2026-07-08"
  // Midnight CDT = 05:00 UTC (CDT is UTC-5)
  return new Date(`${today}T05:00:00.000Z`).toISOString();
}

/** Current time as UTC ISO string (for DB timestamps) */
export function nowISO(): string {
  return new Date().toISOString();
}
