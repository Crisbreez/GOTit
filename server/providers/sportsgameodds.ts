/**
 * SportsGameOddsProvider
 * Pulls player props from SGO API and outputs RawCanonicalProp[]
 * Gracefully skips if SGO_API_KEY env var is not set.
 *
 * Endpoint: GET https://api.sportsgameodds.com/v2/events
 * Auth: x-api-key header
 * Key params: leagueID, oddsAvailable=true, bookmakerID=prizepicks, includeOpposingOdds=true
 * oddID format: {statID}-{playerID}-game-ou-over
 * e.g. batting_hits-SHOHEI_OHTANI_1_MLB-game-ou-over
 */

import type { RawCanonicalProp, PropSide } from './canonical';

const BASE = 'https://api.sportsgameodds.com/v2';

// Map SGO statID prefixes → readable stat type names
const STAT_NAME_MAP: Record<string, string> = {
  batting_hits: 'Hits',
  batting_home_runs: 'Home Runs',
  batting_rbis: 'RBIs',
  batting_runs: 'Runs Scored',
  batting_strikeouts: 'Strikeouts (Batter)',
  batting_walks: 'Walks',
  batting_total_bases: 'Total Bases',
  batting_singles: 'Singles',
  batting_doubles: 'Doubles',
  pitching_strikeouts: 'Strikeouts (Pitcher)',
  pitching_earned_runs: 'Earned Runs',
  pitching_hits_allowed: 'Hits Allowed',
  pitching_walks: 'Walks Allowed',
  pitching_outs: 'Outs Recorded',
  points: 'Points',
  assists: 'Assists',
  rebounds: 'Rebounds',
  three_pointers_made: '3-Pointers Made',
  steals: 'Steals',
  blocks: 'Blocks',
  turnovers: 'Turnovers',
  passing_yards: 'Passing Yards',
  rushing_yards: 'Rushing Yards',
  receiving_yards: 'Receiving Yards',
  receptions: 'Receptions',
  passing_touchdowns: 'Passing TDs',
  rushing_touchdowns: 'Rushing TDs',
  receiving_touchdowns: 'Receiving TDs',
  // MMA-specific stats
  significant_strikes: 'Significant Strikes',
  total_strikes: 'Total Strikes',
  takedowns: 'Takedowns',
  takedown_attempts: 'Takedown Attempts',
  submission_attempts: 'Submission Attempts',
  knockdowns: 'Knockdowns',
  control_time: 'Control Time (min)',
  fighter_fantasy_score: 'Fighter Fantasy Score',
  sig_strikes_landed: 'Sig. Strikes Landed',
};

function resolveStatName(statId: string): string {
  // Try full match first, then prefix match
  if (STAT_NAME_MAP[statId]) return STAT_NAME_MAP[statId];
  for (const [key, val] of Object.entries(STAT_NAME_MAP)) {
    if (statId.startsWith(key)) return val;
  }
  // Fallback: humanize the statId
  return statId
    .replace(/_/g, ' ')
    .replace(/\b\w/g, c => c.toUpperCase());
}

/** Parse player name from SGO playerID format: SHOHEI_OHTANI_1_MLB → "Shohei Ohtani" */
function resolvePlayerName(playerId: string, players: Record<string, any>): string {
  if (players?.[playerId]?.name) return players[playerId].name;
  if (players?.[playerId]?.displayName) return players[playerId].displayName;

  // Derive from ID as last resort: strip trailing _N_LEAGUE, replace _ with space, title case
  const cleaned = playerId
    .replace(/_\d+_[A-Z]+$/, '') // remove _1_MLB suffix
    .replace(/_/g, ' ')
    .toLowerCase()
    .replace(/\b\w/g, c => c.toUpperCase());
  return cleaned;
}

function resolveTeamAbbr(playerId: string, players: Record<string, any>): string {
  if (players?.[playerId]?.team) return players[playerId].team;
  if (players?.[playerId]?.teamAbbreviation) return players[playerId].teamAbbreviation;
  return '';
}

interface SGOEvent {
  eventID: string;
  league?: string;
  startTime?: string;
  teams?: { away?: { abbreviation?: string }; home?: { abbreviation?: string } };
  players?: Record<string, any>;
  odds?: Record<string, any>;
}

function normalizeEvent(
  event: SGOEvent,
  leagueName: string
): RawCanonicalProp[] {
  const now = new Date().toISOString();
  const odds = event.odds ?? {};
  const players = event.players ?? {};

  const awayAbbr = event.teams?.away?.abbreviation ?? '';
  const homeAbbr = event.teams?.home?.abbreviation ?? '';
  // Prefer full team name from SGO response (name > displayName > abbreviation)
  const awayFull = (event as any).teams?.away?.name ?? (event as any).teams?.away?.displayName ?? awayAbbr;
  const homeFull = (event as any).teams?.home?.name ?? (event as any).teams?.home?.displayName ?? homeAbbr;

  let gameMatchup: string;
  if (leagueName === 'MMA') {
    gameMatchup = awayFull && homeFull ? `${awayFull} vs ${homeFull}` : 'MMA Fight';
  } else {
    // Use "Away vs Home" with full names so matchup filter (LIKE '% vs %') always matches
    gameMatchup = awayFull && homeFull ? `${awayFull} vs ${homeFull}` : `${leagueName} Game`;
  }

  const gameId = event.eventID ?? `sgo-${leagueName}-${now.slice(0, 10)}`;
  const gameStartTime = event.startTime ?? null;

  const results: RawCanonicalProp[] = [];

  for (const [oddID, oddData] of Object.entries(odds)) {
    // oddID: {statID}-{playerID}-game-ou-{side}
    // We want "over" lines only (under comes paired)
    if (!oddID.endsWith('-game-ou-over')) continue;

    // Parse statID and playerID from oddID
    // Format: <statID>-<playerID>-game-ou-over
    // statID can contain hyphens (e.g. three_pointers_made is underscored, but some may have hyphens)
    // playerID is the second-to-last segment before -game-ou-over
    const withoutSuffix = oddID.replace(/-game-ou-over$/, '');
    // Split on hyphen — statID uses underscores, playerID may contain underscores too
    // Convention: statID is everything before the first hyphen; playerID is the rest
    const hyphenIdx = withoutSuffix.indexOf('-');
    if (hyphenIdx === -1) continue;

    const statId = withoutSuffix.slice(0, hyphenIdx);
    const playerId = withoutSuffix.slice(hyphenIdx + 1);

    if (!statId || !playerId) continue;

    // Get line from PP bookmaker
    const ppOdds = oddData?.byBookmaker?.prizepicks;
    if (!ppOdds) continue;

    const line = ppOdds.line ?? ppOdds.overLine ?? ppOdds.value;
    if (line == null) continue;

    const lineScore = parseFloat(String(line));
    if (isNaN(lineScore)) continue;

    const playerName = resolvePlayerName(playerId, players);
    const teamAbbr = resolveTeamAbbr(playerId, players);
    const statType = resolveStatName(statId);

    // SGO doesn't natively mark demons/goblins — default to standard
    // (GOTit's scorer will elevate based on scoring logic)
    const sourcePropId = `${event.eventID}:${oddID}`;

    const prop: RawCanonicalProp = {
      id: `sportsgameodds:${sourcePropId}`,
      sourcePropId,
      source: 'sportsgameodds',
      isFallback: true,
      league: leagueName,
      gameId,
      gameMatchup,
      gameStartTime,
      playerName,
      teamAbbr,
      statType,
      lineScore,
      direction: 'over' as PropSide,
      isDemon: false,
      isGoblin: false,
      tier: 'standard',
      pulledAt: now,
    };

    results.push(prop);
  }

  return results;
}

// Leagues where SGO returns meaningful PrizePicks lines
// MMA is excluded — SGO's MMA leagueID returns NFL data or nothing useful
const SGO_SUPPORTED_LEAGUES = new Set(['MLB', 'NBA', 'NFL']);

export async function pullSGO(league: string): Promise<RawCanonicalProp[]> {
  // Skip unsupported leagues — they return wrong data or nothing
  if (!SGO_SUPPORTED_LEAGUES.has(league)) {
    throw new Error(`SGO does not support ${league} — skipping`);
  }

  const apiKey = process.env.SGO_API_KEY;
  if (!apiKey) {
    throw new Error('SGO_API_KEY not set — skipping SGO provider');
  }

  const params = new URLSearchParams({
    leagueID: league,
    oddsAvailable: 'true',
    bookmakerID: 'prizepicks',
    includeOpposingOdds: 'true',
    limit: '100',
  });

  const url = `${BASE}/events?${params}`;

  let resp: Response;
  try {
    resp = await fetch(url, {
      headers: {
        'x-api-key': apiKey,
        Accept: 'application/json',
        'User-Agent': 'GOTit/1.0',
      },
    });
  } catch (e: any) {
    throw new Error(`SGO network error for ${league}: ${e.message}`);
  }

  if (resp.status === 401 || resp.status === 403) {
    throw new Error(`SGO auth error ${resp.status} — check SGO_API_KEY`);
  }
  if (resp.status === 429) {
    throw new Error(`SGO rate limited on ${league}`);
  }
  if (!resp.ok) {
    throw new Error(`SGO HTTP ${resp.status} on ${league}`);
  }

  const json = await resp.json();
  if (!json.success) {
    throw new Error(`SGO returned success=false for ${league}: ${JSON.stringify(json.error ?? '')}`);
  }

  const events: SGOEvent[] = json.data ?? [];
  const results: RawCanonicalProp[] = [];

  for (const event of events) {
    const props = normalizeEvent(event, league);
    results.push(...props);
  }

  return results;
}
