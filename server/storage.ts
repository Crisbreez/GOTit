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
  logPull(league: string, count: number, status?: string): Promise<void>;
  getLastPull(league: string): Promise<any>;
}

export const storage: IStorage = {

  // ── Props ──────────────────────────────────────────────────────────────────
  async upsertProps(rows) {
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

    const dbRows = rows.map(r => ({
      id: r.id,
      league: r.league,
      player_name: r.playerName,
      team_abbr: r.teamAbbr ?? null,
      stat_type: r.statType,
      line_score: r.lineScore,
      direction: r.direction,
      is_demon: r.isDemon ?? false,
      is_goblin: r.isGoblin ?? false,
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
    }));

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
      .filter((p: any) =>
        p.ppDisplayMatchup ||
        (p.gameMatchup && (
          p.gameMatchup.includes(' vs ') ||
          p.gameMatchup.includes('/') ||
          p.gameMatchup.includes(' @ ')
        ))
      );
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
};
