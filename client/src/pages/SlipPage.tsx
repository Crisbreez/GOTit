import { useQuery, useMutation } from '@tanstack/react-query';
import { apiRequest, queryClient } from '@/lib/queryClient';

interface SlipLeg {
  id: number; slipId: number;
  playerName: string; teamAbbr?: string;
  statType: string; lineScore: number; direction: string;
  isDemon?: boolean; isGoblin?: boolean;
  gameStartTime?: string;
  status: string; // pending | live | hit | miss | dnp
  actualValue?: number;
  propScore?: number;
}

interface Slip {
  id: number; league: string;
  gameMatchup?: string; gameStartTime?: string; scriptLabel?: string;
  status: string; // pending | live | settled_win | settled_loss
  qualityScore?: number; correlationScore?: number;
  createdAt: string; settledAt?: string;
  legs?: SlipLeg[];
}

function formatTime(iso?: string) {
  if (!iso) return 'TBD';
  try {
    return new Date(iso).toLocaleTimeString('en-US', { hour:'numeric', minute:'2-digit', timeZone:'America/Chicago', timeZoneName:'short' });
  } catch { return iso; }
}

function LegStatusIcon({ status }: { status: string }) {
  if (status === 'hit') return (
    <svg className="leg-status-icon" viewBox="0 0 24 24" fill="none" stroke="hsl(142 72% 46%)" strokeWidth="2.5">
      <polyline points="20 6 9 17 4 12"/>
    </svg>
  );
  if (status === 'miss') return (
    <svg className="leg-status-icon" viewBox="0 0 24 24" fill="none" stroke="hsl(0 72% 51%)" strokeWidth="2.5">
      <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
    </svg>
  );
  if (status === 'live') return (
    <div className="live-dot" style={{ flexShrink:0 }}/>
  );
  if (status === 'dnp') return (
    // Slash circle — voided
    <svg className="leg-status-icon" viewBox="0 0 24 24" fill="none" stroke="hsl(var(--muted-foreground))" strokeWidth="2">
      <circle cx="12" cy="12" r="10"/>
      <line x1="4.93" y1="4.93" x2="19.07" y2="19.07"/>
    </svg>
  );
  return (
    <svg className="leg-status-icon" viewBox="0 0 24 24" fill="none" stroke="hsl(var(--muted-foreground))" strokeWidth="1.8">
      <circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>
    </svg>
  );
}

function LegTypeTag({ isDemon, isGoblin }: { isDemon?: boolean; isGoblin?: boolean }) {
  if (isDemon) return <span className="badge badge-crimson" style={{ fontSize:'0.5625rem' }}>Demon</span>;
  if (isGoblin) return <span className="badge badge-goblin" style={{ fontSize:'0.5625rem' }}>Goblin</span>;
  return null;
}

function SlipCard({ slip, onRefresh, isRefreshing, onDelete, isDeleting, onMarkDnp }: { slip: Slip; onRefresh: (id:number) => void; isRefreshing: boolean; onDelete: (id:number) => void; isDeleting: boolean; onMarkDnp: (legId:number) => void }) {
  const allLegs = slip.legs || [];
  const activeLegs = allLegs.filter(l => l.status !== 'dnp');  // DNP legs excluded (PrizePicks voids them)
  const dnpLegs = allLegs.filter(l => l.status === 'dnp');
  const hitsCount = activeLegs.filter(l => l.status === 'hit').length;
  const totalLegs = activeLegs.length;
  const isLive = slip.status === 'live';
  const isPending = slip.status === 'pending';
  const hitPct = totalLegs > 0 ? (hitsCount / totalLegs) * 100 : 0;

  const isWin = slip.status === 'settled_win';
  const isLoss = slip.status === 'settled_loss';
  const isSettled = isWin || isLoss;

  const cardClass = `slip-card${isLive ? ' live' : ''}${isSettled ? ' settled' : ''}`;

  const statusTag = isWin ? (
    <span className="tag" style={{ background:'hsl(142 72% 46% / 0.18)', color:'hsl(142 72% 46%)', border:'1px solid hsl(142 72% 46% / 0.4)' }}>WIN</span>
  ) : isLoss ? (
    <span className="tag" style={{ background:'hsl(0 72% 51% / 0.18)', color:'hsl(0 72% 51%)', border:'1px solid hsl(0 72% 51% / 0.4)' }}>LOSS</span>
  ) : isLive ? (
    <span className="tag tag-live">
      <div className="live-dot" style={{ width:6, height:6 }}/>
      Live
    </span>
  ) : (
    <span className="tag tag-pending">Pending</span>
  );

  return (
    <div className={cardClass} data-testid={`slip-card-${slip.id}`}>
      {/* Slip header */}
      <div style={{ display:'flex', alignItems:'flex-start', justifyContent:'space-between', marginBottom:12 }}>
        <div>
          <div style={{ fontWeight:700, fontSize:'0.9375rem', lineHeight:1.2, marginBottom:4 }}>
            {slip.gameMatchup || slip.league + ' Slip'}
          </div>
          <div style={{ display:'flex', gap:6, flexWrap:'wrap', alignItems:'center' }}>
            {statusTag}
            {slip.scriptLabel && (
              <span style={{ fontSize:'0.6875rem', color:'hsl(var(--g-gold))', fontWeight:600 }}>
                {slip.scriptLabel}
              </span>
            )}
            <span style={{ fontSize:'0.6875rem', color:'hsl(var(--muted-foreground))', fontFamily:'Space Mono,monospace' }}>
              {formatTime(slip.gameStartTime)}
            </span>
          </div>
        </div>
        <div style={{ display:'flex', flexDirection:'column', alignItems:'flex-end', gap:4 }}>
          <div style={{ fontFamily:'Space Mono,monospace', fontSize:'0.75rem', fontWeight:700, color:'hsl(var(--g-gold))' }}>
            {hitsCount}/{totalLegs}
          </div>
          <div style={{ fontSize:'0.6875rem', color:'hsl(var(--muted-foreground))' }}>
            {totalLegs} legs{dnpLegs.length > 0 ? ` · ${dnpLegs.length} DNP` : ''}
          </div>
        </div>
      </div>

      {/* Progress bar */}
      {totalLegs > 0 && (
        <div className="progress-track" style={{ marginBottom:14 }}>
          <div className={hitPct === 100 ? 'progress-fill-green' : 'progress-fill-gold'} style={{ width:`${hitPct}%` }}/>
        </div>
      )}

      {/* Legs */}
      <div>
        {allLegs.map((leg) => {
          const isHit = leg.status === 'hit';
          const isMiss = leg.status === 'miss';
          const isLegLive = leg.status === 'live';
          const isDnp = leg.status === 'dnp';
          const hasActual = leg.actualValue != null;
          // Progress toward the line (capped 0–100%)
          const pct = hasActual && leg.lineScore > 0
            ? Math.min(100, Math.round((leg.actualValue! / leg.lineScore) * 100))
            : 0;
          const overTarget = leg.direction === 'over';
          const onTrack = overTarget ? (leg.actualValue ?? 0) >= leg.lineScore : (leg.actualValue ?? Infinity) <= leg.lineScore;
          const trackColor = isHit ? 'hsl(142 72% 46%)' : isMiss ? 'hsl(0 72% 51%)' : onTrack ? 'hsl(142 72% 46%)' : 'hsl(42 96% 56%)';
          return (
            <div key={leg.id} className={`leg-row${isHit ? ' hit' : isMiss ? ' miss' : isDnp ? ' dnp-voided' : ''}`}
              style={isDnp ? { opacity: 0.45 } : undefined}
            >
              <LegStatusIcon status={leg.status}/>
              <div style={{ flex:1, minWidth:0 }}>
                <div style={{ display:'flex', alignItems:'center', gap:6 }}>
                  <span className="leg-player" style={isDnp ? { textDecoration:'line-through' } : undefined}>
                    {leg.playerName}
                  </span>
                  <LegTypeTag isDemon={leg.isDemon} isGoblin={leg.isGoblin}/>
                  {isDnp && (
                    <span style={{
                      fontSize:'0.5rem', fontWeight:800, letterSpacing:'0.08em',
                      color:'hsl(var(--muted-foreground))',
                      background:'hsl(var(--g-border))',
                      borderRadius:3, padding:'1px 5px',
                    }}>DNP · VOIDED</span>
                  )}
                </div>
                <div className="leg-stat" style={isDnp ? { color:'hsl(var(--muted-foreground))' } : undefined}>
                  {leg.statType}
                  {isDnp && <span style={{ marginLeft:6, fontSize:'0.6rem' }}>— Leg removed from slip</span>}
                </div>
                {/* Live score progress bar — skip for DNP */}
                {!isDnp && (isLegLive || isHit || isMiss) && hasActual && (
                  <div style={{ marginTop:5 }}>
                    <div style={{ display:'flex', justifyContent:'space-between', alignItems:'baseline', marginBottom:3 }}>
                      <span style={{ fontSize:'0.6rem', fontWeight:700, fontFamily:'Space Mono,monospace', color: trackColor }}>
                        {leg.actualValue!.toFixed(1)} pts
                      </span>
                      <span style={{ fontSize:'0.55rem', color:'hsl(var(--muted-foreground))', fontFamily:'Space Mono,monospace' }}>
                        need {overTarget ? `>${leg.lineScore}` : `<${leg.lineScore}`}
                      </span>
                    </div>
                    <div style={{ height:3, background:'hsl(var(--g-border))', borderRadius:9999 }}>
                      <div style={{
                        height:'100%', borderRadius:9999,
                        width:`${pct}%`,
                        background: trackColor,
                        transition:'width 400ms ease',
                        maxWidth:'100%',
                      }}/>
                    </div>
                  </div>
                )}
              </div>
              <div style={{ textAlign:'right', flexShrink:0, display:'flex', flexDirection:'column', alignItems:'flex-end', gap:5 }}>
                {isDnp ? (
                  <div style={{ fontSize:'0.6rem', color:'hsl(var(--muted-foreground))', fontStyle:'italic' }}>voided</div>
                ) : (
                  <div className="leg-line" style={{
                    color: isHit ? 'hsl(var(--g-green))' : isMiss ? 'hsl(0 72% 60%)' : 'hsl(var(--foreground))'
                  }}>
                    {overTarget ? '↑' : '↓'} {leg.lineScore}
                  </div>
                )}
                {/* VOID / DNP button — pending or live legs only */}
                {(leg.status === 'pending' || leg.status === 'live') && (
                  <button
                    data-testid={`dnp-leg-${leg.id}`}
                    onClick={() => onMarkDnp(leg.id)}
                    title="Mark player as Did Not Play — voids this leg"
                    style={{
                      fontSize:'0.52rem', fontWeight:800, letterSpacing:'0.06em',
                      color:'hsl(var(--muted-foreground) / 0.5)',
                      background:'transparent',
                      border:'1px solid hsl(var(--g-border))',
                      borderRadius:4, padding:'2px 6px',
                      cursor:'pointer', lineHeight:1.4,
                    }}
                  >VOID</button>
                )}
              </div>
            </div>
          );
        })}
      </div>

      {/* Footer actions */}
      <div style={{ display:'flex', justifyContent:'space-between', alignItems:'center', gap:8, marginTop:14, paddingTop:12, borderTop:'1px solid hsl(var(--g-border))' }}>
        {/* Delete button (left) */}
        <button
          onClick={() => { if (window.confirm('Delete this slip? This cannot be undone.')) onDelete(slip.id); }}
          disabled={isDeleting}
          data-testid={`delete-slip-${slip.id}`}
          style={{
            display:'flex', alignItems:'center', gap:5,
            fontSize:'0.72rem', fontWeight:700,
            color: isDeleting ? 'hsl(var(--muted-foreground))' : 'hsl(0 72% 51%)',
            padding:'0.4rem 0.75rem',
            borderRadius:7,
            border:`1px solid ${isDeleting ? 'hsl(var(--g-border))' : 'hsl(0 72% 51% / 0.35)'}`,
            background: isDeleting ? 'transparent' : 'hsl(0 72% 51% / 0.06)',
            cursor: isDeleting ? 'not-allowed' : 'pointer',
            transition:'all 150ms',
          }}
        >
          {isDeleting ? (
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5"
              style={{ animation:'spin 0.7s linear infinite' }}>
              <path d="M21 12a9 9 0 1 1-6.219-8.56" />
            </svg>
          ) : (
            <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <polyline points="3 6 5 6 21 6"/>
              <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>
              <path d="M10 11v6M14 11v6"/>
              <path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/>
            </svg>
          )}
          {isDeleting ? 'Deleting…' : 'Delete'}
        </button>

        {/* Refresh button (right) */}
        <button
          className="btn-ghost"
          style={{ fontSize:'0.75rem', padding:'0.4rem 0.85rem' }}
          onClick={() => onRefresh(slip.id)}
          disabled={isRefreshing}
          data-testid={`refresh-slip-${slip.id}`}
        >
          {isRefreshing ? 'Refreshing…' : 'Refresh Slip'}
        </button>
      </div>
    </div>
  );
}

export default function SlipPage() {
  const { data: slips = [], isLoading } = useQuery<Slip[]>({
    queryKey: ['/api/slips', 'active'],
    queryFn: () => apiRequest('GET', '/api/slips?status=active').then(r => r.json()),
    refetchInterval: 60_000, // auto-refresh every 60s
    refetchIntervalInBackground: true,
  });

  const dnpLegMutation = useMutation({
    mutationFn: (legId: number) => apiRequest('PATCH', `/api/legs/${legId}`, { status: 'dnp', actualValue: null }).then(r => r.json()),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['/api/slips'] }),
  });

  const refreshMutation = useMutation({
    mutationFn: (id: number) => apiRequest('POST', `/api/slips/${id}/refresh`).then(r => r.json()),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['/api/slips', 'active'] }),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: number) => apiRequest('DELETE', `/api/slips/${id}`).then(r => r.json()),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['/api/slips', 'active'] });
      queryClient.invalidateQueries({ queryKey: ['/api/slips', 'all'] });
    },
  });

  const activeSlips = slips.filter(s => s.status === 'pending' || s.status === 'live');

  return (
    <div style={{ minHeight:'100dvh' }}>
      {/* Header */}
      <header className="app-header">
        <div style={{ display:'flex', alignItems:'center', justifyContent:'space-between' }}>
          <div>
            <div style={{ fontSize:'1.0625rem', fontWeight:800, letterSpacing:'-0.01em' }}>Active Slip</div>
            <div style={{ fontSize:'0.6875rem', color:'hsl(var(--muted-foreground))', letterSpacing:'0.06em', textTransform:'uppercase', fontWeight:600 }}>
              Live Tracking Queue
            </div>
          </div>
          {activeSlips.length > 0 && (
            <div style={{ fontFamily:'Space Mono,monospace', fontSize:'0.875rem', fontWeight:700, color:'hsl(var(--g-gold))' }}>
              {activeSlips.length} slip{activeSlips.length !== 1 ? 's' : ''}
            </div>
          )}
        </div>
      </header>

      {/* Content */}
      {isLoading ? (
        <div style={{ padding:'1rem' }}>
          {[1,2].map(i => (
            <div key={i} className="slip-card" style={{ marginBottom:12, height:200 }}>
              <div className="shimmer" style={{ height:'100%', borderRadius:8 }}/>
            </div>
          ))}
        </div>
      ) : activeSlips.length === 0 ? (
        <div className="empty-state">
          <div className="empty-icon">
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7">
              <path d="M9 5H7a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-2"/>
              <rect x="9" y="3" width="6" height="4" rx="1"/>
              <path d="M9 12h6M9 16h4"/>
            </svg>
          </div>
          <h3>No Active Slips</h3>
          <p>Build a GOTit slip from the Slate to start tracking legs here.</p>
        </div>
      ) : (
        <div style={{ paddingBottom:'0.5rem' }}>
          {/* Live slips first */}
          {activeSlips.filter(s => s.status === 'live').length > 0 && (
            <>
              <div className="section-title" style={{ display:'flex', alignItems:'center', gap:8 }}>
                <div className="live-dot"/>
                <span>Live Now</span>
              </div>
              {activeSlips.filter(s => s.status === 'live').map(slip => (
                <SlipCard key={slip.id} slip={slip} onRefresh={(id) => refreshMutation.mutate(id)} isRefreshing={refreshMutation.isPending} onDelete={(id) => deleteMutation.mutate(id)} isDeleting={deleteMutation.isPending && deleteMutation.variables === slip.id} onMarkDnp={(legId) => dnpLegMutation.mutate(legId)}/>
              ))}
            </>
          )}
          {/* Pending slips */}
          {activeSlips.filter(s => s.status === 'pending').length > 0 && (
            <>
              <div className="section-title">Pending</div>
              {activeSlips.filter(s => s.status === 'pending').map(slip => (
                <SlipCard key={slip.id} slip={slip} onRefresh={(id) => refreshMutation.mutate(id)} isRefreshing={refreshMutation.isPending} onDelete={(id) => deleteMutation.mutate(id)} isDeleting={deleteMutation.isPending && deleteMutation.variables === slip.id} onMarkDnp={(legId) => dnpLegMutation.mutate(legId)}/>
              ))}
            </>
          )}
        </div>
      )}
      <style>{`@keyframes spin { to { transform: rotate(360deg); } }`}</style>
    </div>
  );
}
