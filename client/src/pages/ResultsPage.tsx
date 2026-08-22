import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { apiRequest } from '@/lib/queryClient';
import { RefreshCw, CheckCircle, AlertCircle } from 'lucide-react';

interface SlipLeg {
  id: number; slipId: number;
  playerName: string; statType: string; lineScore: number; direction: string;
  isDemon?: boolean; isGoblin?: boolean; gameMatchup?: string;
  status: string; actualValue?: number;
  hitExplanation?: string; missExplanation?: string; propScore?: number;
  trackingError?: boolean;
}

interface Slip {
  id: number; league: string;
  gameMatchup?: string; scriptLabel?: string;
  status: string;
  qualityScore?: number; correlationScore?: number;
  createdAt: string; settledAt?: string;
  legs?: SlipLeg[];
  weakestLeg?: string;
  warnings?: string[];
}

const RESULT_TABS = ['History', 'Recaps', 'Errors', 'Script Audit'];

// ── helpers ───────────────────────────────────────────────────────────────────
function hitRate(legs: SlipLeg[]) {
  const active = legs.filter(l => l.status !== 'dnp' && l.status !== 'void');
  const hits   = active.filter(l => l.status === 'hit').length;
  return { hits, total: active.length, rate: active.length ? hits / active.length : 0 };
}

function slipDate(slip: Slip) {
  const iso = slip.settledAt || slip.createdAt;
  return new Date(iso).toLocaleDateString('en-US', { timeZone: 'America/Chicago', month: 'short', day: 'numeric' });
}

function legMargin(leg: SlipLeg): string {
  if (leg.actualValue == null) return '';
  const diff = leg.direction === 'over'
    ? leg.actualValue - leg.lineScore
    : leg.lineScore - leg.actualValue;
  return diff >= 0 ? `+${diff.toFixed(1)}` : diff.toFixed(1);
}

// ── History ───────────────────────────────────────────────────────────────────
function HistoryView({ slips }: { slips: Slip[] }) {
  const [expanded, setExpanded] = useState<number | null>(null);
  const settled = slips.filter(s => s.status === 'settled_win' || s.status === 'settled_loss');

  if (!settled.length) return (
    <div className="empty-state">
      <div className="empty-icon">
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7">
          <polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>
        </svg>
      </div>
      <h3>No Settled Slips</h3>
      <p>Settled slips will appear here once games finish.</p>
    </div>
  );

  return (
    <div style={{ padding:'0 1rem', paddingBottom:'1rem' }}>
      <div className="section-title">Settled · {settled.length} slip{settled.length !== 1 ? 's' : ''}</div>
      {settled.map(slip => {
        const legs = slip.legs || [];
        const isWin = slip.status === 'settled_win';
        const isOpen = expanded === slip.id;
        const { hits, total } = hitRate(legs);
        const rate = total ? hits / total : 0;
        const dnps = legs.filter(l => l.status === 'dnp' || l.status === 'void');
        const demons = legs.filter(l => l.isDemon);
        const missLegs = legs.filter(l => l.status === 'miss');
        const worstMiss = missLegs.sort((a, b) => {
          const da = a.actualValue != null ? Math.abs(a.actualValue - a.lineScore) : 0;
          const db = b.actualValue != null ? Math.abs(b.actualValue - b.lineScore) : 0;
          return da - db;
        })[0];

        return (
          <div key={slip.id} className="game-card animate-in" style={{ marginBottom:0, marginTop:12 }}>
            <div style={{ display:'flex', alignItems:'flex-start', justifyContent:'space-between', gap:12 }}>
              <div style={{ flex:1 }}>
                <div style={{ fontWeight:700, fontSize:'0.9375rem', marginBottom:4 }}>
                  {slip.gameMatchup || slip.league + ' Slip #' + slip.id}
                </div>
                <div style={{ display:'flex', gap:6, flexWrap:'wrap', alignItems:'center' }}>
                  <span className={`tag tag-${isWin ? 'won' : 'loss'}`}>
                    {isWin ? '✓ GOTit' : '✗ Miss'}
                  </span>
                  {slip.league && <span style={{ fontSize:'0.6rem', fontWeight:700, letterSpacing:'0.08em', color:'hsl(var(--muted-foreground))', textTransform:'uppercase' }}>{slip.league}</span>}
                  {dnps.length > 0 && <span style={{ fontSize:'0.65rem', color:'hsl(var(--muted-foreground))' }}>{dnps.length} DNP voided</span>}
                  <span style={{ fontSize:'0.6875rem', color:'hsl(var(--muted-foreground))', fontFamily:'Space Mono,monospace' }}>{slipDate(slip)}</span>
                </div>
              </div>
              <div style={{ display:'flex', flexDirection:'column', alignItems:'flex-end', gap:4, flexShrink:0 }}>
                <div style={{ fontFamily:'Space Mono,monospace', fontSize:'1rem', fontWeight:700,
                  color: isWin ? 'hsl(var(--g-green))' : 'hsl(0 72% 60%)' }}>
                  {hits}/{total}
                </div>
                <div style={{ fontSize:'0.65rem', color: rate >= 0.7 ? 'hsl(var(--g-green))' : rate >= 0.5 ? 'hsl(var(--g-gold))' : 'hsl(0 72% 60%)', fontWeight:700 }}>
                  {Math.round(rate * 100)}%
                </div>
              </div>
            </div>

            {/* Weakest miss callout */}
            {!isWin && worstMiss && (
              <div style={{ marginTop:8, padding:'0.4rem 0.6rem', background:'hsl(0 72% 51%/0.07)', borderRadius:5, border:'1px solid hsl(0 72% 51%/0.18)', fontSize:'0.72rem', color:'hsl(0 72% 65%)' }}>
                ↓ Missed: {worstMiss.playerName} {worstMiss.statType} {worstMiss.direction} {worstMiss.lineScore}
                {worstMiss.actualValue != null && ` (actual ${worstMiss.actualValue})`}
              </div>
            )}

            {demons.length > 0 && (
              <div style={{ marginTop:6, fontSize:'0.68rem', color:'hsl(var(--g-gold))', fontWeight:600 }}>
                🔥 {demons.length} demon leg{demons.length > 1 ? 's' : ''} included
              </div>
            )}

            <button className="expand-row" onClick={() => setExpanded(isOpen ? null : slip.id)}>
              <span>{isOpen ? 'Hide legs' : 'Show all legs'}</span>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
                style={{ transform: isOpen ? 'rotate(180deg)' : undefined, transition:'transform 200ms' }}>
                <polyline points="6 9 12 15 18 9"/>
              </svg>
            </button>

            {isOpen && (
              <div style={{ marginTop:12, paddingTop:12, borderTop:'1px solid hsl(var(--g-border))' }}>
                {legs.map(leg => {
                  const margin = legMargin(leg);
                  const isHit = leg.status === 'hit';
                  const isMiss = leg.status === 'miss';
                  const isDnp = leg.status === 'dnp' || leg.status === 'void';
                  return (
                    <div key={leg.id} className={`leg-row${isHit ? ' hit' : isMiss ? ' miss' : ''}`} style={{ opacity: isDnp ? 0.4 : 1 }}>
                      <div>
                        {isDnp
                          ? <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="hsl(var(--muted-foreground))" strokeWidth="2"><circle cx="12" cy="12" r="10"/><line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/></svg>
                          : isHit
                            ? <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="hsl(142 72% 46%)" strokeWidth="2.5"><polyline points="20 6 9 17 4 12"/></svg>
                            : <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="hsl(0 72% 51%)" strokeWidth="2.5"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
                        }
                      </div>
                      <div style={{ flex:1 }}>
                        <div style={{ fontWeight:700, fontSize:'0.8125rem' }}>
                          {leg.playerName}
                          {leg.isDemon && <span style={{ marginLeft:4, fontSize:'0.6rem', color:'hsl(var(--g-gold))', fontWeight:800 }}>DEMON</span>}
                          {leg.isGoblin && <span style={{ marginLeft:4, fontSize:'0.6rem', color:'hsl(270 60% 70%)', fontWeight:800 }}>GOBLIN</span>}
                        </div>
                        <div style={{ fontSize:'0.7rem', color:'hsl(var(--muted-foreground))' }}>{leg.statType}</div>
                      </div>
                      <div style={{ textAlign:'right', flexShrink:0 }}>
                        <div style={{ fontSize:'0.8rem', fontWeight:700, color: isHit ? 'hsl(var(--g-green))' : isMiss ? 'hsl(0 72% 60%)' : 'hsl(var(--muted-foreground))' }}>
                          {leg.direction === 'over' ? '↑' : '↓'} {leg.lineScore}
                        </div>
                        {leg.actualValue != null && (
                          <div style={{ fontSize:'0.65rem', color:'hsl(var(--muted-foreground))', fontFamily:'Space Mono,monospace' }}>
                            {leg.actualValue} <span style={{ color: isHit ? 'hsl(var(--g-green))' : 'hsl(0 72% 60%)' }}>{margin}</span>
                          </div>
                        )}
                        {isDnp && <div style={{ fontSize:'0.6rem', color:'hsl(var(--muted-foreground))' }}>DNP</div>}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ── Recaps ────────────────────────────────────────────────────────────────────
function RecapsView({ slips }: { slips: Slip[] }) {
  const settled = slips.filter(s => s.status === 'settled_win' || s.status === 'settled_loss');

  if (!settled.length) return (
    <div className="empty-state">
      <div className="empty-icon">
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
          <polyline points="14 2 14 8 20 8"/>
        </svg>
      </div>
      <h3>No Recaps Yet</h3>
      <p>Narrative summaries appear after slips settle.</p>
    </div>
  );

  return (
    <div style={{ padding:'0 1rem', paddingBottom:'1rem' }}>
      <div className="section-title">Narrative Recaps</div>
      {settled.map(slip => {
        const legs = slip.legs || [];
        const isWin = slip.status === 'settled_win';
        const { hits, total } = hitRate(legs);
        const hitLegs  = legs.filter(l => l.status === 'hit');
        const missLegs = legs.filter(l => l.status === 'miss');
        const dnpLegs  = legs.filter(l => l.status === 'dnp' || l.status === 'void');
        const demons   = legs.filter(l => l.isDemon);

        // Build narrative sentences
        const sentences: string[] = [];

        if (isWin) {
          sentences.push(`GOTit went ${hits}/${total} on this slip — a clean win.`);
          const topHit = hitLegs[0];
          if (topHit) sentences.push(`${topHit.playerName} led the way: ${topHit.statType} ${topHit.direction} ${topHit.lineScore}${topHit.actualValue != null ? `, actual ${topHit.actualValue}` : ''}.`);
          if (demons.length) sentences.push(`${demons.length} demon leg${demons.length > 1 ? 's' : ''} delivered.`);
        } else {
          sentences.push(`This slip finished ${hits}/${total}${dnpLegs.length ? ` (${dnpLegs.length} DNP voided)` : ''}.`);
          const firstMiss = missLegs[0];
          if (firstMiss) {
            const margin = firstMiss.actualValue != null
              ? ` — came in at ${firstMiss.actualValue} vs line of ${firstMiss.lineScore}`
              : '';
            sentences.push(`${firstMiss.playerName}'s ${firstMiss.statType} was the critical miss${margin}.`);
          }
          if (missLegs.length > 1) {
            sentences.push(`Also missed: ${missLegs.slice(1).map(l => l.playerName).join(', ')}.`);
          }
          if (hitLegs.length) {
            sentences.push(`${hitLegs.map(l => l.playerName).join(', ')} ${hitLegs.length === 1 ? 'hit' : 'all hit'}.`);
          }
        }

        return (
          <div key={slip.id} className="game-card animate-in" style={{ marginTop:12 }}>
            <div style={{ display:'flex', alignItems:'center', gap:8, marginBottom:8 }}>
              <span className={`tag tag-${isWin ? 'won' : 'loss'}`}>{isWin ? 'GOTit' : 'Miss'}</span>
              <span style={{ fontWeight:700, fontSize:'0.875rem', flex:1 }}>{slip.gameMatchup || slip.league + ' Slip #' + slip.id}</span>
              <span style={{ fontFamily:'Space Mono,monospace', fontSize:'0.75rem', color:'hsl(var(--muted-foreground))' }}>{hits}/{total}</span>
            </div>
            <div className="g-divider-gold" style={{ marginBottom:10 }}/>

            {/* Hit/miss bar */}
            <div style={{ display:'flex', gap:2, marginBottom:10, height:4, borderRadius:4, overflow:'hidden' }}>
              {legs.filter(l => l.status !== 'dnp' && l.status !== 'void').map(leg => (
                <div key={leg.id} style={{ flex:1, background: leg.status === 'hit' ? 'hsl(142 72% 46%)' : 'hsl(0 72% 51%)' }}/>
              ))}
            </div>

            <p style={{ fontSize:'0.8125rem', lineHeight:1.7, color:'hsl(var(--foreground)/0.85)', margin:0 }}>
              {sentences.join(' ')}
            </p>

            <div style={{ marginTop:8, fontSize:'0.68rem', color:'hsl(var(--muted-foreground))' }}>
              {slip.league} · {slipDate(slip)}
              {dnpLegs.length > 0 && ` · ${dnpLegs.map(l => l.playerName).join(', ')} DNP`}
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ── Errors ────────────────────────────────────────────────────────────────────
function ErrorsView({ slips }: { slips: Slip[] }) {
  const settled = slips.filter(s => s.status === 'settled_win' || s.status === 'settled_loss');
  const allLegs = settled.flatMap(s => (s.legs || []).map(l => ({ ...l, _slip: s })));
  const missLegs = allLegs.filter(l => l.status === 'miss');
  const dnpLegs  = allLegs.filter(l => l.status === 'dnp' || l.status === 'void');
  const trackingErrors = allLegs.filter(l => (l as any).trackingError);

  if (!missLegs.length && !dnpLegs.length) return (
    <div className="empty-state">
      <div className="empty-icon">
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7">
          <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>
          <line x1="12" y1="9" x2="12" y2="13"/>
        </svg>
      </div>
      <h3>No Errors Yet</h3>
      <p>Miss patterns and tracking errors will appear here.</p>
    </div>
  );

  // Group misses by stat type
  const byStatType: Record<string, typeof missLegs> = {};
  missLegs.forEach(l => {
    byStatType[l.statType] = byStatType[l.statType] || [];
    byStatType[l.statType].push(l);
  });
  const statRanking = Object.entries(byStatType).sort((a,b) => b[1].length - a[1].length);

  // Track which legs missed by a small margin (within 1 of line)
  const closeMisses = missLegs.filter(l => {
    if (l.actualValue == null) return false;
    return Math.abs(l.actualValue - l.lineScore) <= 1;
  });

  return (
    <div style={{ padding:'0 1rem', paddingBottom:'1rem' }}>
      {/* Summary row */}
      <div className="section-title">{missLegs.length} misses · {dnpLegs.length} DNP voided</div>

      {/* Tracking errors */}
      {trackingErrors.length > 0 && (
        <div className="g-card" style={{ padding:'0.75rem 1rem', marginBottom:10, border:'1px solid hsl(0 72% 51%/0.3)' }}>
          <div style={{ fontWeight:700, fontSize:'0.8rem', color:'hsl(0 72% 60%)', marginBottom:6 }}>⚠ Tracking Corrections ({trackingErrors.length})</div>
          {trackingErrors.slice(0,5).map(l => (
            <div key={l.id} style={{ fontSize:'0.72rem', color:'hsl(var(--muted-foreground))', marginBottom:3 }}>
              {l.playerName} · {l.statType} — corrected during tracking
            </div>
          ))}
        </div>
      )}

      {/* DNP legs */}
      {dnpLegs.length > 0 && (
        <div className="g-card" style={{ padding:'0.75rem 1rem', marginBottom:10 }}>
          <div style={{ fontWeight:700, fontSize:'0.8rem', color:'hsl(var(--muted-foreground))', marginBottom:6 }}>DNP / Voided Legs</div>
          {dnpLegs.map(l => (
            <div key={l.id} style={{ display:'flex', justifyContent:'space-between', fontSize:'0.75rem', marginBottom:4 }}>
              <span style={{ color:'hsl(var(--foreground))' }}>{l.playerName}</span>
              <span style={{ color:'hsl(var(--muted-foreground))' }}>{l.statType} · {(l as any)._slip?.league}</span>
            </div>
          ))}
        </div>
      )}

      {/* Close misses */}
      {closeMisses.length > 0 && (
        <div className="g-card" style={{ padding:'0.75rem 1rem', marginBottom:10, border:'1px solid hsl(42 96% 56%/0.25)' }}>
          <div style={{ fontWeight:700, fontSize:'0.8rem', color:'hsl(var(--g-gold))', marginBottom:6 }}>⚡ Close Misses ({closeMisses.length})</div>
          {closeMisses.map(l => (
            <div key={l.id} style={{ display:'flex', justifyContent:'space-between', fontSize:'0.75rem', marginBottom:4 }}>
              <span>{l.playerName} · {l.statType}</span>
              <span style={{ fontFamily:'Space Mono,monospace', color:'hsl(var(--g-gold))' }}>
                {l.actualValue} vs {l.lineScore}
              </span>
            </div>
          ))}
        </div>
      )}

      {/* Miss breakdown by stat type */}
      <div style={{ fontWeight:700, fontSize:'0.7rem', letterSpacing:'0.07em', textTransform:'uppercase', color:'hsl(var(--muted-foreground))', marginBottom:8, marginTop:4 }}>
        Misses by Stat Type
      </div>
      {statRanking.map(([stat, legs]) => {
        const pct = missLegs.length > 0 ? (legs.length / missLegs.length) * 100 : 0;
        return (
          <div key={stat} className="g-card" style={{ padding:'0.6rem 0.85rem', marginBottom:8 }}>
            <div style={{ display:'flex', justifyContent:'space-between', alignItems:'center', marginBottom:5 }}>
              <span style={{ fontWeight:700, fontSize:'0.8125rem' }}>{stat}</span>
              <span style={{ fontFamily:'Space Mono,monospace', fontSize:'0.8rem', fontWeight:700, color:'hsl(0 72% 60%)' }}>{legs.length}×</span>
            </div>
            <div className="progress-track">
              <div style={{ height:'100%', width:`${pct}%`, background:'hsl(0 72% 51%)', borderRadius:'9999px', transition:'width 0.5s ease' }}/>
            </div>
            <div style={{ marginTop:5, fontSize:'0.68rem', color:'hsl(var(--muted-foreground))' }}>
              {legs.slice(0,3).map(l => l.playerName).join(', ')}{legs.length > 3 ? ` +${legs.length-3}` : ''}
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ── Script Audit ──────────────────────────────────────────────────────────────
function ScriptAuditView({ slips }: { slips: Slip[] }) {
  const settled = slips.filter(s => s.status === 'settled_win' || s.status === 'settled_loss');

  if (!settled.length) return (
    <div className="empty-state">
      <div className="empty-icon">
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7">
          <path d="M12 2L4 7v6c0 5.25 3.75 10.15 8 11 4.25-.85 8-5.75 8-11V7l-8-5z"/>
        </svg>
      </div>
      <h3>No Script Data</h3>
      <p>Script accuracy audits appear after slips settle.</p>
    </div>
  );

  const wins     = settled.filter(s => s.status === 'settled_win').length;
  const allLegs  = settled.flatMap(s => s.legs || []);
  const active   = allLegs.filter(l => l.status !== 'dnp' && l.status !== 'void');
  const legHits  = active.filter(l => l.status === 'hit').length;
  const slipRate = settled.length ? Math.round((wins / settled.length) * 100) : 0;
  const legRate  = active.length  ? Math.round((legHits / active.length) * 100) : 0;

  const demonLegs  = allLegs.filter(l => l.isDemon);
  const goblinLegs = allLegs.filter(l => l.isGoblin);
  const demonHits  = demonLegs.filter(l => l.status === 'hit').length;
  const goblinHits = goblinLegs.filter(l => l.status === 'hit').length;
  const demonRate  = demonLegs.length  ? Math.round((demonHits / demonLegs.length) * 100) : null;
  const goblinRate = goblinLegs.length ? Math.round((goblinHits / goblinLegs.length) * 100) : null;

  return (
    <div style={{ padding:'0 1rem', paddingBottom:'1rem' }}>
      <div className="section-title">Script Performance · {settled.length} slips</div>

      {/* Top-level KPIs */}
      <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap:10, marginBottom:14 }}>
        {[
          { label: 'Slip Hit Rate', value: `${slipRate}%`, sub: `${wins}W–${settled.length - wins}L`, color: slipRate >= 50 ? 'hsl(var(--g-green))' : 'hsl(0 72% 60%)' },
          { label: 'Leg Hit Rate',  value: `${legRate}%`,  sub: `${legHits}/${active.length} legs`, color: legRate >= 55 ? 'hsl(var(--g-green))' : legRate >= 45 ? 'hsl(var(--g-gold))' : 'hsl(0 72% 60%)' },
        ].map(kpi => (
          <div key={kpi.label} className="g-card-gold" style={{ padding:'0.875rem', textAlign:'center' }}>
            <div style={{ fontFamily:'Space Mono,monospace', fontSize:'1.5rem', fontWeight:700, color:kpi.color }}>{kpi.value}</div>
            <div style={{ fontSize:'0.65rem', fontWeight:700, letterSpacing:'0.07em', textTransform:'uppercase', color:'hsl(var(--muted-foreground))', marginTop:2 }}>{kpi.label}</div>
            <div style={{ fontSize:'0.68rem', color:'hsl(var(--muted-foreground))', marginTop:2 }}>{kpi.sub}</div>
          </div>
        ))}
      </div>

      {/* Demon / Goblin rates */}
      {(demonRate !== null || goblinRate !== null) && (
        <div style={{ display:'grid', gridTemplateColumns:'1fr 1fr', gap:10, marginBottom:14 }}>
          {demonRate !== null && (
            <div className="g-card" style={{ padding:'0.75rem', textAlign:'center' }}>
              <div style={{ fontFamily:'Space Mono,monospace', fontSize:'1.1rem', fontWeight:700, color:'hsl(var(--g-gold))' }}>{demonRate}%</div>
              <div style={{ fontSize:'0.65rem', fontWeight:700, letterSpacing:'0.06em', textTransform:'uppercase', color:'hsl(var(--muted-foreground))' }}>Demon Hit Rate</div>
              <div style={{ fontSize:'0.68rem', color:'hsl(var(--muted-foreground))' }}>{demonHits}/{demonLegs.length}</div>
            </div>
          )}
          {goblinRate !== null && (
            <div className="g-card" style={{ padding:'0.75rem', textAlign:'center' }}>
              <div style={{ fontFamily:'Space Mono,monospace', fontSize:'1.1rem', fontWeight:700, color:'hsl(270 60% 70%)' }}>{goblinRate}%</div>
              <div style={{ fontSize:'0.65rem', fontWeight:700, letterSpacing:'0.06em', textTransform:'uppercase', color:'hsl(var(--muted-foreground))' }}>Goblin Hit Rate</div>
              <div style={{ fontSize:'0.68rem', color:'hsl(var(--muted-foreground))' }}>{goblinHits}/{goblinLegs.length}</div>
            </div>
          )}
        </div>
      )}

      {/* Script tag breakdown — SUPPORT vs BLIND miss analysis */}
      {(() => {
        const missLegs = allLegs.filter(l => l.status === 'miss');
        const byTag: Record<string, {hit:number; miss:number}> = {};
        for (const l of allLegs.filter(l => l.status !== 'dnp' && l.status !== 'void')) {
          const tag = (l as any).missTag || ((l.status === 'hit') ? 'hit' : 'untagged');
          const key = l.status === 'hit' ? 'hit' : tag;
          if (!byTag[key]) byTag[key] = {hit:0, miss:0};
          l.status === 'hit' ? byTag[key].hit++ : byTag[key].miss++;
        }
        const tagRows = Object.entries(byTag).filter(([k]) => k !== 'hit');
        if (!tagRows.length) return null;
        return (
          <div style={{ marginBottom:14 }}>
            <div style={{ fontWeight:700, fontSize:'0.65rem', letterSpacing:'0.07em', textTransform:'uppercase', color:'hsl(var(--muted-foreground))', marginBottom:6 }}>Miss Breakdown</div>
            <div style={{ display:'flex', flexWrap:'wrap', gap:6 }}>
              {tagRows.map(([tag, {miss}]) => {
                const color = tag === 'price_wrong' ? 'hsl(35 90% 55%)' : tag === 'script_wrong' ? 'hsl(0 72% 60%)' : 'hsl(220 60% 65%)';
                const label = tag === 'price_wrong' ? '⚡ Price Wrong' : tag === 'script_wrong' ? '✗ Script Wrong' : tag === 'variance' ? '〜 Variance' : tag;
                return <span key={tag} style={{ fontSize:'0.68rem', fontWeight:700, color, background:'hsl(var(--card))', border:`1px solid ${color}`, padding:'2px 7px', borderRadius:4 }}>{label}: {miss}</span>;
              })}
            </div>
          </div>
        );
      })()}

      {/* Per-slip breakdown */}
      <div style={{ fontWeight:700, fontSize:'0.7rem', letterSpacing:'0.07em', textTransform:'uppercase', color:'hsl(var(--muted-foreground))', marginBottom:8 }}>
        Slip Breakdown
      </div>
      {settled.map(slip => {
        const legs    = slip.legs || [];
        const { hits, total } = hitRate(legs);
        const isWin   = slip.status === 'settled_win';
        const rate    = total ? hits / total : 0;
        const missL   = legs.filter(l => l.status === 'miss');
        return (
          <div key={slip.id} className="g-card" style={{ padding:'0.75rem 1rem', marginBottom:8 }}>
            <div style={{ display:'flex', justifyContent:'space-between', alignItems:'flex-start', gap:8, marginBottom:6 }}>
              <div>
                <div style={{ fontWeight:700, fontSize:'0.8125rem', marginBottom:2 }}>
                  {slip.gameMatchup || slip.league + ' Slip #' + slip.id}
                </div>
                <div style={{ fontSize:'0.68rem', color:'hsl(var(--muted-foreground))' }}>{slipDate(slip)} · {slip.league}</div>
              </div>
              <div style={{ display:'flex', flexDirection:'column', alignItems:'flex-end', gap:3 }}>
                <span className={`tag tag-${isWin ? 'won' : 'loss'}`} style={{ fontSize:'0.6rem' }}>
                  {isWin ? '✓ GOTit' : '✗ Miss'}
                </span>
                <span style={{ fontFamily:'Space Mono,monospace', fontSize:'0.75rem', fontWeight:700,
                  color: rate >= 0.7 ? 'hsl(var(--g-green))' : rate >= 0.5 ? 'hsl(var(--g-gold))' : 'hsl(0 72% 60%)' }}>
                  {hits}/{total}
                </span>
              </div>
            </div>

            {/* Mini leg bar */}
            <div style={{ display:'flex', gap:2, height:3, borderRadius:2, overflow:'hidden', marginBottom: missL.length ? 6 : 0 }}>
              {legs.filter(l => l.status !== 'dnp' && l.status !== 'void').map(l => (
                <div key={l.id} style={{ flex:1, background: l.status === 'hit' ? 'hsl(142 72% 46%)' : 'hsl(0 72% 51%)' }}/>
              ))}
            </div>

            {missL.length > 0 && (
              <div style={{ fontSize:'0.68rem', color:'hsl(0 72% 65%)', marginTop:4 }}>
                {missL.map(l => {
                  const tag = (l as any).missTag;
                  const tagColor = tag === 'price_wrong' ? 'hsl(35 90% 55%)'
                                 : tag === 'script_wrong' ? 'hsl(0 72% 60%)'
                                 : tag === 'variance'     ? 'hsl(220 60% 65%)'
                                 : 'hsl(var(--muted-foreground))';
                  const tagLabel = tag === 'price_wrong' ? '⚡ price' : tag === 'script_wrong' ? '✗ script' : tag === 'variance' ? '〜 variance' : null;
                  return (
                    <div key={l.id} style={{ display:'flex', alignItems:'center', gap:4, marginBottom:2 }}>
                      <span>{l.playerName} {l.statType}{l.actualValue != null ? ` · ${l.actualValue}` : ''}</span>
                      {tagLabel && <span style={{ fontSize:'0.6rem', fontWeight:700, color: tagColor, background:'hsl(var(--card))', padding:'1px 5px', borderRadius:3 }}>{tagLabel}</span>}
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ── Main ──────────────────────────────────────────────────────────────────────
export default function ResultsPage() {
  const [activeTab, setActiveTab] = useState('History');
  const [autoSettleMsg, setAutoSettleMsg] = useState<string | null>(null);
  const qc = useQueryClient();

  const { data: slips = [], isLoading } = useQuery<Slip[]>({
    queryKey: ['/api/slips', 'all'],
    queryFn: () => apiRequest('GET', '/api/slips?status=all').then(r => r.json()),
  });

  const autoSettleMut = useMutation({
    mutationFn: () => apiRequest('POST', '/api/settle/auto', { league: 'MLB' }).then(r => r.json()),
    onSuccess: (data: any) => {
      qc.invalidateQueries({ queryKey: ['/api/slips'] });
      setAutoSettleMsg(data.message ?? `Settled ${data.settled} legs`);
      setTimeout(() => setAutoSettleMsg(null), 5000);
    },
    onError: () => {
      setAutoSettleMsg('Auto-settle failed — check connection');
      setTimeout(() => setAutoSettleMsg(null), 4000);
    },
  });

  const pending = slips.filter(s => s.status === 'pending' || s.status === 'live');
  const settled = slips.filter(s => s.status === 'settled_win' || s.status === 'settled_loss');
  const wins    = settled.filter(s => s.status === 'settled_win').length;

  return (
    <div style={{ minHeight:'100dvh' }}>
      <header className="app-header">
        <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between' }}>
          <div>
            <div style={{ fontSize:'1.0625rem', fontWeight:800, letterSpacing:'-0.01em' }}>Results</div>
            <div style={{ fontSize:'0.6875rem', color:'hsl(var(--muted-foreground))', letterSpacing:'0.06em', textTransform:'uppercase', fontWeight:600 }}>
              Analysis & Learning
            </div>
          </div>
          <div style={{ display:'flex', alignItems:'center', gap:'0.5rem' }}>
            <div style={{ fontFamily:'Space Mono,monospace', fontSize:'0.875rem', fontWeight:700, color:'hsl(var(--muted-foreground))' }}>
              {wins}W–{settled.length - wins}L
            </div>
            {pending.length > 0 && (
              <button
                data-testid="btn-auto-settle"
                onClick={() => autoSettleMut.mutate()}
                disabled={autoSettleMut.isPending}
                style={{
                  display:'flex', alignItems:'center', gap:'0.3rem',
                  padding:'0.3rem 0.65rem', borderRadius:6,
                  background:'hsl(var(--accent))', color:'hsl(var(--accent-foreground))',
                  border:'none', fontSize:'0.7rem', fontWeight:700,
                  letterSpacing:'0.05em', textTransform:'uppercase', cursor:'pointer',
                  opacity: autoSettleMut.isPending ? 0.6 : 1,
                }}
              >
                <RefreshCw size={11} style={{ animation: autoSettleMut.isPending ? 'spin 1s linear infinite' : 'none' }} />
                {autoSettleMut.isPending ? 'Settling...' : `Settle (${pending.length})`}
              </button>
            )}
          </div>
        </div>
        {autoSettleMsg && (
          <div style={{
            marginTop:'0.4rem', fontSize:'0.7rem', fontWeight:600,
            color: autoSettleMsg.includes('failed') ? 'hsl(var(--destructive))' : 'hsl(142 72% 50%)',
            display:'flex', alignItems:'center', gap:'0.3rem',
          }}>
            {autoSettleMsg.includes('failed')
              ? <AlertCircle size={11} />
              : <CheckCircle size={11} />}
            {autoSettleMsg}
          </div>
        )}
      </header>

      <div className="result-tabs">
        {RESULT_TABS.map(tab => (
          <button
            key={tab}
            className={`result-tab${activeTab === tab ? ' active' : ''}`}
            onClick={() => setActiveTab(tab)}
            data-testid={`result-tab-${tab.toLowerCase().replace(' ', '-')}`}
          >{tab}</button>
        ))}
      </div>
      <div className="g-divider" style={{ marginTop:0 }}/>

      {isLoading ? (
        <div style={{ padding:'1rem' }}>
          {[1,2,3].map(i => (
            <div key={i} className="g-card" style={{ height:80, marginBottom:10 }}>
              <div className="shimmer" style={{ height:'100%', borderRadius:8 }}/>
            </div>
          ))}
        </div>
      ) : (
        <div style={{ paddingBottom:'0.5rem' }}>
          {activeTab === 'History'      && <HistoryView     slips={slips}/>}
          {activeTab === 'Recaps'       && <RecapsView       slips={slips}/>}
          {activeTab === 'Errors'       && <ErrorsView       slips={slips}/>}
          {activeTab === 'Script Audit' && <ScriptAuditView slips={slips}/>}
        </div>
      )}
    </div>
  );
}
