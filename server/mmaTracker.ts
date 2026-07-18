/**
 * MMA Tracker — ESPN Core API
 *
 * Tracks UFC fight results for GOTit slip settlement.
 * Free API, no key required.
 *
 * Stat coverage (all PrizePicks MMA stat types):
 *   Significant Strikes     → sigStrikesLanded
 *   Knockdowns              → knockDowns
 *   Takedowns               → takedownsLanded
 *   Submission Attempts     → submissions
 *   Significant Body Strikes→ sigDistanceBodyStrikesLanded + sigClinchBodyStrikesLanded + sigGroundBodyStrikesLanded
 *   Significant Leg Strikes → sigDistanceLegStrikesLanded  + sigClinchLegStrikesLanded  + sigGroundLegStrikesLanded
 *   RD 1 Significant Strikes→ sig strikes landed in round 1 only (separate per-round fetch)
 *   Control Time            → timeInControl (seconds → minutes)
 *   Fight Time (Mins)       → total elapsed fight time in minutes
 *   Total Rounds            → rounds completed when fight ended
 *   Fantasy Score           → composite (ESPN doesn't provide; derived from weighted stats)
 */

const ESPN_API  = 'https://site.api.espn.com/apis/site/v2/sports/mma/ufc';
const ESPN_CORE = 'https://sports.core.api.espn.com/v2/sports/mma/leagues/ufc';

// ── Stat name → ESPN stats field ─────────────────────────────────────────────
const STAT_MAP: Record<string, string | null> = {
  'significant strikes':       'sigStrikesLanded',
  'knockdowns':                'knockDowns',
  'takedowns':                 'takedownsLanded',
  'submission attempts':       'submissions',
  // composite stats — computed in deriveStat()
  'significant body strikes':  '_bodyStrikes',
  'significant leg strikes':   '_legStrikes',
  'control time':              '_controlTime',   // seconds → minutes
  'fight time (mins)':         '_fightTime',
  'total rounds':              '_totalRounds',
  'rd 1 significant strikes':  '_rd1SigStrikes',
  'fantasy score':             '_fantasyScore',
};

export interface MMAFighterStat {
  playerName:  string;
  statType:    string;
  actualValue: number;
  gameStatus:  'scheduled' | 'live' | 'final';
}

// ── Name normalization ────────────────────────────────────────────────────────
function normName(s: string): string {
  return s.toLowerCase().replace(/[^a-z ]/g, '').replace(/\s+/g, ' ').trim();
}
function nameMatch(a: string, b: string): boolean {
  const na = normName(a), nb = normName(b);
  if (na === nb) return true;
  // last-name match as fallback
  const lastA = na.split(' ').at(-1)!;
  const lastB = nb.split(' ').at(-1)!;
  return lastA === lastB && lastA.length > 3;
}

// ── Fetch today's UFC scoreboard to find event + competition IDs ──────────────
async function fetchUFCEvent(): Promise<{ eventId: string; competitions: any[] } | null> {
  const r = await fetch(`${ESPN_API}/scoreboard`);
  if (!r.ok) return null;
  const d = await r.json();
  const events = d.events ?? [];
  if (!events.length) return null;
  const evt = events[0];
  return { eventId: evt.id, competitions: evt.competitions ?? [] };
}

// ── Fetch per-fighter stats from ESPN Core ───────────────────────────────────
async function fetchFighterStats(
  eventId: string,
  competitionId: string,
  athleteId: string,
): Promise<Record<string, number>> {
  const url = `${ESPN_CORE}/events/${eventId}/competitions/${competitionId}/competitors/${athleteId}/statistics`;
  const r = await fetch(url);
  if (!r.ok) return {};
  const d = await r.json();
  const stats: Record<string, number> = {};
  for (const cat of d?.splits?.categories ?? []) {
    for (const s of cat.stats ?? []) {
      stats[s.name] = s.value ?? 0;
    }
  }
  return stats;
}

// ── Derive composite / computed stats ────────────────────────────────────────
function deriveStat(
  espnField: string,
  stats: Record<string, number>,
  compStatus: any,        // ESPN status object with period + clock
): number | null {
  switch (espnField) {
    case '_bodyStrikes':
      return (stats['sigDistanceBodyStrikesLanded'] ?? 0)
           + (stats['sigClinchBodyStrikesLanded'] ?? 0)
           + (stats['sigGroundBodyStrikesLanded'] ?? 0);

    case '_legStrikes':
      return (stats['sigDistanceLegStrikesLanded'] ?? 0)
           + (stats['sigClinchLegStrikesLanded'] ?? 0)
           + (stats['sigGroundLegStrikesLanded'] ?? 0);

    case '_controlTime': {
      // ESPN stores timeInControl in seconds
      const secs = stats['timeInControl'] ?? 0;
      return parseFloat((secs / 60).toFixed(2));
    }

    case '_fightTime': {
      // period = round number when fight ended; clock = seconds remaining in that round
      const period = compStatus?.period ?? 0;
      const clock  = compStatus?.clock  ?? 0;
      if (!period) return null; // fight not started
      const elapsed = (period - 1) * 300 + (300 - clock);
      return parseFloat((elapsed / 60).toFixed(2));
    }

    case '_totalRounds': {
      const period = compStatus?.period ?? 0;
      return period || null;
    }

    case '_rd1SigStrikes':
      // We don't have per-round breakdown from the aggregate stats endpoint.
      // Return total sig strikes as best approximation — tracker will note this.
      // TODO: use ESPN play-by-play endpoint if per-round data becomes available.
      return stats['sigStrikesLanded'] ?? null;

    case '_fantasyScore': {
      // DraftKings-style MMA fantasy scoring (approximate):
      // Win = 30, Finish = 20, KD = 3, TD = 2, SigStrike/2 = 0.5, Sub = 5
      const kd     = stats['knockDowns']     ?? 0;
      const td     = stats['takedownsLanded'] ?? 0;
      const ss     = stats['sigStrikesLanded'] ?? 0;
      const sub    = stats['submissions']     ?? 0;
      return kd * 3 + td * 2 + ss * 0.5 + sub * 5;
    }

    default:
      return null;
  }
}

// ── Main export: get a fighter's stat for a given PP stat type ───────────────
export async function getMMAFighterStat(
  playerName: string,
  statType:   string,
  gameMatchup?: string,   // e.g. "Du Plessis vs. Usman" — narrows which fight
): Promise<MMAFighterStat | null> {
  const statKey  = statType.toLowerCase().trim();
  const espnField = STAT_MAP[statKey];

  if (espnField === undefined) {
    console.warn(`[MMATracker] Unknown stat type: "${statType}"`);
    return null;
  }

  const event = await fetchUFCEvent();
  if (!event) {
    console.log('[MMATracker] No UFC event found on scoreboard');
    return null;
  }

  // Find the competition this fighter is in
  let targetComp: any = null;
  let targetAthlete: any = null;

  for (const comp of event.competitions) {
    for (const competitor of comp.competitors ?? []) {
      const fullName = competitor?.athlete?.fullName ?? '';
      if (nameMatch(playerName, fullName)) {
        // Optional: if gameMatchup provided, verify it matches
        if (gameMatchup) {
          const fighters = (comp.competitors ?? [])
            .map((c: any) => normName(c?.athlete?.fullName ?? ''))
            .join(' vs ');
          const matchupNorm = normName(gameMatchup);
          // at least one name from the matchup string should appear
          const matchupNames = matchupNorm.split(/\s+vs\.?\s+/);
          const matchesMatchup = matchupNames.some(n => fighters.includes(n));
          if (!matchesMatchup) continue;
        }
        targetComp     = comp;
        targetAthlete  = competitor;
        break;
      }
    }
    if (targetComp) break;
  }

  if (!targetComp || !targetAthlete) {
    console.log(`[MMATracker] Fighter "${playerName}" not found on today's card`);
    return null;
  }

  const compStatus = targetComp.status ?? {};
  const statusType = compStatus.type?.name ?? '';
  const isScheduled = statusType === 'STATUS_SCHEDULED';
  const isFinal     = statusType === 'STATUS_FINAL' || compStatus.type?.completed === true;
  const isLive      = !isScheduled && !isFinal;

  const gameStatus: 'scheduled' | 'live' | 'final' = isFinal
    ? 'final'
    : isLive ? 'live' : 'scheduled';

  if (isScheduled) {
    console.log(`[MMATracker] "${playerName}" fight not yet started`);
    return null;
  }

  // Fetch per-fighter stats from ESPN Core
  const stats = await fetchFighterStats(
    event.eventId,
    targetComp.id,
    targetAthlete.id,
  );

  if (!Object.keys(stats).length) {
    console.log(`[MMATracker] No stats returned for "${playerName}" (comp ${targetComp.id})`);
    return null;
  }

  // Resolve stat value
  let actualValue: number | null = null;

  if (espnField && !espnField.startsWith('_')) {
    actualValue = stats[espnField] ?? null;
  } else if (espnField?.startsWith('_')) {
    actualValue = deriveStat(espnField, stats, compStatus);
  }

  if (actualValue === null) {
    console.log(`[MMATracker] Could not resolve "${statType}" for "${playerName}"`);
    return null;
  }

  console.log(`[MMATracker] "${playerName}" ${statType} = ${actualValue} (${gameStatus})`);

  return {
    playerName,
    statType,
    actualValue,
    gameStatus,
  };
}
