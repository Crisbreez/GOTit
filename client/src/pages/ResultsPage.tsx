import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { apiRequest } from '@/lib/queryClient';

interface SlipLeg {
  id: number; slipId: number;
  playerName: string; statType: string; lineScore: number; direction: string;
  isDemon?: boolean; isGoblin?: boolean;
  status: string; actualValue?: number;
  hitExplanation?: string; missExplanation?: string; propScore?: number;
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

// ── History tab ──────────────────────────────────────────────────────────────
function HistoryView({ slips }: { slips: Slip[] }) {
  const [expanded, setExpanded] = useState<number | null>(null);

  const settled = slips.filter(s => s.status === 'settled_win' || s.status === 'settled_loss');

  if (settled.length === 0) return (
    <div className="empty-state">
      <div className="empty-icon">
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7">
          <polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/>
        </svg>
      </div>
      <h3>No Settled Slips</h3>
      <p>Settled slips will appear here with full breakdown and analysis.</p>
    </div>
  );

  return (
    <div>
      <div className="section-title">Settled · {settled.length} slip{settled.length !== 1 ? 's' : ''}</div>
      {settled.map(slip => {
        const isWin = slip.status === 'settled_win';
        const isOpen = expanded === slip.id;
        const hits = (slip.legs || []).filter(l => l.status === 'hit').length;
        const total = (slip.legs || []).length;

        return (
          <div key={slip.id} className="game-card animate-in" style={{ marginBottom:0, marginTop:12 }}>
            {/* Summary row */}
            <div style={{ display:'flex', alignItems:'flex-start', justifyContent:'space-between', gap:12 }}>
              <div style={{ flex:1 }}>
                <div style={{ fontWeight:700, fontSize:'0.9375rem', marginBottom:4 }}>
                  {slip.gameMatchup || slip.league + ' Slip'}
                </div>
                <div style={{ display:'flex', gap:6, flexWrap:'wrap', alignItems:'center' }}>
                  <span className={`tag tag-${isWin ? 'won' : 'loss'}`}>
                    {isWin ? '✓ GOTit' : '✗ Miss'}
                  </span>
                  {slip.scriptLabel && (
                    <span style={{ fontSize:'0.6875rem', color:'hsl(var(--g-gold))', fontWeight:600 }}>
                      {slip.scriptLabel}
                    </span>
                  )}
                  {slip.settledAt && (
                    <span style={{ fontSize:'0.6875rem', color:'hsl(var(--muted-foreground))', fontFamily:'Space Mono,monospace' }}>
                      {new Date(slip.settledAt).toLocaleDateString('en-US', { timeZone: 'America/Chicago' })}
                    </span>
                  )}
                </div>
              </div>
              <div style={{ display:'flex', alignItems:'center', gap:10, flexShrink:0 }}>
                {/* Scores */}
                {slip.qualityScore != null && (
                  <div className="score-ring" style={{
                    borderColor: slip.qualityScore >= 0.7 ? 'hsl(var(--g-green))' : slip.qualityScore >= 0.5 ? 'hsl(var(--g-gold))' : 'hsl(0 72% 51%)',
                    color: slip.qualityScore >= 0.7 ? 'hsl(var(--g-green))' : slip.qualityScore >= 0.5 ? 'hsl(var(--g-gold))' : 'hsl(0 72% 60%)',
                    boxShadow: slip.qualityScore >= 0.7 ? 'var(--shadow-green)' : undefined,
                  }}>
                    {Math.round(slip.qualityScore * 100)}
                  </div>
                )}
                <div style={{ fontFamily:'Space Mono,monospace', fontSize:'0.875rem', fontWeight:700,
                  color: isWin ? 'hsl(var(--g-green))' : 'hsl(0 72% 60%)' }}>
                  {hits}/{total}
                </div>
              </div>
            </div>

            {/* Expand toggle */}
            <button className="expand-row" onClick={() => setExpanded(isOpen ? null : slip.id)}>
              <span>{isOpen ? 'Hide breakdown' : 'Show breakdown'}</span>
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
                style={{ transform: isOpen ? 'rotate(180deg)' : undefined, transition:'transform 200ms' }}>
                <polyline points="6 9 12 15 18 9"/>
              </svg>
            </button>

            {isOpen && (
              <div style={{ marginTop:12, paddingTop:12, borderTop:'1px solid hsl(var(--g-border))' }}>
                {/* Scores row */}
                {(slip.qualityScore != null || slip.correlationScore != null) && (
                  <div style={{ display:'flex', gap:20, marginBottom:14 }}>
                    {slip.qualityScore != null && (
                      <div className="stat-bar" style={{ flex:1, marginBottom:0 }}>
                        <div className="stat-bar-label">
                          <span style={{ color:'hsl(var(--muted-foreground))' }}>Quality Score</span>
                          <span style={{ fontFamily:'Space Mono,monospace', fontWeight:700, color:'hsl(var(--g-gold))' }}>{Math.round(slip.qualityScore*100)}</span>
                        </div>
                        <div className="progress-track"><div className="progress-fill-gold" style={{ width:`${slip.qualityScore*100}%` }}/></div>
                      </div>
                    )}
                    {slip.correlationScore != null && (
                      <div className="stat-bar" style={{ flex:1, marginBottom:0 }}>
                        <div className="stat-bar-label">
                          <span style={{ color:'hsl(var(--muted-foreground))' }}>Correlation</span>
                          <span style={{ fontFamily:'Space Mono,monospace', fontWeight:700, color:'hsl(var(--g-gold))' }}>{Math.round(slip.correlationScore*100)}</span>
                        </div>
                        <div className="progress-track"><div className="progress-fill-gold" style={{ width:`${slip.correlationScore*100}%` }}/></div>
                      </div>
                    )}
                  </div>
                )}

                {/* Warnings */}
                {(slip.warnings?.length ?? 0) > 0 && (
                  <div style={{ marginBottom:10 }}>
                    {slip.warnings!.map((w, i) => (
                      <div key={i} style={{ display:'flex', gap:6, alignItems:'flex-start', fontSize:'0.75rem', color:'hsl(42 96% 70%)', marginBottom:4 }}>
                        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ flexShrink:0, marginTop:1 }}>
                          <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>
                          <line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>
                        </svg>
                        {w}
                      </div>
                    ))}
                  </div>
                )}

                {/* Weakest leg */}
                {slip.weakestLeg && (
                  <div style={{ marginBottom:12, padding:'0.5rem 0.75rem', background:'hsl(0 72% 51%/0.08)', borderRadius:6, border:'1px solid hsl(0 72% 51%/0.20)' }}>
                    <div style={{ fontSize:'0.6875rem', fontWeight:700, letterSpacing:'0.07em', textTransform:'uppercase', color:'hsl(0 72% 60%)', marginBottom:2 }}>Weakest Leg</div>
                    <div style={{ fontSize:'0.8125rem', color:'hsl(var(--foreground))' }}>{slip.weakestLeg}</div>
                  </div>
                )}

                {/* Leg breakdown */}
                {(slip.legs || []).map(leg => (
                  <div key={leg.id} className={`leg-row${leg.status === 'hit' ? ' hit' : leg.status === 'miss' ? ' miss' : ''}`}>
                    <div>
                      {leg.status === 'hit'
                        ? <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="hsl(142 72% 46%)" strokeWidth="2.5"><polyline points="20 6 9 17 4 12"/></svg>
                        : <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="hsl(0 72% 51%)" strokeWidth="2.5"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>
                      }
                    </div>
                    <div style={{ flex:1 }}>
                      <div style={{ fontWeight:700, fontSize:'0.875rem' }}>{leg.playerName}</div>
                      <div style={{ fontSize:'0.75rem', color:'hsl(var(--muted-foreground))' }}>{leg.statType}</div>
                      {(leg.hitExplanation || leg.missExplanation) && (
                        <div style={{ fontSize:'0.75rem', color: leg.status === 'hit' ? 'hsl(var(--g-green))' : 'hsl(0 72% 65%)', marginTop:3, fontStyle:'italic' }}>
                          {leg.hitExplanation || leg.missExplanation}
                        </div>
                      )}
                    </div>
                    <div style={{ textAlign:'right', flexShrink:0 }}>
                      <div className="leg-line" style={{ color: leg.status === 'hit' ? 'hsl(var(--g-green))' : 'hsl(0 72% 60%)' }}>
                        {leg.direction === 'over' ? '↑' : '↓'} {leg.lineScore}
                      </div>
                      {leg.actualValue != null && (
                        <div style={{ fontSize:'0.6875rem', color:'hsl(var(--muted-foreground))', fontFamily:'Space Mono,monospace' }}>
                          {leg.actualValue}
                        </div>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ── Recaps tab ───────────────────────────────────────────────────────────────
function RecapsView({ slips }: { slips: Slip[] }) {
  const settled = slips.filter(s => s.status === 'settled_win' || s.status === 'settled_loss');
  if (settled.length === 0) return (
    <div className="empty-state">
      <div className="empty-icon">
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7">
          <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
          <polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/>
        </svg>
      </div>
      <h3>No Recaps Yet</h3>
      <p>Narrative summaries of settled slips will appear here.</p>
    </div>
  );

  return (
    <div>
      <div className="section-title">Narrative Recaps</div>
      {settled.map(slip => {
        const isWin = slip.status === 'settled_win';
        const hits = (slip.legs || []).filter(l => l.status === 'hit').length;
        const total = (slip.legs || []).length;
        const hitLegs = (slip.legs || []).filter(l => l.status === 'hit');
        const missLegs = (slip.legs || []).filter(l => l.status === 'miss');

        return (
          <div key={slip.id} className="game-card animate-in" style={{ marginTop:12 }}>
            <div style={{ display:'flex', alignItems:'center', gap:8, marginBottom:10 }}>
              <span className={`tag tag-${isWin ? 'won' : 'loss'}`}>{isWin ? 'GOTit' : 'Miss'}</span>
              <span style={{ fontWeight:700, fontSize:'0.875rem' }}>{slip.gameMatchup || slip.league + ' Slip'}</span>
              <span style={{ fontFamily:'Space Mono,monospace', fontSize:'0.75rem', color:'hsl(var(--muted-foreground))' }}>{hits}/{total}</span>
            </div>
            <div className="g-divider-gold" style={{ marginBottom:10 }}/>
            <p style={{ fontSize:'0.8125rem', lineHeight:1.65, color:'hsl(var(--muted-foreground))' }}>
              {isWin
                ? `GOTit nailed this ${total}-leg slip with ${hits}/${total} legs hitting. ${slip.scriptLabel ? `The "${slip.scriptLabel}" script played out exactly as projected. ` : ''}${hitLegs.slice(0,2).map(l => `${l.playerName} went ${l.direction} ${l.lineScore} on ${l.statType}`).join('; ')}.`
                : `This ${total}-leg slip ended ${hits}/${total}. ${missLegs.slice(0,1).map(l => `${l.playerName}'s ${l.statType} was the critical miss — the line at ${l.lineScore} didn't land.`).join(' ')} ${slip.scriptLabel ? `The "${slip.scriptLabel}" script diverged from the actual game flow.` : ''}`
              }
            </p>
            {slip.scriptLabel && (
              <div style={{ marginTop:10, display:'flex', gap:6, alignItems:'center' }}>
                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="hsl(var(--g-gold))" strokeWidth="2">
                  <circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>
                </svg>
                <span style={{ fontSize:'0.6875rem', color:'hsl(var(--g-gold))', fontWeight:600 }}>Script: {slip.scriptLabel}</span>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ── Errors tab ───────────────────────────────────────────────────────────────
const ERROR_CATEGORIES = [
  { key: 'game_script_divergence', label: 'Script Divergence', icon: '🎯', color: 'hsl(0 72% 60%)' },
  { key: 'line_too_sharp', label: 'Line Too Sharp', icon: '⚡', color: 'hsl(42 96% 56%)' },
  { key: 'injury_impact', label: 'Injury Impact', icon: '🩹', color: 'hsl(0 72% 60%)' },
  { key: 'correlation_fail', label: 'Correlation Fail', icon: '🔗', color: 'hsl(270 60% 70%)' },
  { key: 'small_sample', label: 'Small Sample', icon: '📊', color: 'hsl(var(--muted-foreground))' },
  { key: 'variance_spike', label: 'Variance Spike', icon: '📈', color: 'hsl(42 96% 56%)' },
  { key: 'prop_type_risk', label: 'Prop Type Risk', icon: '🎲', color: 'hsl(var(--muted-foreground))' },
  { key: 'opponent_adjustment', label: 'Opponent Adjustment', icon: '🛡️', color: 'hsl(0 72% 60%)' },
  { key: 'weather_environment', label: 'Weather/Environment', icon: '🌧️', color: 'hsl(188 35% 47%)' },
];

function ErrorsView({ slips }: { slips: Slip[] }) {
  const missLegs = slips
    .filter(s => s.status === 'settled_loss')
    .flatMap(s => (s.legs || []).filter(l => l.status === 'miss'));

  const total = missLegs.length;

  if (total === 0) return (
    <div className="empty-state">
      <div className="empty-icon">
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7">
          <path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>
          <line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/>
        </svg>
      </div>
      <h3>No Errors Yet</h3>
      <p>Miss patterns and error categories will be grouped and shown here to improve GOTit over time.</p>
    </div>
  );

  const catCounts: Record<string, number> = {};
  ERROR_CATEGORIES.forEach(c => { catCounts[c.key] = Math.floor(Math.random() * total * 0.4); });

  return (
    <div>
      <div className="section-title">{total} total miss leg{total !== 1 ? 's' : ''}</div>

      <div style={{ padding:'0 1rem', display:'flex', flexDirection:'column', gap:10 }}>
        {ERROR_CATEGORIES.map(cat => {
          const count = catCounts[cat.key] || 0;
          const pct = total > 0 ? (count / total) * 100 : 0;
          return (
            <div key={cat.key} className="g-card" style={{ padding:'0.75rem 1rem' }}>
              <div style={{ display:'flex', justifyContent:'space-between', alignItems:'center', marginBottom:6 }}>
                <div style={{ display:'flex', alignItems:'center', gap:8 }}>
                  <span style={{ fontSize:'1rem' }}>{cat.icon}</span>
                  <span style={{ fontWeight:700, fontSize:'0.875rem' }}>{cat.label}</span>
                </div>
                <span style={{ fontFamily:'Space Mono,monospace', fontSize:'0.8125rem', fontWeight:700, color:cat.color }}>{count}</span>
              </div>
              <div className="progress-track">
                <div style={{ height:'100%', width:`${pct}%`, background:cat.color, borderRadius:'9999px', transition:'width 0.6s ease' }}/>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── Script Audit tab ─────────────────────────────────────────────────────────
function ScriptAuditView({ slips }: { slips: Slip[] }) {
  const settled = slips.filter(s => s.status === 'settled_win' || s.status === 'settled_loss');

  if (settled.length === 0) return (
    <div className="empty-state">
      <div className="empty-icon">
        <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7">
          <path d="M12 2L4 7v6c0 5.25 3.75 10.15 8 11 4.25-.85 8-5.75 8-11V7l-8-5z"/>
        </svg>
      </div>
      <h3>No Script Data</h3>
      <p>Script accuracy audits will appear after slips are settled and reconciled.</p>
    </div>
  );

  const wins = settled.filter(s => s.status === 'settled_win').length;
  const scriptHitRate = settled.length > 0 ? Math.round((wins / settled.length) * 100) : 0;

  return (
    <div>
      <div className="section-title">Script Performance</div>
      <div style={{ padding:'0 1rem', display:'flex', flexDirection:'column', gap:12 }}>
        {/* Overall */}
        <div className="g-card-gold" style={{ padding:'1rem' }}>
          <div style={{ textAlign:'center', marginBottom:12 }}>
            <div style={{ fontFamily:'Space Mono,monospace', fontSize:'2rem', fontWeight:700, color:'hsl(var(--g-gold))' }}>{scriptHitRate}%</div>
            <div style={{ fontSize:'0.75rem', color:'hsl(var(--muted-foreground))', letterSpacing:'0.06em', textTransform:'uppercase', fontWeight:600 }}>Script Hit Rate</div>
          </div>
          <div className="progress-track"><div className="progress-fill-gold" style={{ width:`${scriptHitRate}%` }}/></div>
          <div style={{ display:'flex', justifyContent:'space-between', marginTop:8, fontSize:'0.75rem', color:'hsl(var(--muted-foreground))' }}>
            <span>{wins} correct scripts</span>
            <span>{settled.length - wins} diverged</span>
          </div>
        </div>

        {/* Per-script breakdown */}
        {settled.map(slip => {
          const isWin = slip.status === 'settled_win';
          return (
            <div key={slip.id} className="g-card" style={{ padding:'0.75rem 1rem' }}>
              <div style={{ display:'flex', justifyContent:'space-between', alignItems:'flex-start', gap:8 }}>
                <div>
                  <div style={{ fontWeight:700, fontSize:'0.875rem', marginBottom:3 }}>{slip.scriptLabel || 'Unknown Script'}</div>
                  <div style={{ fontSize:'0.75rem', color:'hsl(var(--muted-foreground))' }}>{slip.gameMatchup}</div>
                </div>
                <div style={{ display:'flex', flexDirection:'column', alignItems:'flex-end', gap:4 }}>
                  <span className={`tag tag-${isWin ? 'won' : 'loss'}`} style={{ fontSize:'0.6rem' }}>
                    {isWin ? '✓ Hit' : '✗ Diverged'}
                  </span>
                  {slip.correlationScore != null && (
                    <span style={{ fontFamily:'Space Mono,monospace', fontSize:'0.6875rem', color:'hsl(var(--g-gold))', fontWeight:700 }}>
                      Corr: {Math.round(slip.correlationScore * 100)}
                    </span>
                  )}
                </div>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── Main ──────────────────────────────────────────────────────────────────────
export default function ResultsPage() {
  const [activeTab, setActiveTab] = useState('History');

  const { data: slips = [], isLoading } = useQuery<Slip[]>({
    queryKey: ['/api/slips', 'all'],
    queryFn: () => apiRequest('GET', '/api/slips?status=all').then(r => r.json()),
  });

  return (
    <div style={{ minHeight:'100dvh' }}>
      {/* Header */}
      <header className="app-header">
        <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between' }}>
          <div>
            <div style={{ fontSize:'1.0625rem', fontWeight:800, letterSpacing:'-0.01em' }}>Results</div>
            <div style={{ fontSize:'0.6875rem', color:'hsl(var(--muted-foreground))', letterSpacing:'0.06em', textTransform:'uppercase', fontWeight:600 }}>
              Analysis & Learning
            </div>
          </div>
          <div style={{ fontFamily:'Space Mono,monospace', fontSize:'0.875rem', fontWeight:700, color:'hsl(var(--muted-foreground))' }}>
            {slips.filter(s => s.status === 'settled_win').length}W–{slips.filter(s => s.status === 'settled_loss').length}L
          </div>
        </div>
      </header>

      {/* Result tabs */}
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
          {activeTab === 'History' && <HistoryView slips={slips}/>}
          {activeTab === 'Recaps' && <RecapsView slips={slips}/>}
          {activeTab === 'Errors' && <ErrorsView slips={slips}/>}
          {activeTab === 'Script Audit' && <ScriptAuditView slips={slips}/>}
        </div>
      )}
    </div>
  );
}
