/**
 * GOTit Storage — Supabase via direct PostgREST HTTP
 *
 * Uses a lightweight fetch-based client (server/supabase.ts) instead of
 * supabase-js to avoid the WebSocket/realtime crash on Node 20.
 *
 * Data persists in Supabase Postgres — survives every redeploy forever.
 */

import db from './supabase';
import { centralTodayStartUTC } from './time';
import type { Slip, SlipLeg, InsertSlip, InsertLeg } from '@shared/schema';

// ── snake_case → camelCase mappers ────────────────────────────────────────────
function mapProp(r: any) {
  if (!r) return r;
  return {
    id: r.id,
    league: r.league,
    playerName: r.player_name,
    teamAbbr: r.team_abbr,
    statType: r.stat_type,
    lineScore: r.line_score,
    direction: r.direction,
    isDemon: r.is_demon,
    isGoblin: r.is_goblin,
    isSynthetic: r.is_synthetic ?? false,
    gameId: r.game_id,
    gameMatchup: r.game_matchup,
    gameStartTime: r.game_start_time,
    confidenceLevel: r.confidence_level,
    propScore: r.prop_score,
    rejectReason: r.reject_reason,
    scriptLabel: r.script_label,
    pulledAt: r.pulled_at,
    source: r.source,
    ppDisplayMatchup: r.pp_display_matchup,
    ppDisplayPlayer: r.pp_display_player,
    ppDisplayStat: r.pp_display_stat,
    ppDisplayTeam: r.pp_display_team,
    ppEventTitle: r.pp_event_title,
  };
}

function mapSlip(r: any): Slip {
  if (!r) return r;
  return {
    id: r.id,
    league: r.league,
    gameMatchup: r.game_matchup,
    gameStartTime: r.game_start_time,
    scriptLabel: r.script_label,
    status: r.status,
    qualityScore: r.quality_score,
    correlationScore: r.correlation_score,
    createdAt: r.created_at,
    settledAt: r.settled_at,
  } as Slip;
}

function mapLeg(r: any): SlipLeg {
  if (!r) return r;
  return {
    id: r.id,
    slipId: r.slip_id,
    propId: r.prop_id,
    playerName: r.player_name,
    teamAbbr: r.team_abbr,
    statType: r.stat_type,
    lineScore: r.line_score,
    direction: r.direction,
    isDemon: r.is_demon,
    isGoblin: r.is_goblin,
    gameMatchup: r.game_matchup,
    gameStartTime: r.game_start_time,
    status: r.status,
    actualValue: r.actual_value,
    hitExplanation: r.hit_explanation,
    missExplanation: r.miss_explanation,
    propScore: r.prop_score,
  } as SlipLeg;
}

export interface IStorage {
  upsertProps(rows: any[]): Promise<void>;
  getProps(league: string): Promise<any[]>;
  deletePropsForLeague(league: string): Promise<void>;
  createSlip(data: InsertSlip): Promise<Slip>;
  getSlips(statusFilter?: string[]): Promise<any[]>;
  getSlipById(id: number): Promise<Slip | undefined>;
  updateSlipStatus(id: number, status: string, extra?: Partial<Slip>): Promise<void>;
  deleteSlip(id: number): Promise<void>;
  createLegs(legs: InsertLeg[]): Promise<void>;
  getLegsBySlip(slipId: number): Promise<SlipLeg[]>;
  updateLegStatus(id: number, status: string, actualValue?: number): Promise<void>;
  reconcileLeg(id: number, status: string, actualValue: number | null, lastCheckedAt: string, trackingError: boolean): Promise<void>;
  logPull(league: string, count: number, status?: string): Promise<void>;
  getLastPull(league: string): Promise<any>;
  updatePlayerPerformance(playerName: string, statType: string, league: string, outcome: 'hit' | 'miss', actualValue: number | null, line: number): Promise<void>;
  getPlayerPerformance(playerName: string, statType: string, league: string): Promise<{ hitCount: number; missCount: number; last5: string[]; avgMargin: number | null } | null>;
  getAllPerformance(): Promise<any[]>;
}

export const storage: IStorage = {

  // ── Props ──────────────────────────────────────────────────────────────────
  async upsertProps(rows: any[]) {
    if (rows.length === 0) return;

    for (const r of rows) {
      if (r.gameStartTime && !r.gameStartTime.endsWith('Z')) {
        try { r.gameStartTime = new Date(r.gameStartTime).toISOString(); } catch (_) {}
      }
    }

    const league = rows[0].league;
    // Stamp every prop in this pull with the same pulledAt timestamp.
    // After writing succeeds, delete any props with an older timestamp.
    // Write-first, delete-second: slate is never left empty if write fails.
    const pullTimestamp = new Date().toISOString();
    rows.forEach(r => { r.pulledAt = pullTimestamp; });

    // ── Dedup pass 1: exact duplicate (same player+stat+line+direction+game)
    // PP occasionally sends the same projection twice with different IDs.
    const exactMap = new Map<string, typeof rows[0]>();
    for (const r of rows) {
      const key = `${r.playerName}|${r.statType}|${r.lineScore}|${r.direction}|${r.gameId}`;
      const existing = exactMap.get(key);
      if (!existing) {
        exactMap.set(key, r);
      } else {
        // Prefer standard > goblin > demon for canonical line
        const tierRank = (p: typeof r) => p.isDemon ? 0 : p.isGoblin ? 1 : 2;
        if (tierRank(r) > tierRank(existing)) exactMap.set(key, r);
      }
    }

    // ── Dedup pass 2: alt-line collapse (same player+stat+direction+game, different lines)
    // PP is an alt-line platform — each player+stat has 6-7 lines (0.5, 1.5 ... 7.5).
    // Keep ONE line per player+stat+direction+game:
    //   • The standard/goblin line is the canonical PP line — always preferred.
    //   • If no standard/goblin exists, keep the demon line closest to the median
    //     (middle of the range = most liquid alt-line).
    const altMap = new Map<string, typeof rows[0]>();
    for (const r of Array.from(exactMap.values())) {
      const key = `${r.playerName}|${r.statType}|${r.direction}|${r.gameId}`;
      const existing = altMap.get(key);
      if (!existing) {
        altMap.set(key, r);
      } else {
        const isStandard = (p: typeof r) => !p.isDemon && !p.isGoblin;
        const isGoblin   = (p: typeof r) => !!p.isGoblin;
        // Tier priority: standard > goblin > demon
        // Among same tier: standard/goblin keep lower line; demon keeps higher line
        if (isStandard(r) && !isStandard(existing)) {
          altMap.set(key, r);
        } else if (isGoblin(r) && !isStandard(existing) && !isGoblin(existing)) {
          altMap.set(key, r);
        } else if (isStandard(r) && isStandard(existing)) {
          if (r.lineScore < existing.lineScore) altMap.set(key, r);
        } else if (isGoblin(r) && isGoblin(existing)) {
          if (r.lineScore < existing.lineScore) altMap.set(key, r);
        } else if (r.isDemon && existing.isDemon) {
          // Keep the LOWEST demon line — most achievable.
          // High demon lines (e.g. H+R+RBI 6.5) are near-impossible traps.
          if (r.lineScore < existing.lineScore) altMap.set(key, r);
        }
      }
    }

    const deduped = Array.from(altMap.values());
    console.log(`[storage] upsertProps: ${rows.length} raw → ${deduped.length} after dedup (pass1=${exactMap.size})`);
    rows = deduped;

    // ── Line movement tracking ────────────────────────────────────────────────
    // For each prop, check if it already exists in DB. If it does:
    //   - preserve firstSeenLine and firstSeenAt
    //   - increment lineMoveCount if line changed
    // If it's new: set firstSeenLine = lineScore, firstSeenAt = now
    const now = new Date().toISOString();
    const existingIds = rows.map(r => r.id);
    let existingMap: Map<string, any> = new Map();
    if (existingIds.length > 0) {
      try {
        const { data: existingRows } = await db
          .from('props')
          .select('id, first_seen_line, first_seen_at, line_score, line_move_count')
          .in('id', existingIds.slice(0, 500));
        for (const ex of (existingRows || [])) existingMap.set(ex.id, ex);
      } catch (_) { /* non-critical — skip tracking if lookup fails */ }
    }

    const dbRows = rows.map(r => {
      const ex = existingMap.get(r.id);
      const firstSeenLine = ex?.first_seen_line ?? r.lineScore;
      const firstSeenAt   = ex?.first_seen_at   ?? now;
      const prevLine      = ex?.line_score ?? r.lineScore;
      const lineChanged   = ex && (prevLine !== r.lineScore);
      const lineMoveCount = (ex?.line_move_count ?? 0) + (lineChanged ? 1 : 0);

      // PP shade signal: compare PP line to sharp fair line
      let ppShadeSignal: string = 'no_data';
      if (r.sharpFairLine != null) {
        const delta = r.lineScore - r.sharpFairLine;
        if (delta > 0.3)       ppShadeSignal = 'lean_under'; // PP line high vs sharp → under has edge
        else if (delta < -0.3) ppShadeSignal = 'lean_over';  // PP line low vs sharp → over has edge
        else                   ppShadeSignal = 'neutral';
      }

      return {
        id: r.id,
        league: r.league,
        player_name: r.playerName,
        team_abbr: r.teamAbbr ?? null,
        stat_type: r.statType,
        line_score: r.lineScore,
        direction: r.direction,
        is_demon: r.isDemon ?? false,
        is_goblin: r.isGoblin ?? false,
        is_synthetic: r.isSynthetic ?? false,
        game_id: r.gameId ?? null,
        game_matchup: r.gameMatchup ?? null,
        game_start_time: r.gameStartTime ?? null,
        confidence_level: r.confidenceLevel ?? 3,
        prop_score: r.propScore ?? 0,
        reject_reason: r.rejectReason ?? null,
        script_label: r.scriptLabel ?? null,
        pulled_at: r.pulledAt ?? null,
        source: r.source ?? null,
        pp_display_matchup: r.ppDisplayMatchup ?? null,
        pp_display_player: r.ppDisplayPlayer ?? null,
        pp_display_stat: r.ppDisplayStat ?? null,
        pp_display_team: r.ppDisplayTeam ?? null,
        pp_event_title: r.ppEventTitle ?? null,
        // Line movement
        first_seen_line: firstSeenLine,
        first_seen_at:   firstSeenAt,
        last_seen_at:    now,
        line_move_count: lineMoveCount,
        // Sharp signals
        sharp_fair_line:  r.sharpFairLine  ?? null,
        sharp_over_juice: r.sharpOverJuice ?? null,
        sharp_under_juice:r.sharpUnderJuice?? null,
        pp_shade_signal:  ppShadeSignal,
      };
    });

    const CHUNK = 500;
    for (let i = 0; i < dbRows.length; i += CHUNK) {
      const chunk = dbRows.slice(i, i + CHUNK);
      const { error } = await db.from('props').upsert(chunk, { onConflict: 'id' });
      if (error) throw new Error(`[storage] upsertProps failed: ${error}`);
    }

    // Write succeeded — delete any props for this league older than this pull.
    const delUrl = `${process.env.SUPABASE_URL}/rest/v1/props?league=eq.${league}&pulled_at=lt.${encodeURIComponent(pullTimestamp)}`;
    const delResp = await fetch(delUrl, {
      method: 'DELETE',
      headers: {
        'apikey': process.env.SUPABASE_ANON_KEY ?? '',
        'Authorization': `Bearer ${process.env.SUPABASE_ANON_KEY ?? ''}`,
        'Content-Type': 'application/json',
        'Prefer': 'return=minimal',
      },
    });
    if (!delResp.ok) {
      const txt = await delResp.text();
      console.error(`[storage] delete stale props failed (${delResp.status}):`, txt);
    }
  },

  async getProps(league) {
    // Stats permanently blocked from standard slate — blocked at both ingest AND read time
    const BLOCKED_STANDARD_STATS = new Set([
      'Home Runs', 'Doubles', 'RBIs', 'Singles', 'Walks',
      'Triples', 'Stolen Bases', 'Hitter Strikeouts',
      'Plate Appearances', 'Pitcher Fantasy Score', 'Runs',
    ]);
    const PAGE = 1000;
    let all: any[] = [];
    let from = 0;
    while (true) {
      const url = `${process.env.SUPABASE_URL}/rest/v1/props?select=*&league=eq.${league}&order=game_start_time.asc&limit=${PAGE}&offset=${from}`;
      const resp = await fetch(url, {
        headers: {
          'apikey': process.env.SUPABASE_ANON_KEY ?? '',
          'Authorization': `Bearer ${process.env.SUPABASE_ANON_KEY ?? ''}`,
          'Accept': 'application/json',
        },
      });
      if (!resp.ok) throw new Error(`[storage] getProps failed: ${resp.status}`);
      const rows: any[] = await resp.json();
      all = all.concat(rows);
      if (rows.length < PAGE) break;
      from += PAGE;
    }
    return all
      .map(mapProp)
      .filter((p: any) => {
        // Block trash stats at read time — catches any stale DB rows
        if (!p.isGoblin && !p.isDemon && BLOCKED_STANDARD_STATS.has(p.statType)) return false;
        return (
          p.ppDisplayMatchup ||
          (p.gameMatchup && (
            p.gameMatchup.includes(' vs ') ||
            p.gameMatchup.includes('/') ||
            p.gameMatchup.includes(' @ ')
          ))
        );
      });
  },

  async deletePropsForLeague(league) {
    const { error } = await db.from('props').eq('league', league).delete();
    if (error) throw new Error(`[storage] deletePropsForLeague failed: ${error}`);
  },

  // ── Slips ──────────────────────────────────────────────────────────────────
  async createSlip(data) {
    const dbRow = {
      league: data.league,
      game_matchup: data.gameMatchup ?? null,
      game_start_time: data.gameStartTime ?? null,
      script_label: data.scriptLabel ?? null,
      status: data.status ?? 'pending',
      quality_score: data.qualityScore ?? null,
      correlation_score: data.correlationScore ?? null,
      created_at: data.createdAt,
      settled_at: data.settledAt ?? null,
    };
    const { data: row, error } = await db.from('slips').single().insert(dbRow);
    if (error) throw new Error(`[storage] createSlip failed: ${error}`);
    return mapSlip(row);
  },

  async getSlips(statusFilter) {
    let q = db.from('slips').select('*').order('created_at', { ascending: false });
    if (statusFilter && statusFilter.length > 0) {
      q = q.in('status', statusFilter);
    }
    const { data, error } = await q.select_run();
    if (error) throw new Error(`[storage] getSlips failed: ${error}`);
    return (data ?? []).map(mapSlip);
  },

  async getSlipById(id) {
    const { data, error } = await db.from('slips').select('*').eq('id', id).single().select_run();
    if (error || !data) return undefined;
    return mapSlip(data);
  },

  async updateSlipStatus(id, status, extra = {}) {
    const updates: any = { status };
    if ((extra as any).settledAt) updates.settled_at = (extra as any).settledAt;
    const { error } = await db.from('slips').eq('id', id).update(updates);
    if (error) throw new Error(`[storage] updateSlipStatus failed: ${error}`);
  },

  async deleteSlip(id) {
    const { error } = await db.from('slips').eq('id', id).delete();
    if (error) throw new Error(`[storage] deleteSlip failed: ${error}`);
  },

  // ── Legs ───────────────────────────────────────────────────────────────────
  async createLegs(legs) {
    const dbRows = legs.map(l => ({
      slip_id: l.slipId,
      prop_id: l.propId ?? null,
      player_name: l.playerName,
      team_abbr: l.teamAbbr ?? null,
      stat_type: l.statType,
      line_score: l.lineScore,
      direction: l.direction,
      is_demon: l.isDemon ?? false,
      is_goblin: l.isGoblin ?? false,
      game_matchup: l.gameMatchup ?? null,
      game_start_time: l.gameStartTime ?? null,
      status: l.status ?? 'pending',
      actual_value: l.actualValue ?? null,
      hit_explanation: l.hitExplanation ?? null,
      miss_explanation: l.missExplanation ?? null,
      prop_score: l.propScore ?? null,
    }));
    const { error } = await db.from('slip_legs').insert(dbRows);
    if (error) throw new Error(`[storage] createLegs failed: ${error}`);
  },

  async getLegsBySlip(slipId) {
    const { data, error } = await db.from('slip_legs').select('*').eq('slip_id', slipId).order('id', { ascending: true }).select_run();
    if (error) throw new Error(`[storage] getLegsBySlip failed: ${error}`);
    return (data ?? []).map(mapLeg);
  },

  async updateLegStatus(id, status, actualValue) {
    const updates: any = { status };
    if (actualValue != null) updates.actual_value = actualValue;
    const { error } = await db.from('slip_legs').eq('id', id).update(updates);
    if (error) throw new Error(`[storage] updateLegStatus failed: ${error}`);
  },

  async reconcileLeg(id, status, actualValue, lastCheckedAt, trackingError) {
    const updates: any = { status, last_checked_at: lastCheckedAt, tracking_error: trackingError };
    if (actualValue != null) updates.actual_value = actualValue;
    const { error } = await db.from('slip_legs').eq('id', id).update(updates);
    if (error) throw new Error(`[storage] reconcileLeg failed: ${error}`);
  },

  // ── Pull log ───────────────────────────────────────────────────────────────
  async logPull(league, count, status = 'ok') {
    await db.from('pull_log').insert({
      league,
      prop_count: count,
      pulled_at: new Date().toISOString(),
      status,
    });
  },

  async getLastPull(league) {
    const { data } = await db.from('pull_log').select('*').eq('league', league).order('pulled_at', { ascending: false }).limit(1).single().select_run();
    if (!data) return null;
    return {
      league: data.league,
      propCount: data.prop_count,
      pulledAt: data.pulled_at,
      status: data.status,
    };
  },

  // ── Player performance (learning loop) ────────────────────────────────────
  async updatePlayerPerformance(playerName, statType, league, outcome, actualValue, line) {
    // Fetch existing row
    const { data: existing } = await db
      .from('player_performance')
      .select('*')
      .eq('player_name', playerName)
      .eq('stat_type', statType)
      .eq('league', league)
      .single()
      .select_run();

    const margin = actualValue != null ? actualValue - line : null;
    const hitCount  = (existing?.hit_count  ?? 0) + (outcome === 'hit'  ? 1 : 0);
    const missCount = (existing?.miss_count ?? 0) + (outcome === 'miss' ? 1 : 0);

    // Update last_5 ring buffer
    let last5: string[] = [];
    try { last5 = JSON.parse(existing?.last_5 ?? '[]'); } catch (_) {}
    last5.push(outcome);
    if (last5.length > 5) last5 = last5.slice(-5);

    // Recalculate running average margin
    const totalSettled = hitCount + missCount;
    const prevAvg = existing?.avg_margin ?? 0;
    const newAvg = totalSettled > 1 && margin != null
      ? (prevAvg * (totalSettled - 1) + margin) / totalSettled
      : (margin ?? prevAvg);

    const upsertRow = {
      player_name: playerName,
      stat_type: statType,
      league,
      hit_count: hitCount,
      miss_count: missCount,
      last_5: JSON.stringify(last5),
      avg_margin: newAvg,
      last_seen_line: line,
      updated_at: new Date().toISOString(),
    };

    if (existing) {
      await db.from('player_performance').eq('id', existing.id).update(upsertRow);
    } else {
      await db.from('player_performance').insert(upsertRow);
    }
  },

  async getPlayerPerformance(playerName, statType, league) {
    const { data } = await db
      .from('player_performance')
      .select('*')
      .eq('player_name', playerName)
      .eq('stat_type', statType)
      .eq('league', league)
      .single()
      .select_run();
    if (!data) return null;
    let last5: string[] = [];
    try { last5 = JSON.parse(data.last_5 ?? '[]'); } catch (_) {}
    return {
      hitCount:  data.hit_count,
      missCount: data.miss_count,
      last5,
      avgMargin: data.avg_margin,
    };
  },

  async getAllPerformance() {
    const { data } = await db
      .from('player_performance')
      .select('*')
      .order('updated_at', { ascending: false })
      .select_run();
    return (data ?? []).map((r: any) => ({
      playerName: r.player_name,
      statType:   r.stat_type,
      league:     r.league,
      hitCount:   r.hit_count,
      missCount:  r.miss_count,
      last5:      (() => { try { return JSON.parse(r.last_5 ?? '[]'); } catch (_) { return []; } })(),
      avgMargin:  r.avg_margin,
      lastSeenLine: r.last_seen_line,
      updatedAt:  r.updated_at,
      hitRate:    (r.hit_count + r.miss_count) > 0
        ? r.hit_count / (r.hit_count + r.miss_count)
        : null,
    }));
  },
};
