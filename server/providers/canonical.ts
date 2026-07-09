/**
 * GOTit Canonical Prop Schema
 * Both PrizePicks and SportsGameOdds normalize into this shape.
 * The rest of the app only ever touches CanonicalProp — never raw provider data.
 */

export type PropTier = 'demon' | 'goblin' | 'standard';
export type PropSide = 'over' | 'under';
export type DataSource = 'prizepicks' | 'sportsgameodds' | 'moneyline' | 'cache' | 'demo';

export interface CanonicalProp {
  // Identity
  id: string;                   // stable id: `${source}:${sourcePropId}`
  sourcePropId: string;         // raw id from provider
  source: DataSource;
  isFallback: boolean;          // true if not from PP

  // Event
  league: string;
  gameId: string;               // provider game/event id
  gameMatchup: string;          // "AZ @ LAD"
  gameStartTime: string | null; // ISO

  // Player
  playerName: string;
  teamAbbr: string;

  // Prop
  statType: string;
  lineScore: number;
  direction: PropSide;

  // Tier
  isDemon: boolean;
  isGoblin: boolean;
  tier: PropTier;

  // Display truth — verbatim from source provider, never overwritten by GOTit internals
  ppDisplayMatchup: string | null;  // matchup exactly as PrizePicks returns it
  ppDisplayPlayer: string | null;   // player name exactly as PrizePicks returns it
  ppDisplayStat: string | null;     // stat type exactly as PrizePicks returns it
  ppDisplayTeam: string | null;     // team abbr/name exactly as PrizePicks returns it
  ppEventTitle: string | null;      // event title from PP (MMA event name etc)

  // Scoring (filled by enrichProps after normalization)
  confidenceLevel: number;
  confidenceLabel: string;
  propScore: number;
  rejectReason: string | null;
  fragility: number;
  trueProb: number;
  edge: number;

  // Script (filled by gameScript after grouping)
  scriptLabel: string | null;
  scriptNote: string | null;
  reason: string | null;

  pulledAt: string;
}

/** Minimal insert shape — scoring fields filled later by enrichProps */
export type RawCanonicalProp = Pick<
  CanonicalProp,
  | 'id' | 'sourcePropId' | 'source' | 'isFallback'
  | 'league' | 'gameId' | 'gameMatchup' | 'gameStartTime'
  | 'playerName' | 'teamAbbr'
  | 'statType' | 'lineScore' | 'direction'
  | 'isDemon' | 'isGoblin' | 'tier'
  | 'pulledAt'
> & Partial<Pick<CanonicalProp,
  | 'confidenceLevel' | 'confidenceLabel' | 'propScore' | 'rejectReason'
  | 'fragility' | 'trueProb' | 'edge'
  | 'scriptLabel' | 'scriptNote' | 'reason'
>>;

/** Map canonical prop to the DB storage row shape (matches schema.ts props table) */
export function canonicalToStorageRow(p: RawCanonicalProp): any {
  return {
    id: p.id,
    league: p.league,
    playerName: p.playerName,
    teamAbbr: p.teamAbbr,
    statType: p.statType,
    lineScore: p.lineScore,
    direction: p.direction,
    isDemon: p.isDemon,
    isGoblin: p.isGoblin,
    gameId: p.gameId,
    gameMatchup: p.gameMatchup,
    gameStartTime: p.gameStartTime,
    confidenceLevel: p.confidenceLevel ?? 3,
    propScore: p.propScore ?? 0,
    rejectReason: p.rejectReason ?? null,
    scriptLabel: p.scriptLabel ?? null,
    pulledAt: p.pulledAt,
    // Extra fields stored as JSON in a meta column if schema supports, else dropped
    _source: p.source,
    _isFallback: p.isFallback,
  };
}
