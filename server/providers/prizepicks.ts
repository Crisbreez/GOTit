/**
 * PrizePicksProvider
 * Wraps the existing PP puller and outputs RawCanonicalProp[]
 */

import type { RawCanonicalProp } from './canonical';
import { HttpsProxyAgent } from 'https-proxy-agent';

const LEAGUE_IDS: Record<string, number> = { MLB: 2, NBA: 7, NFL: 1, MMA: 12 }; // MMA = UFC on PP
// partner-api bypasses Cloudflare/DataDome that blocks api.prizepicks.com from datacenter IPs
const BASE = 'https://partner-api.prizepicks.com';
const sleep = (ms: number) => new Promise(r => setTimeout(r, ms));

// Rotate stable device IDs to appear as returning app users
const DEVICE_IDS = [
  'a1b2c3d4-e5f6-7890-abcd-ef1234567890',
  'b2c3d4e5-f6a7-8901-bcde-f01234567891',
  'c3d4e5f6-a7b8-9012-cdef-012345678902',
  'd4e5f6a7-b8c9-0123-defa-123456789013',
];
let deviceIndex = 0;
function nextDeviceId(): string {
  const id = DEVICE_IDS[deviceIndex % DEVICE_IDS.length];
  deviceIndex++;
  return id;
}

function normalizeToCanonical(
  proj: any,
  includedMap: Map<string, any>,
  leagueName: string
): RawCanonicalProp | null {
  const attrs = proj.attributes ?? {};
  const rels = proj.relationships ?? {};

  const playerData = includedMap.get(`new_player:${rels.new_player?.data?.id}`) ?? {};
  const playerAttrs = playerData.attributes ?? {};
  const statData = includedMap.get(`stat_type:${rels.stat_type?.data?.id}`) ?? {};

  const line = attrs.line_score;
  if (line == null) return null;

  const oddsType = (attrs.odds_type ?? '').toLowerCase();
  const isDemon = oddsType === 'demon';
  const isGoblin = oddsType === 'goblin';
  const tier = isDemon ? 'demon' : isGoblin ? 'goblin' : 'standard';

  const now = new Date().toISOString();
  // Normalize to UTC ISO string — PP returns times with timezone offsets like
  // "2026-07-07T18:40:00.000-04:00" which fail SQLite text comparison against
  // our UTC date filter. Parsing through Date() converts to Z-suffixed UTC.
  const rawStartTime = attrs.start_time ?? null;
  const startTime = rawStartTime ? new Date(rawStartTime).toISOString() : null;
  const gameId = attrs.game_id ?? `${leagueName}-${startTime?.slice(0, 10) ?? 'tbd'}`;

  // ── Resolve display truth from PP exactly as PP provides it ────────────────
  const gameData = includedMap.get(`game:${rels.game?.data?.id}`) ?? {};
  const gameAttrs = gameData.attributes ?? {};
  const gameMeta = gameAttrs?.metadata?.game_info ?? {};
  const teams = gameMeta.teams ?? {};

  const awayAbbr = teams.away?.abbreviation ?? '';
  const homeAbbr = teams.home?.abbreviation ?? '';
  const awayName = teams.away?.name ?? teams.away?.full_name ?? awayAbbr;
  const homeName = teams.home?.name ?? teams.home?.full_name ?? homeAbbr;

  // PP-exact player fields
  const ppDisplayPlayer = playerAttrs.display_name ?? playerAttrs.name ?? 'Unknown';
  const ppDisplayTeam = playerAttrs.team ?? '';
  const ppDisplayStat = statData.attributes?.name ?? attrs.stat_type ?? '';

  // PP-exact matchup — prefer full team/fighter names, never produce "X Game" placeholders
  const ppGameTitle = gameAttrs.description ?? gameAttrs.name ?? gameAttrs.title ?? '';
  let ppDisplayMatchup: string | null = null;

  if (awayAbbr && homeAbbr) {
    // Best case: PP gives us both sides — use full names when available
    const awayLabel = (awayName && awayName !== awayAbbr) ? awayName : awayAbbr;
    const homeLabel = (homeName && homeName !== homeAbbr) ? homeName : homeAbbr;
    ppDisplayMatchup = `${awayLabel} vs ${homeLabel}`;
  } else if (leagueName === 'MMA') {
    // MMA: fighter names come from game metadata, not team abbreviations
    const metaAway = gameMeta.away_team ?? gameMeta.away?.name ?? '';
    const metaHome = gameMeta.home_team ?? gameMeta.home?.name ?? '';
    if (metaAway && metaHome) {
      ppDisplayMatchup = `${metaAway} vs ${metaHome}`;
    } else if (ppGameTitle && ppGameTitle.toLowerCase().includes('vs')) {
      ppDisplayMatchup = ppGameTitle;
    }
    // else: leave null — routes.ts will collect fighter names from props in this game group
  } else if (ppGameTitle && ppGameTitle.toLowerCase().includes('vs')) {
    ppDisplayMatchup = ppGameTitle;
  }
  // If we still have only one abbr, leave null — routes.ts resolves via prop team collection
  // NEVER set to "X Game" or "MLB Game" etc.

  // PP-exact event title (for MMA event name, e.g. "UFC 329")
  const ppEventTitle = ppGameTitle || null;

  // Internal gameMatchup: use ppDisplayMatchup if resolved, else fall back to abbrs
  // routes.ts will do a second-pass resolution for any remaining nulls
  const gameMatchup = ppDisplayMatchup
    ?? (awayAbbr && homeAbbr ? `${awayAbbr} vs ${homeAbbr}` : (awayAbbr || homeAbbr || ''));

  const sourcePropId = proj.id;
  // PP internal player ID — preserved so playerRecents can skip the name-lookup step
  const ppPlayerId = rels.new_player?.data?.id ?? null;

  return {
    id: `prizepicks:${sourcePropId}`,
    sourcePropId,
    source: 'prizepicks',
    isFallback: false,
    league: leagueName,
    gameId,
    gameMatchup,
    gameStartTime: startTime,
    playerName: ppDisplayPlayer,
    teamAbbr: ppDisplayTeam,
    statType: ppDisplayStat,
    lineScore: parseFloat(line),
    direction: (attrs.over_under ?? 'over').toLowerCase() === 'under' ? 'under' : 'over',
    isDemon,
    isGoblin,
    tier,
    // Display truth columns — preserved verbatim, never mutated by GOTit internals
    ppDisplayMatchup,
    ppDisplayPlayer,
    ppDisplayStat,
    ppDisplayTeam,
    ppEventTitle,
    ppPlayerId,    // PP internal player ID (string) — used by playerRecents name-lookup cache
    pulledAt: now,
  };
}

// Rotate through realistic User-Agent strings to reduce fingerprinting
const USER_AGENTS = [
  'Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1',
  'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36',
  'Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1',
];
let uaIndex = 0;
function nextUA(): string {
  const ua = USER_AGENTS[uaIndex % USER_AGENTS.length];
  uaIndex++;
  return ua;
}

export async function pullPrizePicks(league: string): Promise<RawCanonicalProp[]> {
  const id = LEAGUE_IDS[league];
  if (!id) throw new Error(`Unknown league: ${league}`);

  // Use the partner-api endpoint — same JSON:API format as api.prizepicks.com but
  // accessible from datacenter IPs without Cloudflare/DataDome blocks
  // Returns all props including demon/goblin tiers in a single call
  const url = `${BASE}/projections?league_id=${id}&per_page=2000`;

  // Use residential proxy if configured — bypasses Cloudflare IP blocks
  const proxyUrl = process.env.PP_PROXY_URL ?? null;
  const agent = proxyUrl ? new HttpsProxyAgent(proxyUrl) : undefined;
  if (proxyUrl) {
    console.log(`[PrizePicks] Using proxy for ${league} pull`);
  }

  let resp: Response;
  try {
    resp = await fetch(url, {
      // @ts-ignore — node fetch accepts dispatcher/agent
      ...(agent ? { agent } : {}),
      headers: {
        'User-Agent': nextUA(),
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate, br',
        'Origin': 'https://app.prizepicks.com',
        'Referer': 'https://app.prizepicks.com/',
        'x-device-id': nextDeviceId(),
        'Cache-Control': 'no-cache',
        'Pragma': 'no-cache',
        'Sec-Fetch-Dest': 'empty',
        'Sec-Fetch-Mode': 'cors',
        'Sec-Fetch-Site': 'same-site',
      },
    });
  } catch (e: any) {
    throw new Error(`Network error pulling PP ${league}: ${e.message}`);
  }

  if (resp.status === 403 || resp.status === 429) {
    const code = resp.status;
    throw new Error(`PP ${code} — rate limited / IP block on ${league}`);
  }
  if (!resp.ok) throw new Error(`PP HTTP ${resp.status} on ${league}`);

  // Verify we got JSON not an HTML captcha page
  const contentType = resp.headers.get('content-type') ?? '';
  if (!contentType.includes('application/json')) {
    // Cloudflare 1015 returns 200 OK with HTML — treat as IP block
    const bodySnippet = await resp.text().then(t => t.slice(0, 200)).catch(() => '');
    const is1015 = bodySnippet.includes('1015') || bodySnippet.includes('error code') || bodySnippet.includes('Cloudflare');
    throw new Error(is1015
      ? `PP IP block (error code 1015) — Cloudflare blocking datacenter IP on ${league}`
      : `PP returned non-JSON (${contentType}) — likely captcha page`
    );
  }

  const json = await resp.json();
  const data: any[] = json.data ?? [];
  const included: any[] = json.included ?? [];

  if (data.length === 0) {
    // Could be legitimately empty (off-season) or a soft block
    console.warn(`[PrizePicks] ${league}: 0 projections in response`);
  }

  const includedMap = new Map<string, any>();
  for (const item of included) {
    includedMap.set(`${item.type}:${item.id}`, item);
  }

  const results: RawCanonicalProp[] = [];
  for (const proj of data) {
    if (proj.type !== 'projection') continue;
    const row = normalizeToCanonical(proj, includedMap, league);
    if (row) results.push(row);
  }

  return results;
}
