import { useState, useRef, useEffect, useMemo } from 'react';
import { useQuery, useMutation } from '@tanstack/react-query';
import { useLocation } from 'wouter';
import { apiRequest, queryClient } from '@/lib/queryClient';
import logoPath from '@assets/logo.png';
import sportMlb from '@assets/sport_mlb.png';
import sportNba from '@assets/sport_nba.png';
import sportNfl from '@assets/sport_nfl.png';
import sportMma from '@assets/sport_mma.png';
import demonImg from '@assets/demon.png';

const LEAGUES = [
  { id: 'MLB', label: 'MLB', img: sportMlb },
  { id: 'NBA', label: 'NBA', img: sportNba },
  { id: 'NFL', label: 'NFL', img: sportNfl },
  { id: 'MMA', label: 'MMA', img: sportMma },
];

interface Prop {
  id: string;
  playerName: string;
  teamAbbr?: string;
  statType: string;
  lineScore: number;
  direction: string;
  isDemon?: boolean;
  isGoblin?: boolean;
  gameId?: string;
  gameMatchup?: string;
  gameStartTime?: string;
  confidenceLevel?: number;
  confidenceLabel?: string;
  propScore?: number;
  rejectReason?: string;
  scriptLabel?: string;
  scriptNote?: string;
  reason?: string;
  trueProb?: number;
  edge?: number;
  EV?: number;
  fragility?: number;
  directionConfidence?: 'high' | 'medium' | 'low';
  gamesFound?: number;
  // Optimizer-enriched fields
  pWin?: number;
  evMarginal?: number;
  evCorrAdj?: number;
  optimizerTier?: 'demon' | 'standard';
  demonScore?: {
    composite: number;
    market_anchor: number;
    dist_hit_rate: number;
    game_script_fit: number;
    role_certainty: number;
    pair_diversity: number;
    tier?: string;
    gates_passed?: string[];
    gates_failed?: string[];
    p_win?: number;
  };
  fallback_render_used?: boolean;
}

interface OptimizerLeg {
  prop_id: string;
  player_name: string;
  stat_type: string;
  line: number;
  direction: string;
  tier: string;
  p_win: number;
  ev_marginal: number;
  ev_corr_adj: number;
  demon_score?: {
    composite: number;
    market_anchor: number;
    dist_hit_rate: number;
    game_script_fit: number;
    role_certainty: number;
    pair_diversity: number;
  };
}

interface OptimizerResult {
  [gameId: string]: {
    six_legs: OptimizerLeg[];
    two_demons: OptimizerLeg[];
    meta: Record<string, unknown>;
  };
}

interface GameCard {
  gameId: string;
  matchup: string;
  startTime: string;
  scriptLabel: string;
  scriptNote: string;
  // MMA-specific event metadata (null for non-MMA)
  eventName?: string | null;
  weightClass?: string | null;
  venue?: string | null;
  demons: Prop[];
  standards: Prop[];
  goblins: Prop[];
}

interface SlateResponse {
  league: string;
  props: Prop[];
  games: GameCard[];
  pulledAt?: string;
  // Provider metadata
  providerUsed?: 'prizepicks' | 'sportsgameodds' | 'cache' | 'demo';
  isFallback?: boolean;
  fallbackReason?: string | null;
  lastSuccessfulProvider?: string | null;
  lastSuccessfulPullAt?: string | null;
  rateLimited?: boolean;
  cooldownMs?: number | null;
}



function formatTime(iso?: string) {
  if (!iso) return 'TBD';
  try {
    return new Date(iso).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', timeZone: 'America/Chicago', timeZoneName: 'short' });
  } catch { return iso; }
}

function formatRelative(iso?: string) {
  if (!iso) return null;
  try {
    const diff = Date.now() - new Date(iso).getTime();
    if (diff < 60000) return 'just now';
    if (diff < 3600000) return `${Math.floor(diff / 60000)}m ago`;
    return `${Math.floor(diff / 3600000)}h ago`;
  } catch { return null; }
}

// ── Confidence display ────────────────────────────────────────────────────────
const CONF_LABELS: Record<number, { label: string; color: string }> = {
  1: { label: 'No Play', color: 'hsl(var(--muted-foreground))' },
  2: { label: 'Weak', color: 'hsl(var(--muted-foreground))' },
  3: { label: 'Neutral', color: 'hsl(42 96% 56%)' },
  4: { label: 'Strong', color: 'hsl(270 60% 70%)' },
  5: { label: 'Demon', color: 'hsl(0 72% 60%)' },
};

function ConfidenceDots({ level }: { level?: number }) {
  const n = Math.max(1, Math.min(5, level || 3));
  const info = CONF_LABELS[n] || CONF_LABELS[3];
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
      <div style={{ display: 'flex', gap: 3 }}>
        {[1, 2, 3, 4, 5].map(i => (
          <div key={i} style={{
            width: 5, height: 5, borderRadius: '50%',
            background: i <= n ? info.color : 'hsl(var(--g-border))',
            transition: 'background 200ms',
          }} />
        ))}
      </div>
      <span style={{ fontSize: '0.6rem', fontWeight: 700, letterSpacing: '0.06em', textTransform: 'uppercase', color: info.color }}>
        {info.label}
      </span>
    </div>
  );
}

// ── Demon prop card ───────────────────────────────────────────────────────────
function DemonCard({ prop, rank }: { prop: Prop; rank: number }) {
  const score = prop.propScore ?? 0.6;
  const pct = Math.round(score * 100);

  return (
    <div className="demon-pick" style={{ position: 'relative', overflow: 'hidden' }}>
      {/* Rank badge */}
      <div style={{
        position: 'absolute', top: 8, right: 8,
        width: 18, height: 18, borderRadius: '50%',
        background: rank === 1 ? 'hsl(0 72% 51%)' : 'hsl(0 72% 51% / 0.5)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        fontSize: '0.6rem', fontWeight: 800, color: '#fff',
      }}>
        {rank}
      </div>

      {/* Player + team */}
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8, marginBottom: 6 }}>
        <img src={demonImg} alt="Demon" style={{ width: 26, height: 26, borderRadius: '50%', objectFit: 'cover', flexShrink: 0, border: '1px solid hsl(0 72% 51% / 0.5)' }} />
        <div>
          <div className="demon-pick-player">{prop.playerName}</div>
          {prop.teamAbbr && (
            <div style={{ fontSize: '0.6rem', fontWeight: 700, color: 'hsl(var(--muted-foreground))', letterSpacing: '0.06em', textTransform: 'uppercase' }}>
              {prop.teamAbbr}
            </div>
          )}
        </div>
      </div>

      {/* Stat line */}
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 6, marginBottom: 8 }}>
        <span className="demon-pick-line">{prop.direction === 'over' ? '↑' : '↓'} {prop.lineScore}</span>
        <span className="demon-pick-stat">{prop.statType}</span>
      </div>

      {/* Confidence */}
      <ConfidenceDots level={prop.confidenceLevel} />

      {/* Score bar */}
      <div style={{ marginTop: 8, height: 2, background: 'hsl(var(--g-border))', borderRadius: 9999 }}>
        <div style={{
          height: '100%', borderRadius: 9999,
          width: `${pct}%`,
          background: 'linear-gradient(90deg, hsl(0 72% 51%), hsl(42 96% 56%))',
          transition: 'width 600ms cubic-bezier(0.22,1,0.36,1)',
        }} />
      </div>
      <div style={{ fontSize: '0.6rem', fontWeight: 700, color: 'hsl(var(--g-gold))', marginTop: 3, fontFamily: 'Space Mono, monospace' }}>
        {pct}%
      </div>

      {/* Reason */}
      {prop.reason && (
        <div style={{
          marginTop: 8, fontSize: '0.6875rem', color: 'hsl(var(--muted-foreground))',
          lineHeight: 1.45, borderTop: '1px solid hsl(var(--g-border))', paddingTop: 8,
          fontStyle: 'italic',
        }}>
          {prop.reason}
        </div>
      )}

      {/* Diagnostic: pipeline path info */}
      {prop.demonScore?.tier && (
        <div style={{ marginTop: 6, fontSize: '0.55rem', fontWeight: 700, color: 'hsl(220 60% 65%)', letterSpacing: '0.06em', fontFamily: 'Space Mono, monospace' }}>
          tier={prop.demonScore.tier} p_win={prop.demonScore.p_win != null ? (prop.demonScore.p_win * 100).toFixed(1) + '%' : 'n/a'}
        </div>
      )}
      {prop.fallback_render_used && (
        <div style={{ marginTop: 2, fontSize: '0.55rem', fontWeight: 700, color: 'hsl(40 90% 60%)', letterSpacing: '0.06em' }}>
          ⚠ DIRECT PATH
        </div>
      )}
    </div>
  );
}

// ── Game card ─────────────────────────────────────────────────────────────────
// ── Selectable prop row ───────────────────────────────────────────────────────
function PropRow({ prop, isSelected, onToggle, disabled }: {
  prop: Prop; isSelected: boolean; onToggle: () => void; disabled: boolean;
}) {
  const isGoblin = prop.isGoblin;
  const isDemon = prop.isDemon;
  // Confidence % derived from trueProb (0-1 float) or confidenceLevel (1-5)
  const confidencePct = prop.trueProb != null
    ? Math.round(prop.trueProb * 100)
    : prop.confidenceLevel != null
      ? Math.round((prop.confidenceLevel / 5) * 100)
      : null;
  const isOver = prop.direction !== 'under';
  const dirConf = prop.directionConfidence ?? 'medium';
  // Conviction-driven badge opacity — low conviction = dimmer badge
  const badgeOpacity = dirConf === 'high' ? 1.0 : dirConf === 'medium' ? 0.75 : 0.45;
  // Show data indicator dot when backed by player recents
  const hasRecents = (prop.gamesFound ?? 0) >= 5;
  // Standard props (not demon, not goblin) can be flipped by the user
  // Direction is GOTit-decided and locked — no user flipping ever

  return (
    <div
      onClick={!disabled || isSelected ? onToggle : undefined}
      style={{
        display: 'flex', alignItems: 'center', gap: 10,
        padding: '9px 10px',
        borderRadius: 8,
        cursor: disabled && !isSelected ? 'default' : 'pointer',
        transition: 'all 150ms',
        background: isSelected
          ? 'hsl(var(--g-gold) / 0.12)'
          : isGoblin ? 'hsl(270 60% 60% / 0.06)' : 'hsl(var(--g-surface-2))',
        border: `1px solid ${
          isSelected ? 'hsl(var(--g-gold) / 0.6)'
          : isGoblin ? 'hsl(270 60% 60% / 0.2)'
          : 'hsl(var(--g-border))'
        }`,
        opacity: disabled && !isSelected ? 0.45 : 1,
      }}
      data-testid={`prop-row-${prop.id}`}
    >
      {/* Add / check button */}
      <div style={{
        width: 22, height: 22, borderRadius: '50%', flexShrink: 0,
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        transition: 'all 150ms',
        background: isSelected ? 'hsl(var(--g-gold))' : 'hsl(var(--background))',
        border: `1.5px solid ${isSelected ? 'hsl(var(--g-gold))' : 'hsl(var(--g-border))'}`,
      }}>
        {isSelected ? (
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="#000" strokeWidth="3">
            <polyline points="20 6 9 17 4 12"/>
          </svg>
        ) : (
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="hsl(var(--g-gold))" strokeWidth="2.5">
            <line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/>
          </svg>
        )}
      </div>

      {/* Player + stat */}
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: '0.78rem', fontWeight: 700, color: 'hsl(var(--foreground))', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
          {prop.playerName}
          {prop.teamAbbr && (() => {
            // Don't show teamAbbr if it looks like JSON (MMA event metadata)
            const abbr = prop.teamAbbr || '';
            if (abbr.startsWith('{')) return null;
            return <span style={{ marginLeft: 5, fontSize: '0.62rem', fontWeight: 600, color: 'hsl(var(--muted-foreground))' }}>{abbr}</span>;
          })()}
        </div>
        <div style={{ fontSize: '0.68rem', color: 'hsl(var(--muted-foreground))', marginTop: 1 }}>
          {prop.statType}
          {isGoblin && <span style={{ marginLeft: 6, fontSize: '0.55rem', fontWeight: 800, letterSpacing: '0.08em', textTransform: 'uppercase', color: 'hsl(270 60% 70%)', background: 'hsl(270 60% 60% / 0.15)', padding: '1px 5px', borderRadius: 8 }}>GOB</span>}
        </div>
        {/* PP Shade Signal label */}
        {(() => {
          const shade = (prop as any).ppShadeSignal;
          const moveCount = (prop as any).lineMoveCount ?? 0;
          const firstLine = (prop as any).firstSeenLine;
          const currentLine = prop.lineScore;
          const moved = firstLine != null && firstLine !== currentLine;
          if (!shade || shade === 'no_data') return null;
          const shadeConfig: Record<string, {label: string; color: string; bg: string}> = {
            lean_over:  { label: 'PP Shaded Over',  color: 'hsl(142 72% 50%)', bg: 'hsl(142 72% 46% / 0.12)' },
            lean_under: { label: 'PP Shaded Under', color: 'hsl(210 80% 65%)', bg: 'hsl(210 80% 60% / 0.12)' },
            neutral:    { label: 'PP Neutral',       color: 'hsl(var(--muted-foreground))', bg: 'hsl(var(--g-surface-2))' },
          };
          const cfg = shadeConfig[shade];
          if (!cfg) return null;
          return (
            <div style={{ display: 'flex', gap: 4, marginTop: 2, flexWrap: 'wrap' }}>
              <span style={{ fontSize: '0.52rem', fontWeight: 800, letterSpacing: '0.06em', textTransform: 'uppercase', color: cfg.color, background: cfg.bg, padding: '1px 5px', borderRadius: 6, border: `1px solid ${cfg.color}30` }}>
                {cfg.label}
              </span>
              {moved && (
                <span style={{ fontSize: '0.52rem', fontWeight: 800, letterSpacing: '0.06em', textTransform: 'uppercase', color: 'hsl(45 90% 60%)', background: 'hsl(45 90% 60% / 0.12)', padding: '1px 5px', borderRadius: 6, border: '1px solid hsl(45 90% 60% / 0.3)' }}>
                  {currentLine > firstLine ? '↑ Moved Up' : '↓ Moved Down'} ×{moveCount}
                </span>
              )}
            </div>
          );
        })()}
        {/* Edge reasons — why GOTit picked this side */}
        {(() => {
          const reasons: string[] = (prop as any).edgeReasons ?? [];
          if (reasons.length === 0) return null;
          const labelMap: Record<string, string> = {
            sharp_line_gap:  'Sharp Gap',
            shade_confirmed: 'Shade Confirmed',
            line_moved:      'Line Moved',
            high_p_win:      'High Win %',
            strong_role:     'Strong Role',
            script_fit:      'Script Fit',
          };
          return (
            <div style={{ display: 'flex', gap: 3, marginTop: 2, flexWrap: 'wrap' }}>
              {reasons.map(r => (
                <span key={r} style={{ fontSize: '0.50rem', fontWeight: 700, letterSpacing: '0.05em', textTransform: 'uppercase', color: 'hsl(45 90% 65%)', background: 'hsl(45 90% 60% / 0.10)', padding: '1px 4px', borderRadius: 5, border: '1px solid hsl(45 90% 60% / 0.25)' }}>
                  {labelMap[r] ?? r}
                </span>
              ))}
            </div>
          );
        })()}
      </div>

      {/* Line + direction badge */}
      <div style={{ textAlign: 'right', flexShrink: 0, display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 3 }}>
        <div style={{ fontSize: '0.88rem', fontWeight: 800, fontFamily: 'Space Mono, monospace', color: isSelected ? 'hsl(var(--g-gold))' : 'hsl(var(--foreground))' }}>
          {prop.lineScore}
        </div>
        {/* Direction arrow badge — GOTit-decided, locked */}
        <div
          onClick={undefined}
          title={undefined}
          style={{
            display: 'flex', alignItems: 'center', gap: 3,
            padding: '2px 6px', borderRadius: 6,
            background: isOver
              ? 'hsl(142 72% 46% / 0.15)'
              : 'hsl(210 80% 60% / 0.15)',
            border: `1px solid ${isOver ? 'hsl(142 72% 46% / 0.4)' : 'hsl(210 80% 60% / 0.4)'}`,
            cursor: 'default',
            transition: 'all 150ms',
            userSelect: 'none',
            opacity: badgeOpacity,
          }}
          data-testid={`dir-badge-${prop.id}`}
        >
          <span style={{ fontSize: '0.75rem', fontWeight: 900, lineHeight: 1, color: isOver ? 'hsl(142 72% 50%)' : 'hsl(210 80% 65%)' }}>
            {isOver ? '↑' : '↓'}
          </span>
          <span style={{ fontSize: '0.58rem', fontWeight: 800, letterSpacing: '0.05em', textTransform: 'uppercase', color: isOver ? 'hsl(142 72% 50%)' : 'hsl(210 80% 65%)' }}>
            {isOver ? 'OVER' : 'UNDR'}
          </span>
          {/* Green dot = backed by player recents data */}
          {hasRecents && (
            <span style={{ width: 4, height: 4, borderRadius: '50%', background: 'hsl(142 72% 46%)', flexShrink: 0, marginLeft: 1 }} />
          )}
        </div>
        {/* Confidence % from directional scorer */}
        {confidencePct != null && (
          <div style={{ fontSize: '0.55rem', fontWeight: 700, letterSpacing: '0.04em', color: 'hsl(var(--muted-foreground))', fontFamily: 'Space Mono, monospace' }}>
            {confidencePct}%
          </div>
        )}
        {/* p_win from MILP optimizer */}
        {prop.pWin != null && (
          <div style={{ fontSize: '0.55rem', fontWeight: 800, letterSpacing: '0.04em', color: 'hsl(42 96% 56%)', fontFamily: 'Space Mono, monospace' }}>
            {Math.round(prop.pWin * 100)}% WIN
          </div>
        )}
      </div>
    </div>
  );
}

function GameCard({ game, selectedIds, onToggle, atMax, onSave, isSaving }: {
  game: GameCard;
  selectedIds: Set<string>;
  onToggle: (prop: any) => void;
  atMax: boolean;
  onSave: (gameId: string, selectedPropIds: string[]) => void;
  isSaving: boolean;
}) {
  const [propCount, setPropCount] = useState<number>(0);
  const isActivated = propCount > 0;
  // Direction is GOTit-decided and locked — dirOverrides removed

  // Direction is GOTit-decided and locked. No flip. No override.

  const allStandards = [...game.standards, ...game.goblins]
    .sort((a, b) => (b.propScore ?? 0) - (a.propScore ?? 0));
  const visibleProps = propCount > 0 ? allStandards.slice(0, propCount) : [];

  // Props visible on this card (standards + demons when activated)
  const visibleDemonIds = isActivated ? game.demons.map((d: any) => d.id) : [];
  const visibleStandardIds = visibleProps.map((p: any) => p.id);
  const allVisibleIds = new Set([...visibleStandardIds, ...visibleDemonIds]);

  // Selected props that belong to THIS card
  const cardSelectedIds = [...selectedIds].filter(id => allVisibleIds.has(id));
  const canSave = cardSelectedIds.length >= 2;

  return (
    <div className="game-card animate-in">
      {/* Header */}
      <div className="game-header">
        <div style={{ flex: 1, minWidth: 0 }}>
          {/* MMA event badge */}
          {game.eventName && (
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 4 }}>
              <span style={{
                fontSize: '0.6rem', fontWeight: 800, letterSpacing: '0.1em',
                textTransform: 'uppercase',
                color: 'hsl(var(--g-gold))',
                background: 'hsl(var(--g-gold) / 0.12)',
                border: '1px solid hsl(var(--g-gold) / 0.3)',
                borderRadius: 4, padding: '1px 6px',
              }}>
                {game.eventName}
              </span>
              {game.weightClass && (
                <span style={{
                  fontSize: '0.6rem', fontWeight: 600, letterSpacing: '0.06em',
                  textTransform: 'uppercase', color: 'hsl(var(--muted-foreground))',
                }}>
                  {game.weightClass}
                </span>
              )}
            </div>
          )}
          <div className="matchup">{game.matchup}</div>
          <div className="game-meta">
            <span className="game-time">
              <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ display: 'inline', marginRight: 3, verticalAlign: 'middle' }}>
                <circle cx="12" cy="12" r="10" /><path d="M12 6v6l4 2" />
              </svg>
              {formatTime(game.startTime)}
            </span>
            <span className="script-label">{game.scriptLabel}</span>
            {game.venue && (
              <span style={{ fontSize: '0.65rem', color: 'hsl(var(--muted-foreground))', opacity: 0.7, marginLeft: 2 }}>
                · {game.venue}
              </span>
            )}
          </div>
        </div>
      </div>

      {/* Script note */}
      {game.scriptNote && (
        <div style={{
          fontSize: '0.75rem', color: 'hsl(var(--muted-foreground))',
          lineHeight: 1.5, margin: '8px 0',
          padding: '8px 10px',
          background: 'hsl(var(--g-surface-2))',
          borderLeft: '2px solid hsl(var(--g-gold) / 0.5)',
          borderRadius: '0 6px 6px 0',
        }}>
          {game.scriptNote}
        </div>
      )}

      {/* Gold divider */}
      <div className="g-divider-gold" style={{ margin: '10px 0 12px' }} />

      {/* 2–6 pick selector */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 12 }}>
        <span style={{ fontSize: '0.6rem', fontWeight: 700, letterSpacing: '0.08em', textTransform: 'uppercase', color: 'hsl(var(--muted-foreground))' }}>
          Picks
        </span>
        <div style={{ display: 'flex', gap: 6 }}>
          {[2, 3, 4, 5, 6].map(n => (
            <button
              key={n}
              onClick={() => setPropCount(prev => prev === n ? 0 : n)}
              data-testid={`pick-count-${game.gameId}-${n}`}
              style={{
                width: 28, height: 28, borderRadius: '50%',
                border: `1.5px solid ${propCount === n ? 'hsl(var(--g-gold))' : 'hsl(var(--g-border))'}`,
                background: propCount === n ? 'hsl(var(--g-gold))' : 'transparent',
                color: propCount === n ? '#000' : 'hsl(var(--muted-foreground))',
                fontFamily: 'Space Mono, monospace',
                fontSize: '0.68rem', fontWeight: 800,
                cursor: 'pointer',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                transition: 'all 150ms',
              }}
            >
              {n}
            </button>
          ))}
        </div>
      </div>

      {/* Standard / Goblin props — revealed when a count is selected, each selectable */}
      {visibleProps.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 5, marginBottom: 12 }}>
          {visibleProps.map(p => {
            const overriddenProp = p;
            return (
              <PropRow
                key={p.id}
                prop={overriddenProp}
                isSelected={selectedIds.has(p.id)}
                onToggle={() => onToggle({ ...overriddenProp })}
                disabled={atMax && !selectedIds.has(p.id)}
              />
            );
          })}
        </div>
      )}

      {/* Save to Slip button — active when ≥2 props from this card are selected */}
      {isActivated && (
        <button
          onClick={() => onSave(game.gameId, cardSelectedIds)}
          disabled={!canSave || isSaving}
          data-testid={`save-slip-${game.gameId}`}
          style={{
            width: '100%',
            marginBottom: 14,
            padding: '0.5rem 0',
            borderRadius: 8,
            border: `1px solid ${canSave ? 'hsl(var(--g-gold) / 0.7)' : 'hsl(var(--g-border))'}`,
            background: canSave ? 'hsl(var(--g-gold) / 0.1)' : 'transparent',
            color: canSave ? 'hsl(var(--g-gold))' : 'hsl(var(--muted-foreground))',
            fontFamily: 'Space Grotesk, sans-serif',
            fontSize: '0.72rem',
            fontWeight: 700,
            letterSpacing: '0.06em',
            textTransform: 'uppercase',
            cursor: canSave ? 'pointer' : 'default',
            transition: 'all 150ms',
            display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 6,
          }}
        >
          {isSaving ? (
            <>
              <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"
                style={{ animation: 'spin 0.7s linear infinite' }}>
                <path d="M21 12a9 9 0 1 1-6.219-8.56" />
              </svg>
              Saving…
            </>
          ) : (
            <>
              <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                <path d="M5 12h14M12 5l7 7-7 7" />
              </svg>
              {cardSelectedIds.length === 0
                ? 'Tap props to select'
                : cardSelectedIds.length < 2
                  ? `Select ${2 - cardSelectedIds.length} more to save`
                  : `Save to Slip · ${cardSelectedIds.length} picks`}
            </>
          )}
        </button>
      )}

      {/* Demon Picks — force-render even when empty for diagnostic visibility */}
      {isActivated && (
        <div className="demon-section">
          <div className="demon-section-header">
            <svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor">
              <path d="M12 2L4 7v6c0 5.25 3.75 10.15 8 11 4.25-.85 8-5.75 8-11V7l-8-5z" />
            </svg>
            Demon Picks
            <span style={{ marginLeft: 'auto', fontSize: '0.6rem', fontWeight: 700, color: 'hsl(0 72% 60%)', letterSpacing: '0.06em' }}>
              TOP {game.demons.length}
            </span>
          </div>
          {/* Fallback render diagnostic tag */}
          {game.demons.some((d: any) => d.fallback_render_used) && (
            <div style={{ fontSize: '0.55rem', fontWeight: 700, color: 'hsl(40 90% 60%)', letterSpacing: '0.08em', marginTop: 4, textTransform: 'uppercase' }}>
              ⚠ DIRECT PATH (fallback_render_used) — MILP bypassed
            </div>
          )}
          {/* Demon section — exactly top 2 GOTit-qualified demons from qualify_demons.py */}
          {game.demons.length === 0 ? (
            <div style={{ fontSize: '0.65rem', color: 'hsl(0 60% 55%)', fontWeight: 600, padding: '10px 0 6px', letterSpacing: '0.04em' }}>
              ⚠ PIPELINE RETURNED 0 DEMONS — all candidates failed gating for this game
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10, marginTop: 10, marginBottom: 8 }}>
              {game.demons.map((d: any, i: number) => (
                <DemonCard key={d.id} prop={d} rank={i + 1} />
              ))}
            </div>
          )}
          <div style={{ display: 'flex', flexDirection: 'column', gap: 5, marginTop: 4 }}>
            {game.demons.map((d: any) => (
              <PropRow
                key={`sel-${d.id}`}
                prop={d}
                isSelected={selectedIds.has(d.id)}
                onToggle={() => onToggle(d)}
                disabled={atMax && !selectedIds.has(d.id)}
              />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// ── Fallback state messages ───────────────────────────────────────────────────
type FallbackReason = 'loading' | 'no_props' | 'pull_failed' | 'grouping_failed' | 'no_games';

function SlateEmptyState({ reason, onPull, isPulling }: { reason: FallbackReason; onPull: () => void; isPulling: boolean }) {
  const states: Record<FallbackReason, { icon: string; title: string; sub: string; showPull: boolean }> = {
    loading: { icon: '◌', title: 'Loading live slate…', sub: 'GOTit is analyzing today\'s board.', showPull: false },
    no_props: { icon: '◈', title: 'No live slate from source', sub: 'PrizePicks has no active projections right now. Pull to check again.', showPull: true },
    pull_failed: { icon: '◆', title: 'Pull failed — retry', sub: 'Could not reach the props source. Check connection and retry.', showPull: true },
    grouping_failed: { icon: '◇', title: 'Props pulled, game grouping failed', sub: 'Props loaded but could not be organized into game cards.', showPull: true },
    no_games: { icon: '◎', title: 'Demons selected, no complete games', sub: 'Not enough props per game to build a full card.', showPull: true },
  };
  const s = states[reason];
  return (
    <div className="empty-state">
      <div className="empty-icon" style={{ fontSize: '1.5rem', fontFamily: 'Space Mono, monospace', color: 'hsl(var(--g-gold))' }}>{s.icon}</div>
      <h3 style={{ fontSize: '0.9rem', fontWeight: 700, marginBottom: 6 }}>{s.title}</h3>
      <p style={{ fontSize: '0.8rem', color: 'hsl(var(--muted-foreground))', maxWidth: 260, textAlign: 'center', lineHeight: 1.5 }}>{s.sub}</p>
      {s.showPull && (
        <button className="btn-gold" style={{ marginTop: 14 }} onClick={onPull} disabled={isPulling} data-testid="empty-pull-btn">
          {isPulling ? 'Pulling…' : 'Pull Live Board'}
        </button>
      )}
    </div>
  );
}

// ── Main SlatePage ────────────────────────────────────────────────────────────
export default function SlatePage() {
  const [, setLocation] = useLocation();
  const [league, setLeague] = useState('MLB');
  // Reset optimizer when league changes
  const prevLeague = useRef(league);
  // Global prop basket — tracks which props the user has selected across all cards
  const [selectedPropIds, setSelectedPropIds] = useState<Set<string>>(new Set());
  const [selectedPropMap, setSelectedPropMap] = useState<Record<string, any>>({});
  const [savingGameId, setSavingGameId] = useState<string | null>(null);
  const MAX_PICKS = 6;

  function handleToggle(prop: any) {
    setSelectedPropIds(prev => {
      const next = new Set(prev);
      if (next.has(prop.id)) {
        next.delete(prop.id);
        setSelectedPropMap(m => { const n = { ...m }; delete n[prop.id]; return n; });
      } else {
        if (next.size >= MAX_PICKS) return prev; // hard cap
        next.add(prop.id);
        setSelectedPropMap(m => ({ ...m, [prop.id]: prop }));
      }
      return next;
    });
  }

  const { data, isLoading, isError } = useQuery<SlateResponse>({
    queryKey: ['/api/slate', league],
    queryFn: () => apiRequest('GET', `/api/slate?league=${league}`).then(r => r.json()),
    staleTime: 0,
    refetchOnWindowFocus: false,
  });

  const pullMutation = useMutation({
    mutationFn: async () => {
      const r = await apiRequest('POST', `/api/pull?league=${league}`);
      const json = await r.json();
      // Only throw on hard server errors (no cache at all)
      if (!r.ok && json.ok === false) throw new Error(json.error || 'Pull failed');
      return json; // may include fromCache, rateLimited, cooldownMs
    },
    onSuccess: () => {
      // Always refetch slate so banner/source info updates
      queryClient.invalidateQueries({ queryKey: ['/api/slate', league] });
      queryClient.refetchQueries({ queryKey: ['/api/slate', league] });
    },
    onError: (err: any) => {
      console.error('[GOTit] Pull error:', err.message);
    },
  });

  // Derive pull status label for banner
  const pullResult: any = pullMutation.data;
  // Use pull result if fresh, otherwise fall back to slate response metadata
  const activePullResult = pullResult ?? {};
  const isRateLimited = activePullResult.rateLimited ?? data?.rateLimited ?? false;
  const fromCache = activePullResult.fromCache ?? (data?.providerUsed === 'cache') ?? false;
  const cooldownMs: number | null = activePullResult.cooldownMs ?? data?.cooldownMs ?? null;
  const cooldownMin = cooldownMs ? Math.ceil(cooldownMs / 60000) : null;
  const providerUsed = activePullResult.providerUsed ?? data?.providerUsed ?? null;
  const isLive = !fromCache && (providerUsed === 'prizepicks' || providerUsed === 'sportsgameodds');
  const sourceName = providerUsed === 'prizepicks'
    ? 'PrizePicks'
    : providerUsed === 'sportsgameodds'
    ? 'SportsGameOdds'
    : providerUsed === 'cache'
    ? 'Cache'
    : providerUsed === 'demo'
    ? 'Demo'
    : null;

  const [savingTray, setSavingTray] = useState(false);

  // Save a slip from the props the user hand-picked on this game card (single-game convenience)
  async function handleSave(gameId: string, cardSelectedIds: string[]) {
    if (cardSelectedIds.length < 2) return;
    setSavingGameId(gameId);
    try {
      const game = games.find((g: any) => g.gameId === gameId);
      // Collect optimized game state (enriched with demonScore, pWin)
      const optGame = optimizedGames.find((g: any) => g.gameId === gameId);
      const allOptProps = optGame
        ? [...(optGame.demons || []), ...(optGame.standards || []), ...(optGame.goblins || [])]
        : [];
      // Build demonScores map: propId -> demonScore for any selected demon legs
      const demonScores: Record<string, any> = {};
      for (const pid of cardSelectedIds) {
        const p = allOptProps.find((x: any) => x.id === pid);
        if (p?.demonScore) demonScores[pid] = { ...p.demonScore, pWin: p.pWin };
      }
      const body = {
        league,
        gameId,
        propIds: cardSelectedIds,
        matchup: game?.matchup || '',
        startTime: game?.startTime || '',
        scriptLabel: game?.scriptLabel || '',
        demonScores,
      };
      await apiRequest('POST', '/api/slips/build', body);
      // Invalidate and force-refetch so SlipPage shows the new slip immediately
      await queryClient.invalidateQueries({ queryKey: ['/api/slips', 'active'] });
      await queryClient.invalidateQueries({ queryKey: ['/api/slips', 'all'] });
      await queryClient.refetchQueries({ queryKey: ['/api/slips', 'active'] });
      // Clear only the props from this game card
      setSelectedPropIds(prev => {
        const next = new Set(prev);
        cardSelectedIds.forEach(id => next.delete(id));
        return next;
      });
      setSelectedPropMap(m => {
        const n = { ...m };
        cardSelectedIds.forEach(id => delete n[id]);
        return n;
      });
      setLocation('/slip');
    } catch (err: any) {
      console.error('[GOTit] Save to Slip failed:', err);
    } finally {
      setSavingGameId(null);
    }
  }

  // Save a cross-game slip from the global tray (all selected props across any game)
  async function handleSaveTray() {
    const allIds = [...selectedPropIds];
    if (allIds.length < 2) return;
    setSavingTray(true);
    try {
      // Build matchup from first prop's game metadata
      const firstProp = selectedPropMap[allIds[0]];
      // Collect demonScores for all selected demon props across all games
      const trayDemonScores: Record<string, any> = {};
      for (const pid of allIds) {
        const p = selectedPropMap[pid];
        if (p?.demonScore) trayDemonScores[pid] = { ...p.demonScore, pWin: p.pWin };
      }
      const body = {
        league,
        gameId: `cross-${Date.now()}`,
        propIds: allIds,
        matchup: firstProp?.gameMatchup || '',
        startTime: firstProp?.gameStartTime || '',
        scriptLabel: 'Cross-Game Slip',
        demonScores: trayDemonScores,
      };
      await apiRequest('POST', '/api/slips/build', body);
      await queryClient.invalidateQueries({ queryKey: ['/api/slips', 'active'] });
      await queryClient.invalidateQueries({ queryKey: ['/api/slips', 'all'] });
      await queryClient.refetchQueries({ queryKey: ['/api/slips', 'active'] });
      // Clear all selections
      setSelectedPropIds(new Set());
      setSelectedPropMap({});
      setLocation('/slip');
    } catch (err: any) {
      console.error('[GOTit] Save cross-game slip failed:', err);
    } finally {
      setSavingTray(false);
    }
  }

  const games: GameCard[] = data?.games || [];
  const pulledAt = data?.pulledAt;

  // ── Optimizer integration ─────────────────────────────────────────────────
  const [optimizerResult, setOptimizerResult] = useState<OptimizerResult | null>(null);
  const [isOptimizing, setIsOptimizing] = useState(false);

  // Clear optimizer result on league change
  useEffect(() => {
    if (prevLeague.current !== league) {
      prevLeague.current = league;
      setOptimizerResult(null);
    }
  }, [league]);

  // Fire optimizer whenever we get fresh game data
  useEffect(() => {
    if (games.length === 0) return;
    // Collect all props across all games
    const allProps: any[] = [];
    for (const g of games) {
      // Slate optimizer only receives standards + goblins.
      // Demons are a separate pipeline (qualify_demons.py) — never mixed in here.
      const gProps = [...(g.standards || []), ...(g.goblins || [])];
      for (const p of gProps) {
        allProps.push({
          propId: p.id,
          gameId: g.gameId,
          playerName: p.playerName,
          teamAbbr: p.teamAbbr || '',
          statType: p.statType,
          lineScore: p.lineScore,
          isDemon: false,
          isGoblin: p.isGoblin || false,
          direction: p.direction || 'over',
          gameMatchup: p.gameMatchup || g.matchup || '',
          gameStartTime: p.gameStartTime || g.startTime || '',
        });
      }
    }
    if (allProps.length === 0) return;

    setIsOptimizing(true);
    apiRequest('POST', '/api/optimize', allProps)
      .then(r => r.json())
      .then((result: OptimizerResult) => {
        if (result && !result.error) {
          setOptimizerResult(result);
        }
      })
      .catch((e: any) => {
        console.warn('[GOTit] Optimizer unavailable, using default ordering:', e.message);
      })
      .finally(() => setIsOptimizing(false));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data?.pulledAt, league]);

  // Merge optimizer results into games: re-rank standards by p_win, replace demons
  const optimizedGames: GameCard[] = useMemo(() => {
    if (!optimizerResult) return games;
    return games.map(g => {
      const opt = optimizerResult[g.gameId];
      if (!opt) return g;

      // Build p_win lookup by player_name+stat_type (prop_id may differ)
      const legLookup = new Map<string, OptimizerLeg>();
      for (const leg of [...(opt.six_legs || []), ...(opt.two_demons || [])]) {
        const key = `${leg.player_name}|${leg.stat_type}`;
        legLookup.set(key, leg);
        if (leg.prop_id) legLookup.set(leg.prop_id, leg);
      }

      function enrichProp(p: Prop): Prop {
        const key = `${p.playerName}|${p.statType}`;
        const leg = legLookup.get(p.id || '') || legLookup.get(key);
        if (!leg) return p;
        return { ...p, pWin: leg.p_win, evMarginal: leg.ev_marginal, evCorrAdj: leg.ev_corr_adj, demonScore: leg.demon_score };
      }

      // Filter standards+goblins to ONLY the MILP-selected six_legs.
      // The optimizer is the source of truth for which props appear.
      // Raw PP props that were not selected by the MILP do not show.
      const sixLegIds = new Set((opt.six_legs || []).map((l: OptimizerLeg) => l.prop_id).filter(Boolean));
      const sixLegKeys = new Set((opt.six_legs || []).map((l: OptimizerLeg) => `${l.player_name}|${l.stat_type}`));

      const selectedStandards = [...g.standards, ...g.goblins]
        .filter((p: Prop) => sixLegIds.has(p.id) || sixLegKeys.has(`${p.playerName}|${p.statType}`))
        .map(enrichProp)
        .sort((a, b) => (b.pWin ?? b.propScore ?? 0) - (a.pWin ?? a.propScore ?? 0));

      // Fallback: if optimizer returned no six_legs for this game, show enriched raw props
      const enrichedStandards = selectedStandards.length >= 6
        ? selectedStandards
        : g.standards.map(enrichProp).sort((a, b) => (b.pWin ?? b.propScore ?? 0) - (a.pWin ?? a.propScore ?? 0));
      const enrichedGoblins = selectedStandards.length >= 6
        ? []   // goblins already merged into selectedStandards above
        : g.goblins.map(enrichProp).sort((a, b) => (b.pWin ?? b.propScore ?? 0) - (a.pWin ?? a.propScore ?? 0));

      // PP is the display source of truth for demons — never replace PP's isDemon=true props.
      // The optimizer enriches demons with p_win but does NOT reassign which props are demons.
      // PP's odds_type=="demon" flag from the pull is the only authority on demon status.
      const enrichedDemons = g.demons.map(enrichProp);

      return { ...g, standards: enrichedStandards, goblins: enrichedGoblins, demons: enrichedDemons };
    });
  }, [games, optimizerResult]);

  // Determine fallback reason
  function getFallbackReason(): FallbackReason {
    if (isLoading) return 'loading';
    if (isError || pullMutation.isError) return 'pull_failed';
    if (!data) return 'no_props';
    if (data.props.length === 0) return 'no_props';
    if (games.length === 0 && data.props.length > 0) return 'no_games';
    return 'no_games';
  }

  const showEmpty = !isLoading && games.length === 0;

  return (
    <div style={{ minHeight: '100dvh' }}>
      {/* Header */}
      <header className="app-header">
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            {/* Long-press on logo to open diagnostics */}
            <img
              src={logoPath}
              alt="GOTit"
              style={{ height: 28, width: 'auto', objectFit: 'contain', userSelect: 'none', WebkitUserSelect: 'none' }}
              data-testid="logo"
            />
            <div>
              <div style={{ fontSize: '1rem', fontWeight: 800, letterSpacing: '-0.02em', lineHeight: 1 }}>
                <span className="text-gold-gradient">GOT</span><span style={{ color: 'hsl(var(--foreground))' }}>it</span>
              </div>
              <div style={{ fontSize: '0.6rem', fontWeight: 700, letterSpacing: '0.12em', textTransform: 'uppercase', color: 'hsl(var(--muted-foreground))', lineHeight: 1 }}>
                Prop Intelligence
              </div>
            </div>
          </div>

          {/* Game status indicator — only green if a game is currently in progress */}
          {(() => {
            const now = Date.now();
            const games: GameCard[] = data?.games ?? [];
            const hasLiveGame = games.some(g => {
              if (!g.startTime) return false;
              const start = new Date(g.startTime).getTime();
              const end = start + 4 * 60 * 60 * 1000; // games last ~4h
              return now >= start && now <= end;
            });
            const nextGame = games
              .filter(g => g.startTime && new Date(g.startTime).getTime() > now)
              .sort((a, b) => new Date(a.startTime!).getTime() - new Date(b.startTime!).getTime())[0];
            const nextStart = nextGame?.startTime ? new Date(nextGame.startTime) : null;
            const nextLabel = nextStart
              ? nextStart.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true, timeZone: 'America/Chicago' })
              : null;
            return (
              <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 2 }}>
                {hasLiveGame ? (
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '0.25rem 0.6rem', borderRadius: 9999, background: 'hsl(142 72% 46% / 0.12)', border: '1px solid hsl(142 72% 46% / 0.25)' }}>
                    <div className="live-dot" />
                    <span style={{ fontSize: '0.6rem', fontWeight: 700, letterSpacing: '0.07em', textTransform: 'uppercase', color: 'hsl(var(--g-green))' }}>Live</span>
                  </div>
                ) : nextLabel ? (
                  <div style={{ display: 'flex', alignItems: 'center', gap: 5, padding: '0.25rem 0.6rem', borderRadius: 9999, background: 'hsl(var(--g-surface-2))', border: '1px solid hsl(var(--g-border))' }}>
                    <span style={{ fontSize: '0.6rem', fontWeight: 600, letterSpacing: '0.06em', textTransform: 'uppercase', color: 'hsl(var(--muted-foreground))' }}>Next {nextLabel}</span>
                  </div>
                ) : null}
                {pulledAt && (
                  <div style={{ fontSize: '0.6rem', color: 'hsl(var(--muted-foreground))', letterSpacing: '0.04em' }}>
                    {formatRelative(pulledAt)}
                  </div>
                )}
              </div>
            );
          })()}
        </div>
      </header>

      {/* League tabs */}
      <div className="league-tabs">
        {LEAGUES.map(l => (
          <button
            key={l.id}
            className={`league-tab${league === l.id ? ' active' : ''}`}
            onClick={() => setLeague(l.id)}
            data-testid={`league-${l.id}`}
          >
            <img src={l.img} alt={l.label} />
            {l.label}
          </button>
        ))}
      </div>

      {/* Pull status bar */}
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: '0.5rem 1rem',
        background: 'hsl(var(--g-surface-2))',
        borderBottom: '1px solid hsl(var(--g-border))',
        gap: 10,
      }}>
        {/* Left: status info */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0 }}>
          {/* Row 1: live/cached badge + source */}
          <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
            {pullMutation.isPending ? (
              <span style={{
                fontSize: '0.62rem', fontWeight: 700, letterSpacing: '0.07em', textTransform: 'uppercase',
                color: 'hsl(var(--g-gold))', padding: '1px 6px', borderRadius: 3,
                border: '1px solid hsl(var(--g-gold) / 0.4)', background: 'hsl(var(--g-gold) / 0.08)',
              }}>PULLING</span>
            ) : isLive ? (
              <span style={{
                fontSize: '0.62rem', fontWeight: 700, letterSpacing: '0.07em', textTransform: 'uppercase',
                color: 'hsl(var(--g-green))', padding: '1px 6px', borderRadius: 3,
                border: '1px solid hsl(var(--g-green) / 0.4)', background: 'hsl(var(--g-green) / 0.08)',
              }}>LIVE</span>
            ) : isRateLimited ? (
              <span style={{
                fontSize: '0.62rem', fontWeight: 700, letterSpacing: '0.07em', textTransform: 'uppercase',
                color: 'hsl(42 80% 65%)', padding: '1px 6px', borderRadius: 3,
                border: '1px solid hsl(42 80% 65% / 0.4)', background: 'hsl(42 80% 65% / 0.08)',
              }}>CACHED</span>
            ) : fromCache || sourceName === 'Cache' ? (
              <span style={{
                fontSize: '0.62rem', fontWeight: 700, letterSpacing: '0.07em', textTransform: 'uppercase',
                color: 'hsl(var(--muted-foreground))', padding: '1px 6px', borderRadius: 3,
                border: '1px solid hsl(var(--g-border))',
              }}>CACHED</span>
            ) : (
              <span style={{
                fontSize: '0.62rem', fontWeight: 700, letterSpacing: '0.07em', textTransform: 'uppercase',
                color: 'hsl(var(--muted-foreground))', padding: '1px 6px', borderRadius: 3,
                border: '1px solid hsl(var(--g-border))',
              }}>READY</span>
            )}
            {sourceName && !pullMutation.isPending && (
              <span style={{ fontSize: '0.7rem', color: 'hsl(var(--muted-foreground))', letterSpacing: '0.02em' }}>
                {sourceName}
              </span>
            )}
          </div>
          {/* Row 2: timestamp or rate-limit info */}
          <span style={{ fontSize: '0.67rem', color: 'hsl(var(--muted-foreground) / 0.7)', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
            {pullMutation.isPending
              ? 'Contacting PrizePicks — then SportsGameOdds if needed…'
              : pullMutation.isError
              ? `Both sources unavailable — showing last cached slate`
              : isRateLimited && cooldownMin
              ? `Rate-limited — retry available in ${cooldownMin}m`
              : pulledAt
              ? `Last updated ${new Date(pulledAt).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', hour12: true, timeZone: 'America/Chicago' })}`
              : 'Press Pull to load live props'}
          </span>
        </div>

        {/* Right: Pull button */}
        <button
          onClick={() => pullMutation.mutate()}
          disabled={pullMutation.isPending}
          style={{
            display: 'flex', alignItems: 'center', gap: 5, flexShrink: 0,
            fontSize: '0.7rem', fontWeight: 700, letterSpacing: '0.06em', textTransform: 'uppercase',
            color: pullMutation.isPending ? 'hsl(var(--muted-foreground))' : 'hsl(var(--g-gold))',
            padding: '0.3rem 0.85rem', borderRadius: 5,
            border: `1px solid ${pullMutation.isPending ? 'hsl(var(--g-border))' : 'hsl(var(--g-gold) / 0.5)'}`,
            background: pullMutation.isPending ? 'transparent' : 'hsl(var(--g-gold) / 0.07)',
            transition: 'all 150ms', whiteSpace: 'nowrap', cursor: pullMutation.isPending ? 'not-allowed' : 'pointer',
          }}
          data-testid="pull-btn"
        >
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"
            style={pullMutation.isPending ? { animation: 'spin 0.7s linear infinite' } : undefined}>
            <path d="M21 12a9 9 0 1 1-6.219-8.56" />
          </svg>
          {pullMutation.isPending ? 'Pulling…' : 'Pull'}
        </button>
      </div>

      {/* Slate label */}
      <div className="section-title" style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <span>{league} Slate</span>
        {optimizedGames.length > 0 && (
          <span style={{ fontSize: '0.6875rem', fontWeight: 600, color: 'hsl(var(--g-gold))', fontFamily: 'Space Mono, monospace' }}>
            {optimizedGames.length} game{optimizedGames.length !== 1 ? 's' : ''}
          </span>
        )}
        {isOptimizing && (
          <span style={{ fontSize: '0.58rem', fontWeight: 800, letterSpacing: '0.08em', textTransform: 'uppercase', color: 'hsl(270 60% 70%)', background: 'hsl(270 60% 60% / 0.12)', border: '1px solid hsl(270 60% 60% / 0.3)', borderRadius: 4, padding: '1px 6px' }}>AI</span>
        )}
        {optimizerResult && !isOptimizing && (
          <span style={{ fontSize: '0.58rem', fontWeight: 800, letterSpacing: '0.08em', textTransform: 'uppercase', color: 'hsl(142 72% 50%)', background: 'hsl(142 72% 46% / 0.12)', border: '1px solid hsl(142 72% 46% / 0.3)', borderRadius: 4, padding: '1px 6px' }}>MATH</span>
        )}
      </div>

      {/* Content — never blank */}
      {isLoading ? (
        <div style={{ padding: '1rem' }}>
          {[1, 2, 3].map(i => (
            <div key={i} className="game-card" style={{ marginBottom: 12, height: 240 }}>
              <div className="shimmer" style={{ height: '100%', borderRadius: 8 }} />
            </div>
          ))}
        </div>
      ) : showEmpty ? (
        <SlateEmptyState
          reason={getFallbackReason()}
          onPull={() => pullMutation.mutate()}
          isPulling={pullMutation.isPending}
        />
      ) : (
        <div style={{ paddingBottom: '0.5rem' }}>
          {optimizedGames.map((game, i) => (
            <div key={game.gameId} style={{ animationDelay: `${i * 60}ms` }}>
              <GameCard
                game={game}
                selectedIds={selectedPropIds}
                onToggle={handleToggle}
                atMax={selectedPropIds.size >= MAX_PICKS}
                onSave={handleSave}
                isSaving={savingGameId === game.gameId}
              />
            </div>
          ))}
          <div style={{ height: selectedPropIds.size >= 2 ? '5.5rem' : '0.5rem' }} />
        </div>
      )}

      {/* ── Cross-game slip tray ── appears when ≥2 props are selected */}
      {selectedPropIds.size >= 2 && (
        <div
          style={{
            position: 'fixed',
            bottom: 'calc(env(safe-area-inset-bottom, 0px) + 4.25rem)', // sits above bottom nav
            left: 0, right: 0,
            zIndex: 90,
            padding: '0 12px 8px',
            pointerEvents: 'none',
          }}
        >
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              background: 'hsl(10 8% 7%)',
              border: '1px solid hsl(var(--g-gold) / 0.55)',
              borderRadius: 12,
              padding: '10px 14px',
              boxShadow: '0 -4px 24px hsl(0 0% 0% / 0.55), 0 0 0 1px hsl(var(--g-gold) / 0.08)',
              pointerEvents: 'all',
              gap: 12,
            }}
          >
            {/* Left: selected props summary */}
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontSize: '0.72rem', fontWeight: 800, color: 'hsl(var(--g-gold))', letterSpacing: '0.04em' }}>
                {selectedPropIds.size} Prop{selectedPropIds.size !== 1 ? 's' : ''} Selected
              </div>
              <div
                style={{
                  fontSize: '0.6rem',
                  color: 'hsl(var(--muted-foreground))',
                  whiteSpace: 'nowrap',
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                  marginTop: 2,
                }}
              >
                {Object.values(selectedPropMap)
                  .map((p: any) => p.playerName)
                  .join(' · ')}
              </div>
            </div>

            {/* Right: clear + save */}
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0 }}>
              {/* Clear button */}
              <button
                onClick={() => { setSelectedPropIds(new Set()); setSelectedPropMap({}); }}
                style={{
                  padding: '5px 10px',
                  borderRadius: 7,
                  border: '1px solid hsl(var(--g-border))',
                  background: 'transparent',
                  fontSize: '0.65rem',
                  fontWeight: 700,
                  color: 'hsl(var(--muted-foreground))',
                  cursor: 'pointer',
                  letterSpacing: '0.04em',
                }}
                data-testid="tray-clear"
              >
                Clear
              </button>

              {/* Save slip button */}
              <button
                onClick={handleSaveTray}
                disabled={savingTray}
                style={{
                  padding: '6px 14px',
                  borderRadius: 7,
                  border: '1px solid hsl(var(--g-gold) / 0.7)',
                  background: savingTray ? 'transparent' : 'hsl(var(--g-gold) / 0.12)',
                  fontSize: '0.68rem',
                  fontWeight: 800,
                  color: savingTray ? 'hsl(var(--muted-foreground))' : 'hsl(var(--g-gold))',
                  cursor: savingTray ? 'not-allowed' : 'pointer',
                  letterSpacing: '0.06em',
                  textTransform: 'uppercase',
                  display: 'flex',
                  alignItems: 'center',
                  gap: 5,
                  transition: 'all 150ms',
                }}
                data-testid="tray-save"
              >
                {savingTray ? (
                  <>
                    <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"
                      style={{ animation: 'spin 0.7s linear infinite' }}>
                      <path d="M21 12a9 9 0 1 1-6.219-8.56" />
                    </svg>
                    Saving…
                  </>
                ) : (
                  <>
                    <svg width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                      <path d="M5 12h14M12 5l7 7-7 7" />
                    </svg>
                    Save Slip
                  </>
                )}
              </button>
            </div>
          </div>
        </div>
      )}

      <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
    </div>
  );
}
