/**
 * MoneyLineProvider
 * Pulls player props from https://mlapi.bet/v1/player-props
 * Normalizes to RawCanonicalProp — identical shape to PrizePicks output.
 *
 * Source priority: PrizePicks lines preferred (bookmakerId === 'prizepicks').
 * Falls back to DraftKings → FanDuel → first available if PP not in feed.
 *
 * Market key → GOTit stat type mapping for MLB, NBA, NFL:
 *   MLB: batter_hits, batter_home_runs, batter_rbis, batter_total_bases,
 *        batter_stolen_bases, batter_walks, batter_runs_scored,
 *        pitcher_strikeouts, pitcher_earned_runs, pitcher_outs, pitcher_walks
 *   NBA: player_points, player_rebounds, player_assists, player_threes,
 *        player_blocks, player_steals, player_turnovers
 *   NFL: player_pass_yds, player_rush_yds, player_rec_yds, player_receptions,
 *        player_pass_tds, player_rush_tds, player_rec_tds
 */

import type { RawCanonicalProp } from './canonical';

const BASE_URL = 'https://mlapi.bet/v1';

// Preferred bookmaker order for line selection
const BOOKIE_PRIORITY = ['prizepicks', 'draftkings', 'fanduel', 'betmgm', 'caesars', 'bovada'];

// ML league key → GOTit league name
const LEAGUE_MAP: Record<string, string> = {
  mlb: 'MLB',
  nba: 'NBA',
  nfl: 'NFL',
  nhl: 'NHL',
  mma: 'MMA',
};

// ML market key → human-readable stat type for GOTit
const MARKET_TO_STAT: Record<string, string> = {
  // MLB hitter
  batter_hits: 'Hits',
  batter_home_runs: 'Home Runs',
  batter_rbis: 'RBIs',
  batter_total_bases: 'Total Bases',
  batter_stolen_bases: 'Stolen Bases',
  batter_walks: 'Walks',
  batter_runs_scored: 'Runs',
  batter_doubles: 'Doubles',
  batter_triples: 'Triples',
  batter_strikeouts: 'Batter Strikeouts',
  // MLB pitcher
  pitcher_strikeouts: 'Pitcher Strikeouts',
  pitcher_earned_runs: 'Earned Runs Allowed',
  pitcher_outs: 'Outs Recorded',
  pitcher_walks: 'Pitcher Walks',
  pitcher_hits_allowed: 'Hits Allowed',
  // NBA
  player_points: 'Points',
  player_rebounds: 'Rebounds',
  player_assists: 'Assists',
  player_threes: '3-Pointers Made',
  player_blocks: 'Blocks',
  player_steals: 'Steals',
  player_turnovers: 'Turnovers',
  player_pts_rebs_asts: 'Pts+Rebs+Asts',
  player_pts_rebs: 'Pts+Rebs',
  player_pts_asts: 'Pts+Asts',
  player_rebs_asts: 'Rebs+Asts',
  // NFL
  player_pass_yds: 'Passing Yards',
  player_rush_yds: 'Rushing Yards',
  player_rec_yds: 'Receiving Yards',
  player_receptions: 'Receptions',
  player_pass_tds: 'Passing TDs',
  player_rush_tds: 'Rushing TDs',
  player_rec_tds: 'Receiving TDs',
  player_pass_attempts: 'Pass Attempts',
  player_pass_completions: 'Pass Completions',
};

// Markets we want to include — skip alternate lines & niche markets
const ALLOWED_MARKETS = new Set(Object.keys(MARKET_TO_STAT));

interface MLOffer {
  bookmakerId: string;
  bookmakerName: string;
  sourceType: string;
  selection: string;
  price: number;
  impliedProbability: number;
  isBest: boolean;
  lastUpdate: string;
}

interface MLLine {
  point: number;
  offers: MLOffer[];
}

interface MLMarket {
  marketType: string;
  marketName: string;
  format: string;
  isAlternate: boolean;
  lines: MLLine[];
}

interface MLPlayer {
  playerName: string;
  playerId: string;
  markets: MLMarket[];
}

interface MLEvent {
  eventId: string;
  canonicalEventId?: string;
  leagueId: string;
  sport: string;
  homeTeamName: string;
  awayTeamName: string;
  startTime: string;
  fetchedAt: string;
  players: MLPlayer[];
}

interface MLResponse {
  success: boolean;
  data: MLEvent[];
}

/**
 * Pick the best line from a market:
 * 1. Prefer PP line if present
 * 2. Fall back through BOOKIE_PRIORITY
 * 3. Returns { line, bookmakerId } or null if no usable line
 */
function pickBestLine(market: MLMarket): { point: number; bookmakerId: string; selection: string } | null {
  for (const bookie of BOOKIE_PRIORITY) {
    for (const lineObj of market.lines) {
      const offer = lineObj.offers.find(o =>
        o.bookmakerId === bookie &&
        (o.selection.toLowerCase() === 'over' || o.selection.toLowerCase() === 'more')
      );
      if (offer && lineObj.point != null) {
        return { point: lineObj.point, bookmakerId: bookie, selection: 'over' };
      }
    }
  }
  // Any offer with an over
  for (const lineObj of market.lines) {
    const offer = lineObj.offers.find(o =>
      o.selection.toLowerCase() === 'over' || o.selection.toLowerCase() === 'more'
    );
    if (offer && lineObj.point != null) {
      return { point: lineObj.point, bookmakerId: lineObj.offers[0]?.bookmakerId ?? 'unknown', selection: 'over' };
    }
  }
  return null;
}

export async function pullMoneyLine(league: string): Promise<RawCanonicalProp[]> {
  const apiKey = process.env.ML_API_KEY;
  if (!apiKey) throw new Error('ML_API_KEY not set');

  const leagueKey = league.toLowerCase();
  const leagueName = LEAGUE_MAP[leagueKey] ?? league.toUpperCase();

  const url = `${BASE_URL}/player-props?league=${leagueKey}`;
  console.log(`[MoneyLine] Fetching: ${url}`);

  const resp = await fetch(url, {
    headers: {
      'x-api-key': apiKey,
      'Accept': 'application/json',
    },
  });

  if (!resp.ok) {
    const body = await resp.text();
    throw new Error(`MoneyLine HTTP ${resp.status} on ${leagueKey}: ${body.slice(0, 200)}`);
  }

  const json: MLResponse = await resp.json();
  if (!json.success || !Array.isArray(json.data)) {
    throw new Error(`MoneyLine bad response shape for ${leagueKey}`);
  }

  const now = new Date().toISOString();
  const props: RawCanonicalProp[] = [];

  for (const event of json.data) {
    const gameId = event.canonicalEventId ?? event.eventId;
    const startTime = event.startTime ? new Date(event.startTime).toISOString() : null;
    const awayAbbr = event.awayTeamName?.split(' ').pop() ?? 'AWAY';
    const homeAbbr = event.homeTeamName?.split(' ').pop() ?? 'HOME';
    const gameMatchup = `${awayAbbr} @ ${homeAbbr}`;

    for (const player of (event.players ?? [])) {
      for (const market of (player.markets ?? [])) {
        // Skip alternate lines and unknown markets
        if (market.isAlternate) continue;
        if (!ALLOWED_MARKETS.has(market.marketType)) continue;

        const best = pickBestLine(market);
        if (!best) continue;

        const statType = MARKET_TO_STAT[market.marketType] ?? market.marketName;
        const propId = `${event.eventId}:${player.playerId}:${market.marketType}`;
        const fromPP = best.bookmakerId === 'prizepicks';

        props.push({
          id: `moneyline:${propId}`,
          sourcePropId: propId,
          source: 'sportsgameodds', // reuse existing fallback source type — keeps rest of app unchanged
          isFallback: true,

          league: leagueName,
          gameId,
          gameMatchup,
          gameStartTime: startTime,

          playerName: player.playerName,
          teamAbbr: '', // ML doesn't always include team abbr at player level

          statType,
          lineScore: best.point,
          direction: 'over',

          isDemon: false,
          isGoblin: false,
          tier: 'standard',

          // Display truth — use PP values if sourced from PP, else null
          ppDisplayMatchup: fromPP ? gameMatchup : null,
          ppDisplayPlayer: fromPP ? player.playerName : null,
          ppDisplayStat: fromPP ? statType : null,
          ppDisplayTeam: fromPP ? '' : null,
          ppEventTitle: null,

          pulledAt: now,
        });
      }
    }
  }

  console.log(`[MoneyLine] ${leagueName}: ${props.length} props from ${json.data.length} events`);
  return props;
}
