/**
 * Demo seeder — runs on first boot when props table is empty.
 * Seeds realistic MLB and MMA game data so the app works even when PP API is unavailable.
 */
import { storage } from './storage';
import { centralToday, nowISO } from './time';

const NOW = nowISO();
const TODAY = centralToday();

// ── MLB Demo games ───────────────────────────────────────────────────────────
const MLB_DEMO_GAMES = [
  {
    gameId: `demo-mlb-nyy-bos-${TODAY}`,
    matchup: 'New York Yankees vs Boston Red Sox',
    startTime: `${TODAY}T17:05:00Z`,
    scriptLabel: 'High Scoring Offense',
    league: 'MLB',
    home: 'NYY', away: 'BOS',
  },
  {
    gameId: `demo-mlb-lad-sf-${TODAY}`,
    matchup: 'LA Dodgers vs San Francisco Giants',
    startTime: `${TODAY}T20:10:00Z`,
    scriptLabel: 'Pitcher Dominance',
    league: 'MLB',
    home: 'LAD', away: 'SF',
  },
  {
    gameId: `demo-mlb-hou-tex-${TODAY}`,
    matchup: 'Houston Astros vs Texas Rangers',
    startTime: `${TODAY}T19:05:00Z`,
    scriptLabel: 'Close Game Script',
    league: 'MLB',
    home: 'HOU', away: 'TEX',
  },
  {
    gameId: `demo-mlb-chc-stl-${TODAY}`,
    matchup: 'Chicago Cubs vs St. Louis Cardinals',
    startTime: `${TODAY}T18:40:00Z`,
    scriptLabel: 'Bullpen Game',
    league: 'MLB',
    home: 'CHC', away: 'STL',
  },
  {
    gameId: `demo-mlb-atl-mia-${TODAY}`,
    matchup: 'Atlanta Braves vs Miami Marlins',
    startTime: `${TODAY}T19:20:00Z`,
    scriptLabel: 'Blowout Script',
    league: 'MLB',
    home: 'ATL', away: 'MIA',
  },
];

// ── MMA Demo fights ──────────────────────────────────────────────────────────
// Real upcoming UFC cards (July 2026)
const MMA_DEMO_FIGHTS = [
  {
    gameId: `demo-mma-mcgregor-holloway-${TODAY}`,
    matchup: 'Conor McGregor vs Max Holloway',
    startTime: '2026-07-11T22:00:00Z',
    scriptLabel: 'KO Script',
    league: 'MMA',
    fighter1: 'Conor McGregor',
    fighter2: 'Max Holloway',
    eventName: 'UFC 329',
    weightClass: 'Welterweight',
    venue: 'T-Mobile Arena, Las Vegas',
  },
  {
    gameId: `demo-mma-duplessis-usman-${TODAY}`,
    matchup: 'Dricus Du Plessis vs Kamaru Usman',
    startTime: '2026-07-18T22:00:00Z',
    scriptLabel: 'Grappling Contest',
    league: 'MMA',
    fighter1: 'Dricus Du Plessis',
    fighter2: 'Kamaru Usman',
    eventName: 'UFC Fight Night',
    weightClass: 'Middleweight',
    venue: 'Paycom Center, Oklahoma City',
  },
  {
    gameId: `demo-mma-ankalaev-rountree-${TODAY}`,
    matchup: 'Magomed Ankalaev vs Khalil Rountree Jr.',
    startTime: '2026-07-25T22:00:00Z',
    scriptLabel: 'Striking Battle',
    league: 'MMA',
    fighter1: 'Magomed Ankalaev',
    fighter2: 'Khalil Rountree Jr.',
    eventName: 'UFC Fight Night',
    weightClass: 'Light Heavyweight',
    venue: 'Etihad Arena, Abu Dhabi',
  },
];

// ── MLB Player prop templates ────────────────────────────────────────────────
const MLB_GAME_PROPS: Record<string, any[]> = {
  'demo-mlb-nyy-bos': [
    { player: 'Aaron Judge', team: 'NYY', stat: 'Hitter Fantasy Score', line: 35.5, demon: true },
    { player: 'Juan Soto', team: 'NYY', stat: 'Total Bases', line: 2.5, demon: true },
    { player: 'Anthony Volpe', team: 'NYY', stat: 'Hits', line: 0.5, goblin: true },
    { player: 'Rafael Devers', team: 'BOS', stat: 'Hitter Fantasy Score', line: 18.5, goblin: true },
    { player: 'Gerrit Cole', team: 'NYY', stat: 'Strikeouts', line: 6.5 },
    { player: 'Giancarlo Stanton', team: 'NYY', stat: 'Total Bases', line: 1.5 },
    { player: 'Masataka Yoshida', team: 'BOS', stat: 'Hits', line: 1.5 },
    { player: 'Alex Bregman', team: 'BOS', stat: 'RBIs', line: 0.5 },
  ],
  'demo-mlb-lad-sf': [
    { player: 'Shohei Ohtani', team: 'LAD', stat: 'Hitter Fantasy Score', line: 38.5, demon: true },
    { player: 'Mookie Betts', team: 'LAD', stat: 'Stolen Bases', line: 0.5, demon: true },
    { player: 'Freddie Freeman', team: 'LAD', stat: 'Hits', line: 0.5, goblin: true },
    { player: 'Matt Chapman', team: 'SF', stat: 'Hitter Fantasy Score', line: 14.5, goblin: true },
    { player: 'Yoshinobu Yamamoto', team: 'LAD', stat: 'Strikeouts', line: 7.5 },
    { player: 'Will Smith', team: 'LAD', stat: 'Total Bases', line: 1.5 },
    { player: 'Heliot Ramos', team: 'SF', stat: 'Hits', line: 1.5 },
    { player: 'Logan Webb', team: 'SF', stat: 'Pitcher Fantasy Score', line: 22.5 },
  ],
  'demo-mlb-hou-tex': [
    { player: 'Jose Altuve', team: 'HOU', stat: 'Hitter Fantasy Score', line: 22.5, demon: true },
    { player: 'Kyle Tucker', team: 'HOU', stat: 'Total Bases', line: 2.5, demon: true },
    { player: 'Yordan Alvarez', team: 'HOU', stat: 'Hits', line: 0.5, goblin: true },
    { player: 'Corey Seager', team: 'TEX', stat: 'Hitter Fantasy Score', line: 20.5, goblin: true },
    { player: 'Framber Valdez', team: 'HOU', stat: 'Strikeouts', line: 5.5 },
    { player: 'Nathaniel Lowe', team: 'TEX', stat: 'Total Bases', line: 1.5 },
    { player: 'Marcus Semien', team: 'TEX', stat: 'Hits', line: 1.5 },
    { player: 'Adolis Garcia', team: 'TEX', stat: 'RBIs', line: 0.5 },
  ],
  'demo-mlb-chc-stl': [
    { player: 'Seiya Suzuki', team: 'CHC', stat: 'Hitter Fantasy Score', line: 18.5, demon: true },
    { player: 'Nico Hoerner', team: 'CHC', stat: 'Hits', line: 1.5, demon: true },
    { player: 'Ian Happ', team: 'CHC', stat: 'Total Bases', line: 1.5, goblin: true },
    { player: 'Paul Goldschmidt', team: 'STL', stat: 'Hitter Fantasy Score', line: 17.5, goblin: true },
    { player: 'Justin Steele', team: 'CHC', stat: 'Strikeouts', line: 4.5 },
    { player: 'Dansby Swanson', team: 'CHC', stat: 'RBIs', line: 0.5 },
    { player: 'Nolan Arenado', team: 'STL', stat: 'Total Bases', line: 1.5 },
    { player: 'Lars Nootbaar', team: 'STL', stat: 'Hits', line: 1.5 },
  ],
  'demo-mlb-atl-mia': [
    { player: 'Ronald Acuna Jr.', team: 'ATL', stat: 'Hitter Fantasy Score', line: 36.5, demon: true },
    { player: 'Austin Riley', team: 'ATL', stat: 'Total Bases', line: 2.5, demon: true },
    { player: 'Matt Olson', team: 'ATL', stat: 'Hits', line: 0.5, goblin: true },
    { player: 'Jazz Chisholm Jr.', team: 'MIA', stat: 'Stolen Bases', line: 0.5, goblin: true },
    { player: 'Spencer Strider', team: 'ATL', stat: 'Strikeouts', line: 8.5 },
    { player: 'Ozzie Albies', team: 'ATL', stat: 'Hits', line: 1.5 },
    { player: 'Luis Arraez', team: 'MIA', stat: 'Hits', line: 1.5 },
    { player: 'Bryan De La Cruz', team: 'MIA', stat: 'Total Bases', line: 1.5 },
  ],
};

// ── MMA Fighter prop templates ───────────────────────────────────────────────
const MMA_FIGHT_PROPS: Record<string, any[]> = {
  'demo-mma-mcgregor-holloway': [
    // UFC 329 — KO power matchup, elite strikers in a welterweight classic
    { player: 'Conor McGregor', stat: 'Significant Strikes', line: 44.5, demon: true },
    { player: 'Max Holloway', stat: 'Significant Strikes', line: 58.5, demon: true },
    { player: 'Conor McGregor', stat: 'Knockdowns', line: 0.5, goblin: true },
    { player: 'Max Holloway', stat: 'Takedown Attempts', line: 0.5, goblin: true },
    { player: 'Conor McGregor', stat: 'Sig. Strikes Landed', line: 34.5 },
    { player: 'Max Holloway', stat: 'Sig. Strikes Landed', line: 46.5 },
    { player: 'Conor McGregor', stat: 'Total Strikes', line: 58.5 },
    { player: 'Max Holloway', stat: 'Total Strikes', line: 74.5 },
  ],
  'demo-mma-duplessis-usman': [
    // UFC Fight Night — Champion vs legend, grappling chess match at middleweight
    { player: 'Dricus Du Plessis', stat: 'Significant Strikes', line: 52.5, demon: true },
    { player: 'Kamaru Usman', stat: 'Takedowns', line: 2.5, demon: true },
    { player: 'Dricus Du Plessis', stat: 'Submission Attempts', line: 0.5, goblin: true },
    { player: 'Kamaru Usman', stat: 'Control Time (min)', line: 4.5, goblin: true },
    { player: 'Dricus Du Plessis', stat: 'Sig. Strikes Landed', line: 42.5 },
    { player: 'Kamaru Usman', stat: 'Sig. Strikes Landed', line: 36.5 },
    { player: 'Dricus Du Plessis', stat: 'Takedown Attempts', line: 1.5 },
    { player: 'Kamaru Usman', stat: 'Total Strikes', line: 58.5 },
  ],
  'demo-mma-ankalaev-rountree': [
    // UFC Fight Night — Light heavyweight war, elite wrestling vs knockout power
    { player: 'Magomed Ankalaev', stat: 'Significant Strikes', line: 46.5, demon: true },
    { player: 'Khalil Rountree Jr.', stat: 'Significant Strikes', line: 48.5, demon: true },
    { player: 'Magomed Ankalaev', stat: 'Takedowns', line: 1.5, goblin: true },
    { player: 'Khalil Rountree Jr.', stat: 'Knockdowns', line: 0.5, goblin: true },
    { player: 'Magomed Ankalaev', stat: 'Sig. Strikes Landed', line: 36.5 },
    { player: 'Khalil Rountree Jr.', stat: 'Sig. Strikes Landed', line: 38.5 },
    { player: 'Magomed Ankalaev', stat: 'Total Strikes', line: 62.5 },
    { player: 'Khalil Rountree Jr.', stat: 'Total Strikes', line: 64.5 },
  ],
};

// ── Score lookup ─────────────────────────────────────────────────────────────
function getPropScore(isDemon: boolean, isGoblin: boolean, stat: string): number {
  if (isDemon) return 0.62 + Math.random() * 0.18;
  if (isGoblin) return 0.52 + Math.random() * 0.18;
  const s = stat.toLowerCase();
  if (s.includes('fantasy') || s.includes('score')) return 0.42 + Math.random() * 0.15;
  if (s.includes('strikeout') || s.includes('pitcher')) return 0.28 + Math.random() * 0.15;
  if (s.includes('significant') || s.includes('strikes')) return 0.38 + Math.random() * 0.15;
  return 0.32 + Math.random() * 0.18;
}

function getConfidence(isDemon: boolean, isGoblin: boolean, score: number): number {
  if (isDemon) return 5;
  if (isGoblin) return 4;
  if (score >= 0.55) return 4;
  if (score >= 0.42) return 3;
  return 2;
}

// ── MLB seed ─────────────────────────────────────────────────────────────────
async function seedMLB(): Promise<void> {
  const existing = await storage.getProps('MLB');
  if (existing.length > 0) {
    console.log(`[DemoSeed] MLB already has ${existing.length} props — skipping MLB seed`);
    return;
  }

  console.log('[DemoSeed] Seeding demo MLB props...');

  const rows: any[] = [];
  for (const game of MLB_DEMO_GAMES) {
    const templateKey = Object.keys(MLB_GAME_PROPS).find(k => game.gameId.startsWith(`${k}-`)) || Object.keys(MLB_GAME_PROPS)[0];
    const propTemplates = MLB_GAME_PROPS[templateKey] || MLB_GAME_PROPS['demo-mlb-nyy-bos'];

    propTemplates.forEach((p, idx) => {
      const isDemon = !!p.demon;
      const isGoblin = !!p.goblin;
      const score = getPropScore(isDemon, isGoblin, p.stat);
      const conf = getConfidence(isDemon, isGoblin, score);
      const direction = p.line < 1 ? 'over' : 'over';

      rows.push({
        id: `${game.gameId}-${p.player.replace(/\s+/g, '-').toLowerCase()}-${idx}`,
        league: 'MLB',
        playerName: p.player,
        teamAbbr: p.team,
        statType: p.stat,
        lineScore: p.line,
        direction,
        isDemon,
        isGoblin,
        gameId: game.gameId,
        gameMatchup: game.matchup,
        gameStartTime: game.startTime,
        confidenceLevel: conf,
        propScore: Math.round(score * 1000) / 1000,
        rejectReason: null,
        scriptLabel: game.scriptLabel,
        pulledAt: NOW,
      });
    });
  }

  await storage.upsertProps(rows);
  await storage.logPull('MLB', rows.length);
  console.log(`[DemoSeed] Seeded ${rows.length} MLB props across ${MLB_DEMO_GAMES.length} games`);
}

// MMA stat types that are valid — used to detect contamination from NFL data
const MMA_VALID_STATS = [
  'Significant Strikes', 'Sig. Strikes Landed', 'Total Strikes',
  'Takedowns', 'Takedown Attempts', 'Submission Attempts',
  'Knockdowns', 'Control Time', 'Fighter Fantasy Score',
];

// All valid MMA stat keywords — anything outside this is contamination
const MMA_STAT_KEYWORDS = [
  'strike', 'takedown', 'submission', 'knockdown', 'control', 'fighter', 'grapple',
  'sig.', 'significant', 'total strikes', 'ground', 'clinch',
];

function isMMAContaminated(existingProps: any[]): boolean {
  if (existingProps.length === 0) return false;
  // Check if any prop has a stat type that doesn't match MMA
  const hasNonMMAStats = existingProps.some(p => {
    const stat = (p.statType || '').toLowerCase();
    // NFL/football red flags (both long and short form)
    const nflTerms = ['pass yard', 'rush yard', 'receiv', 'receptions', 'sack', ' td', 'touchdown', 'intercept', 'completion', 'carry', 'rushing td', 'passing td'];
    if (nflTerms.some(t => stat.includes(t))) return true;
    // Baseball red flags
    const baseballTerms = ['hit', 'home run', 'rbi', 'stolen base', 'strikeout', 'earned run', 'inning', 'pitch', 'total base', 'hitter fantasy', 'pitcher fantasy'];
    if (baseballTerms.some(t => stat.includes(t))) return true;
    // Basketball red flags
    const basketballTerms = ['point', 'rebound', 'assist', 'three-point', 'block', 'turnover', 'nba'];
    if (basketballTerms.some(t => stat.includes(t))) return true;
    return false;
  });
  if (hasNonMMAStats) return true;

  // Also re-seed if data contains old demo fighters (outdated cards from previous version)
  const oldFighters = ['dustin poirier', 'justin gaethje', 'sean strickland', 'alexander volkanovski'];
  const hasOldFighters = existingProps.some(p =>
    oldFighters.some(f => (p.playerName || '').toLowerCase().includes(f))
  );
  return hasOldFighters;
}

// ── MMA seed ─────────────────────────────────────────────────────────────────
async function seedMMA(): Promise<void> {
  const existing = await storage.getProps('MMA');

  // Clear contaminated data (NFL props stored under MMA key from a previous bug)
  if (existing.length > 0 && isMMAContaminated(existing)) {
    console.log(`[DemoSeed] MMA props appear contaminated with non-MMA data — clearing and reseeding`);
    await storage.deletePropsForLeague('MMA');
  } else if (existing.length > 0) {
    console.log(`[DemoSeed] MMA already has ${existing.length} props — skipping MMA seed`);
    return;
  }

  console.log('[DemoSeed] Seeding demo MMA props...');

  const rows: any[] = [];
  for (const fight of MMA_DEMO_FIGHTS) {
    const templateKey = Object.keys(MMA_FIGHT_PROPS).find(k => fight.gameId.startsWith(`${k}-`)) || Object.keys(MMA_FIGHT_PROPS)[0];
    const propTemplates = MMA_FIGHT_PROPS[templateKey] || MMA_FIGHT_PROPS['demo-mma-mcgregor-holloway'];

    // Encode event metadata into teamAbbr as JSON (fighters have no team abbr)
    // This lets routes.ts read event name, weight class, venue for the card header
    const eventMeta = JSON.stringify({
      event: fight.eventName || 'UFC Fight Night',
      weightClass: fight.weightClass || 'Unknown',
      venue: fight.venue || '',
    });

    propTemplates.forEach((p, idx) => {
      const isDemon = !!p.demon;
      const isGoblin = !!p.goblin;
      const score = getPropScore(isDemon, isGoblin, p.stat);
      const conf = getConfidence(isDemon, isGoblin, score);

      rows.push({
        id: `${fight.gameId}-${p.player.replace(/\s+/g, '-').toLowerCase()}-${idx}`,
        league: 'MMA',
        playerName: p.player,
        teamAbbr: eventMeta,  // JSON event metadata — parsed in routes.ts for card header
        statType: p.stat,
        lineScore: p.line,
        direction: 'over',
        isDemon,
        isGoblin,
        gameId: fight.gameId,
        gameMatchup: fight.matchup,  // "Fighter A vs Fighter B" format
        gameStartTime: fight.startTime,
        confidenceLevel: conf,
        propScore: Math.round(score * 1000) / 1000,
        rejectReason: null,
        scriptLabel: fight.scriptLabel,
        pulledAt: NOW,
      });
    });
  }

  await storage.upsertProps(rows);
  await storage.logPull('MMA', rows.length);
  console.log(`[DemoSeed] Seeded ${rows.length} MMA props across ${MMA_DEMO_FIGHTS.length} fights`);
}

// ── Main seed function ───────────────────────────────────────────────────────
export async function seedDemoData(): Promise<void> {
  await seedMLB();
  await seedMMA();

  // ── Seed demo settled slips (for Results page) ───────────────────────────
  await seedDemoSlips();
}

async function seedDemoSlips(): Promise<void> {
  // Use Central time for yesterday so demo results show on the correct CDT date
  const centralNow = new Date(Date.now() + (-5 * 60 * 60 * 1000));
  const yesterdayCentral = new Date(centralNow.getTime() - 86400000);
  const yesterday = yesterdayCentral.toISOString().slice(0, 10);

  // Slip 1: Win
  const slip1 = await storage.createSlip({
    league: 'MLB',
    gameMatchup: 'New York Yankees vs Boston Red Sox',
    gameStartTime: `${yesterday}T17:05:00Z`,
    scriptLabel: 'High Scoring Offense',
    status: 'settled_win',
    qualityScore: 0.78,
    correlationScore: 0.82,
    createdAt: `${yesterday}T15:00:00Z`,
    settledAt: `${yesterday}T22:00:00Z`,
  });

  await storage.createLegs([
    {
      slipId: slip1.id, playerName: 'Aaron Judge', teamAbbr: 'NYY',
      statType: 'Hitter Fantasy Score', lineScore: 35.5, direction: 'over',
      isDemon: true, isGoblin: false,
      gameStartTime: `${yesterday}T17:05:00Z`, status: 'hit', actualValue: 42.1,
      hitExplanation: 'Judge crushed 2 HRs and drove in 4 runs — exactly the high-scoring script.',
      propScore: 0.74,
    },
    {
      slipId: slip1.id, playerName: 'Gerrit Cole', teamAbbr: 'NYY',
      statType: 'Strikeouts', lineScore: 6.5, direction: 'over',
      isDemon: false, isGoblin: false,
      gameStartTime: `${yesterday}T17:05:00Z`, status: 'hit', actualValue: 9,
      hitExplanation: 'Cole was dominant — 9 Ks in 6 IP against a swing-happy lineup.',
      propScore: 0.55,
    },
    {
      slipId: slip1.id, playerName: 'Juan Soto', teamAbbr: 'NYY',
      statType: 'Total Bases', lineScore: 2.5, direction: 'over',
      isDemon: true, isGoblin: false,
      gameStartTime: `${yesterday}T17:05:00Z`, status: 'hit', actualValue: 4,
      hitExplanation: 'Soto doubled twice and scored — total bases demon cashed clean.',
      propScore: 0.68,
    },
  ]);

  // Slip 2: Loss
  const slip2 = await storage.createSlip({
    league: 'MLB',
    gameMatchup: 'LA Dodgers vs San Francisco Giants',
    gameStartTime: `${yesterday}T20:10:00Z`,
    scriptLabel: 'Pitcher Dominance',
    status: 'settled_loss',
    qualityScore: 0.61,
    correlationScore: 0.68,
    createdAt: `${yesterday}T18:00:00Z`,
    settledAt: `${yesterday}T23:30:00Z`,
  });

  await storage.createLegs([
    {
      slipId: slip2.id, playerName: 'Shohei Ohtani', teamAbbr: 'LAD',
      statType: 'Hitter Fantasy Score', lineScore: 38.5, direction: 'over',
      isDemon: true, isGoblin: false,
      gameStartTime: `${yesterday}T20:10:00Z`, status: 'hit', actualValue: 41.2,
      hitExplanation: 'Ohtani went 2-for-3 with a HR — demon hit.',
      propScore: 0.72,
    },
    {
      slipId: slip2.id, playerName: 'Freddie Freeman', teamAbbr: 'LAD',
      statType: 'Total Bases', lineScore: 2.5, direction: 'over',
      isDemon: false, isGoblin: false,
      gameStartTime: `${yesterday}T20:10:00Z`, status: 'miss', actualValue: 1,
      missExplanation: 'Freeman went 1-for-4 with a single. Script diverged — pitcher dominated all game.',
      propScore: 0.38,
    },
    {
      slipId: slip2.id, playerName: 'Logan Webb', teamAbbr: 'SF',
      statType: 'Strikeouts', lineScore: 7.5, direction: 'over',
      isDemon: false, isGoblin: false,
      gameStartTime: `${yesterday}T20:10:00Z`, status: 'miss', actualValue: 6,
      missExplanation: 'Webb left after 5 innings with tightness — unexpected exit killed the strikeout leg.',
      propScore: 0.29,
    },
  ]);

  console.log('[DemoSeed] Seeded 2 demo settled slips');
}
