/**
 * MLB Stats API Tracker
 * Free API — no key needed.
 * Resolves player stats from live and final games.
 *
 * Used to settle GOTit slip legs against real box score data.
 */

const MLB_API = 'https://statsapi.mlb.com/api/v1';

// ── Stat type → MLB API field mapping ────────────────────────────────────────
// Maps PrizePicks/GOTit stat names to MLB Stats API batting/pitching fields

const BATTING_STAT_MAP: Record<string, string> = {
  'hits': 'hits',
  'home runs': 'homeRuns',
  'home run': 'homeRuns',
  'rbis': 'rbi',
  'rbi': 'rbi',
  'runs scored': 'runs',
  'runs': 'runs',
  'strikeouts': 'strikeOuts',        // batter strikeouts
  'strikeouts (batter)': 'strikeOuts',
  'hitter strikeouts': 'strikeOuts',
  'walks': 'baseOnBalls',
  'total bases': 'totalBases',
  'singles': 'hits',                  // derived: hits - doubles - triples - homeRuns
  'doubles': 'doubles',
  'stolen bases': 'stolenBases',
  'plate appearances': 'plateAppearances',
  'at bats': 'atBats',
  'hit by pitch': 'hitByPitch',
  'triples': 'triples',
};

const PITCHING_STAT_MAP: Record<string, string> = {
  'strikeouts (pitcher)': 'strikeOuts',
  'pitcher strikeouts': 'strikeOuts',
  'earned runs': 'earnedRuns',
  'earned runs allowed': 'earnedRuns',
  'hits allowed': 'hits',
  'walks allowed': 'baseOnBalls',
  'outs recorded': 'outs',
  'pitching outs': 'outs',
  'innings pitched': 'inningsPitched',  // string like "6.1", convert to outs
  'pitches thrown': 'numberOfPitches',
  'pitches': 'numberOfPitches',
};

export interface PlayerGameStat {
  playerName: string;
  statType: string;
  actualValue: number;
  gameStatus: 'scheduled' | 'live' | 'final';
  gamePk: number;
}

// ── Fuzzy player name match ───────────────────────────────────────────────────
function nameMatch(a: string, b: string): boolean {
  const normalize = (s: string) => s.toLowerCase()
    .replace(/['.]/g, '')   // remove apostrophes and periods first
    .replace(/[^a-z ]/g, '')
    .replace(/\s+/g, ' ')
    .trim();
  const na = normalize(a);
  const nb = normalize(b);
  if (na === nb) return true;

  // Strip common suffixes (jr, sr, ii, iii) before comparing
  const stripSuffix = (s: string) => s.replace(/\s+(jr|sr|ii|iii|iv)$/, '').trim();
  const ca = stripSuffix(na);
  const cb = stripSuffix(nb);
  if (ca === cb) return true;

  // Last name match (on suffix-stripped name, must be > 2 chars)
  const lastA = ca.split(' ').pop() ?? '';
  const lastB = cb.split(' ').pop() ?? '';
  if (lastA.length > 2 && lastA === lastB) return true;

  // First-name prefix match: "Vlad" matches "Vladimir" — one starts with the other
  const firstA = ca.split(' ')[0];
  const firstB = cb.split(' ')[0];
  if (lastA === lastB && lastA.length > 2) return true; // already handled above
  if (lastA === lastB &&
      (firstA.startsWith(firstB) || firstB.startsWith(firstA)) &&
      Math.min(firstA.length, firstB.length) >= 3) return true;

  return false;
}

// ── PrizePicks Fantasy Score formulas (official) ─────────────────────────────
// Hitter: Single=3, Double=5, Triple=8, HR=10, Run=2, RBI=2, BB=2, HBP=2, SB=5
function calcHitterFantasyScore(batting: Record<string, any>): number {
  const h   = batting['hits']        ?? 0;
  const d   = batting['doubles']     ?? 0;
  const t   = batting['triples']     ?? 0;
  const hr  = batting['homeRuns']    ?? 0;
  const rbi = batting['rbi']         ?? 0;
  const r   = batting['runs']        ?? 0;
  const bb  = batting['baseOnBalls'] ?? 0;
  const sb  = batting['stolenBases'] ?? 0;
  const hbp = batting['hitByPitch']  ?? 0;
  const singles = Math.max(0, h - d - t - hr);
  // Official PrizePicks scoring:
  // Single=3, Double=5, Triple=8, HR=10, Run=2, RBI=2, BB=2, HBP=2, SB=5
  return singles * 3 + d * 5 + t * 8 + hr * 10 + rbi * 2 + r * 2 + bb * 2 + sb * 5 + hbp * 2;
}

// Pitcher Fantasy Score: Out=1, K=3, W=+6, QualityStart=+4, ER=-3
// Quality Start = 6+ IP and ≤3 ER
// IP stored as "6.1" string where .1 = 1 out, .2 = 2 outs
function calcPitcherFantasyScore(pitching: Record<string, any>): number {
  const ipStr = String(pitching['inningsPitched'] ?? '0');
  const parts = ipStr.split('.');
  const fullInnings = parseInt(parts[0]) || 0;
  const partialOuts = parseInt(parts[1] ?? '0') || 0;
  const totalOuts = fullInnings * 3 + partialOuts;
  const k  = pitching['strikeOuts'] ?? 0;
  const w  = pitching['wins']       ?? 0;
  const er = pitching['earnedRuns'] ?? 0;
  // Official PrizePicks Pitcher Fantasy Score (NO Quality Start bonus):
  // Out=+1, K=+3, Win=+6, EarnedRun=-3
  return totalOuts * 1 + k * 3 + w * 6 + er * -3;
}

// ── Resolve stat value from MLB player stats object ───────────────────────────
function resolveStatValue(
  statType: string,
  batting: Record<string, any>,
  pitching: Record<string, any>
): number | null {
  const key = statType.toLowerCase().trim();

  // ── PrizePicks composite fantasy score stats ──────────────────────────────
  if (key === 'hitter fantasy score' || key === 'hitter fantasy pts') {
    if (Object.keys(batting).length === 0) return null; // no batting data yet
    return calcHitterFantasyScore(batting);
  }

  if (key === 'pitcher fantasy score' || key === 'pitcher fantasy pts') {
    if (Object.keys(pitching).length === 0) return null; // not a pitcher
    return calcPitcherFantasyScore(pitching);
  }

  // ── Hits + Runs + RBIs composite ─────────────────────────────────────────
  if (key === 'hits+runs+rbis' || key === 'hits + runs + rbis' || key === 'h+r+rbi') {
    if (Object.keys(batting).length === 0) return null;
    return (batting['hits'] ?? 0) + (batting['runs'] ?? 0) + (batting['rbi'] ?? 0);
  }

  // Try pitching first if stat is pitching-specific
  const pitchField = PITCHING_STAT_MAP[key];
  if (pitchField && Object.keys(pitching).length > 0) {
    const val = pitching[pitchField];
    if (val != null) {
      // innings pitched is a string like "6.1" → convert to decimal outs
      if (pitchField === 'inningsPitched') {
        const parts = String(val).split('.');
        return parseInt(parts[0]) * 3 + (parseInt(parts[1] ?? '0') || 0);
      }
      return typeof val === 'number' ? val : parseFloat(val);
    }
  }

  // Try batting
  const batField = BATTING_STAT_MAP[key];
  if (batField) {
    // Singles: hits - doubles - triples - homeRuns
    if (key === 'singles') {
      const h = batting['hits'] ?? 0;
      const d = batting['doubles'] ?? 0;
      const t = batting['triples'] ?? 0;
      const hr = batting['homeRuns'] ?? 0;
      return h - d - t - hr;
    }
    const val = batting[batField];
    if (val != null) return typeof val === 'number' ? val : parseFloat(val);
  }

  // Try pitching strikeouts for generic "strikeouts" if batting has none
  if (key === 'strikeouts' && Object.keys(pitching).length > 0) {
    const val = pitching['strikeOuts'];
    if (val != null) return typeof val === 'number' ? val : parseFloat(val);
  }

  return null;
}

// ── Abbreviation → substring map for teams whose abbr doesn't appear in their full name
const ABBR_MAP: Record<string, string> = {
  CHC: 'CUBS',
  CWS: 'WHITE SOX',
  KCR: 'ROYALS',
  KCA: 'ROYALS',
  SDP: 'PADRES',
  SD:  'PADRES',
  SFG: 'GIANTS',
  SF:  'GIANTS',
  TBR: 'RAYS',
  TBA: 'RAYS',
  TB:  'RAYS',
  WSN: 'NATIONALS',
  WAS: 'NATIONALS',
  LAA: 'ANGELS',
  STL: 'CARDINALS',
  NYY: 'YANKEES',
  NYM: 'METS',
  LAD: 'DODGERS',
  MIN: 'TWINS',
  CLE: 'GUARDIANS',
  DET: 'TIGERS',
  HOU: 'ASTROS',
  ATL: 'BRAVES',
  PHI: 'PHILLIES',
  BOS: 'RED SOX',
  BAL: 'ORIOLES',
  TOR: 'BLUE JAYS',
  MIL: 'BREWERS',
  CHW: 'WHITE SOX',
  MIA: 'MARLINS',
  COL: 'ROCKIES',
  ARI: 'DIAMONDBACKS',
  AZ:  'DIAMONDBACKS',
  SEA: 'MARINERS',
  OAK: 'ATHLETICS',
  ATH: 'ATHLETICS',
  TEX: 'RANGERS',
  PIT: 'PIRATES',
  CIN: 'REDS',
  KC:  'ROYALS',
  LAD: 'DODGERS',
  NYY: 'YANKEES',
  NYM: 'METS',
  OAK: 'ATHLETICS',
  ATH: 'ATHLETICS',
  AZ:  'DIAMONDBACKS',
  ARI: 'DIAMONDBACKS',
  MIL: 'BREWERS',
  CLE: 'GUARDIANS',
  // Two-letter abbrs that don't substring-match full team names
  KC:  'ROYALS',
};

// ── Parse matchup string into search tokens ───────────────────────────────────
// Handles both "Away vs Home" (full names) and "AZ @ LAD" (abbr) formats.
// Returns an array of uppercase tokens to test against team names.
function parseMatchupTokens(matchup: string): string[] {
  if (!matchup) return [];
  const sep = matchup.includes(' vs ') ? ' vs '
    : matchup.includes('@') ? '@'
    : matchup.includes('/') ? '/'
    : null;
  if (!sep) return [matchup.trim().toUpperCase()];
  return matchup.split(sep).map(s => s.trim().toUpperCase()).filter(Boolean);
}

// ── Check if a game's teams match any of the tokens ──────────────────────────
function gameMatchesMatchup(
  awayTeam: string,
  homeTeam: string,
  tokens: string[]
): boolean {
  const away = awayTeam.toUpperCase();
  const home = homeTeam.toUpperCase();
  // Each token must match at least one team.
  // Resolve abbreviations via ABBR_MAP before falling back to direct substring.
  return tokens.every(t => {
    const resolved = ABBR_MAP[t] ?? t;
    return away.includes(resolved) || home.includes(resolved)
        || away.includes(t)       || home.includes(t);
  });
}

// ── Fetch today's schedule with game PKs ──────────────────────────────────────
export async function getTodayGames(): Promise<Array<{
  gamePk: number;
  status: string;
  awayTeam: string;
  homeTeam: string;
  gameDate: string;
}>> {
  // Smart date resolution: try today (UTC), then yesterday.
  // MLB games run until ~midnight ET; after midnight UTC the schedule may already
  // show tomorrow's games. If no in-progress/final games found for today, fall back
  // to yesterday so active slips can still be settled.
  async function fetchGamesForDate(dateStr: string) {
    const resp = await fetch(`${MLB_API}/schedule?sportId=1&date=${dateStr}`);
    if (!resp.ok) return [];
    const json = await resp.json();
    return (json.dates?.[0]?.games ?? []).map((g: any) => ({
      gamePk: g.gamePk,
      status: g.status.detailedState,
      awayTeam: g.teams.away.team.name,
      homeTeam: g.teams.home.team.name,
      gameDate: dateStr,
    }));
  }

  const now = new Date();

  // Use US/Central local date — games at 9 PM CDT on July 8 belong to July 8,
  // even though UTC has already rolled to July 9. Using UTC causes the tracker
  // to look up the wrong schedule date for late-night games.
  const centralOffset = -5; // CDT = UTC-5 (CST = UTC-6; we use -5 for CDT)
  const centralNow = new Date(now.getTime() + centralOffset * 60 * 60 * 1000);
  const todayDate = centralNow.toISOString().slice(0, 10);
  const yestDate  = new Date(centralNow.getTime() - 24 * 60 * 60 * 1000).toISOString().slice(0, 10);

  // Fetch both today and yesterday in parallel
  // Yesterday is still useful for games that started before midnight local time
  // and ran long (or for very early UTC games).
  const [todayGames, yestGames] = await Promise.all([
    fetchGamesForDate(todayDate),
    fetchGamesForDate(yestDate),
  ]);

  const activeToday = todayGames.filter(g =>
    g.status === 'In Progress' || g.status === 'Final' || g.status === 'Game Over'
  );
  const activeYest = yestGames.filter(g =>
    g.status === 'In Progress' || g.status === 'Final' || g.status === 'Game Over'
  );

  // Merge logic:
  // - Always include yesterday's In Progress / Final games — a game that started
  //   yesterday UTC and is still live (or just ended) must be tracked.
  // - Only exclude yesterday's game if today has the SAME matchup AND it is also
  //   In Progress or Final (i.e. a genuine double-header or makeup game that is
  //   actively being played today). A Scheduled game today does NOT block
  //   yesterday's live game.
  const activeTodayKeys = new Set(
    activeToday.map(g => [g.awayTeam, g.homeTeam].sort().join('|'))
  );

  const merged = [
    ...todayGames,
    ...activeYest.filter(g => {
      const key = [g.awayTeam, g.homeTeam].sort().join('|');
      // Block yesterday only if today's ACTIVE (not just Scheduled) game has same teams
      return !activeTodayKeys.has(key);
    }),
  ];

  // De-duplicate by gamePk
  const seen = new Set<number>();
  const games = merged.filter(g => {
    if (seen.has(g.gamePk)) return false;
    seen.add(g.gamePk);
    return true;
  });

  console.log(`[mlbTracker] Games pool: ${games.length} (activeYest=${activeYest.length} activeToday=${activeToday.length})`);

  return games;
}

// ── Fetch box score and resolve a single player's stat ───────────────────────
// Returns a full PlayerGameStat (including dnp) or null if player not found.
async function getPlayerStatFromGame(
  gamePk: number,
  gameStatus: string,    // 'In Progress' | 'Final' | 'Game Over'
  playerName: string,
  statType: string
): Promise<PlayerGameStat | null> {
  const resp = await fetch(`${MLB_API}/game/${gamePk}/boxscore`);
  if (!resp.ok) return null;
  const json = await resp.json();

  for (const side of ['away', 'home']) {
    const team = json.teams?.[side] ?? {};
    const allPlayers = team.players ?? {};

    for (const pid of Object.keys(allPlayers)) {
      const p = allPlayers[pid];
      const fullName: string = p?.person?.fullName ?? '';
      if (!nameMatch(fullName, playerName)) continue;

      const batting = p?.stats?.batting ?? {};
      const pitching = p?.stats?.pitching ?? {};
      const hasActivity = (batting.atBats ?? 0) > 0 || (batting.plateAppearances ?? 0) > 0
        || (pitching.outs ?? 0) > 0 || (pitching.battersFaced ?? 0) > 0;

      const val = resolveStatValue(statType, batting, pitching);
      if (val !== null) {
        // Only treat as final when the MLB API explicitly says the game is over.
        // Anything else (Warmup, Pre-Game, Delayed, Suspended, etc.) stays live
        // so we never prematurely settle a leg mid-game.
        const FINAL_STATES = new Set(['Final', 'Game Over', 'Completed Early']);
        const resolvedStatus: 'live' | 'final' = FINAL_STATES.has(gameStatus) ? 'final' : 'live';
        return { playerName, statType, actualValue: val, gameStatus: resolvedStatus, gamePk };
      }
    }
  }
  return null;
}

// ── Main: find a player's stat across today's games ──────────────────────────
export async function getPlayerStat(
  playerName: string,
  statType: string,
  gameMatchup?: string   // "Brewers vs Cardinals" or "MIL @ STL" hint to narrow which game
): Promise<PlayerGameStat | null> {
  try {
    const games = await getTodayGames();

    // Parse the matchup hint into tokens for flexible matching
    const matchupTokens = gameMatchup ? parseMatchupTokens(gameMatchup) : [];

    console.log(`[mlbTracker] getPlayerStat: player="${playerName}" stat="${statType}" matchup="${gameMatchup || 'none'}" tokens=${JSON.stringify(matchupTokens)}`);
    console.log(`[mlbTracker] Today's games: ${games.map(g => `${g.awayTeam} @ ${g.homeTeam} [${g.status}]`).join(', ')}`);

    // Narrow to active/final games only
    let candidates = games.filter(g =>
      g.status === 'In Progress' || g.status === 'Final' || g.status === 'Game Over'
    );

    console.log(`[mlbTracker] Active/final candidates: ${candidates.length}`);

    if (matchupTokens.length >= 2) {
      // Try to find the specific game using parsed tokens
      const matchingGame = candidates.find(g =>
        gameMatchesMatchup(g.awayTeam, g.homeTeam, matchupTokens)
      );
      if (matchingGame) {
        console.log(`[mlbTracker] Matched game: ${matchingGame.awayTeam} @ ${matchingGame.homeTeam} (pk=${matchingGame.gamePk})`);
        candidates = [matchingGame];
      } else {
        // Fall back to single-token partial match (one team found)
        const partialMatch = candidates.find(g => {
          const away = g.awayTeam.toUpperCase();
          const home = g.homeTeam.toUpperCase();
          return matchupTokens.some(t => away.includes(t) || home.includes(t));
        });
        if (partialMatch) {
          console.log(`[mlbTracker] Partial match: ${partialMatch.awayTeam} @ ${partialMatch.homeTeam}`);
          candidates = [partialMatch];
        } else {
          console.log(`[mlbTracker] No game match found for tokens — searching all active games`);
        }
      }
    }

    for (const game of candidates) {
      console.log(`[mlbTracker] Checking boxscore for gamePk=${game.gamePk} (${game.awayTeam} @ ${game.homeTeam})`);
      const result = await getPlayerStatFromGame(game.gamePk, game.status, playerName, statType);
      if (result !== null) {
        console.log(`[mlbTracker] ✅ Found ${playerName} ${statType}=${result.actualValue} gameStatus=${result.gameStatus} in gamePk=${game.gamePk}`);
        return result;
      } else {
        console.log(`[mlbTracker] ❌ Player ${playerName} not found in gamePk=${game.gamePk}`);
      }
    }

    // Check if the player's specific game is still scheduled (not started yet)
    // Bug fix: only return null for THIS player's game, not any random scheduled game.
    // We check using matchup tokens first; if no match found, fall back to "game not started yet".
    if (matchupTokens.length >= 2) {
      const playerGame = games.find(g =>
        gameMatchesMatchup(g.awayTeam, g.homeTeam, matchupTokens)
      );
      if (playerGame) {
        const isScheduled = playerGame.status === 'Scheduled'
          || playerGame.status === 'Pre-Game'
          || playerGame.status === 'Warmup';
        if (isScheduled) {
          console.log(`[mlbTracker] Game for ${playerName} is ${playerGame.status} — not started yet`);
          return null;
        }
      }
    } else {
      // No matchup hint — can't determine player's game specifically
      // Don't block on other games being scheduled
    }

    console.log(`[mlbTracker] No stat found for ${playerName} ${statType} (searched ${candidates.length} games)`);
    return null;
  } catch (e: any) {
    console.warn(`[mlbTracker] Failed to get stat for ${playerName} ${statType}: ${e.message}`);
    return null;
  }
}
