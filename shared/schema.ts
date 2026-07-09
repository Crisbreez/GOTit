import { sqliteTable, text, integer, real } from "drizzle-orm/sqlite-core";
import { createInsertSchema } from "drizzle-zod";
import { z } from "zod";

// ── Props (live PrizePicks projections) ──────────────────────────────────────
export const props = sqliteTable("props", {
  id: text("id").primaryKey(),
  league: text("league").notNull(),           // MLB | NBA | NFL | MMA
  playerName: text("player_name").notNull(),
  teamAbbr: text("team_abbr"),
  statType: text("stat_type").notNull(),
  lineScore: real("line_score").notNull(),
  direction: text("direction").notNull(),     // over | under
  isDemon: integer("is_demon", { mode: "boolean" }).default(false),
  isGoblin: integer("is_goblin", { mode: "boolean" }).default(false),
  gameId: text("game_id"),
  gameMatchup: text("game_matchup"),
  gameStartTime: text("game_start_time"),
  confidenceLevel: integer("confidence_level").default(3),
  propScore: real("prop_score").default(0),
  rejectReason: text("reject_reason"),
  scriptLabel: text("script_label"),
  pulledAt: text("pulled_at"),
  // Display truth — preserved verbatim from the source provider (PrizePicks)
  // Never overwritten by GOTit's internal transforms
  source: text("source"),                         // 'prizepicks' | 'sportsgameodds' | 'demo' | 'cache'
  ppDisplayMatchup: text("pp_display_matchup"),   // matchup exactly as PP returns it
  ppDisplayPlayer: text("pp_display_player"),     // player name exactly as PP returns it
  ppDisplayStat: text("pp_display_stat"),         // stat type exactly as PP returns it
  ppDisplayTeam: text("pp_display_team"),         // team abbr/name exactly as PP returns it
  ppEventTitle: text("pp_event_title"),           // event title from PP (MMA event name etc)
});

// ── Slips ────────────────────────────────────────────────────────────────────
export const slips = sqliteTable("slips", {
  id: integer("id").primaryKey({ autoIncrement: true }),
  league: text("league").notNull(),
  gameMatchup: text("game_matchup"),
  gameStartTime: text("game_start_time"),
  scriptLabel: text("script_label"),
  status: text("status").notNull().default("pending"), // pending | live | settled_win | settled_loss
  qualityScore: real("quality_score"),
  correlationScore: real("correlation_score"),
  createdAt: text("created_at").notNull(),
  settledAt: text("settled_at"),
});

export const insertSlipSchema = createInsertSchema(slips).omit({ id: true });
export type InsertSlip = z.infer<typeof insertSlipSchema>;
export type Slip = typeof slips.$inferSelect;

// ── Slip Legs ────────────────────────────────────────────────────────────────
export const slipLegs = sqliteTable("slip_legs", {
  id: integer("id").primaryKey({ autoIncrement: true }),
  slipId: integer("slip_id").notNull(),
  propId: text("prop_id"),
  playerName: text("player_name").notNull(),
  teamAbbr: text("team_abbr"),
  statType: text("stat_type").notNull(),
  lineScore: real("line_score").notNull(),
  direction: text("direction").notNull(),
  isDemon: integer("is_demon", { mode: "boolean" }).default(false),
  isGoblin: integer("is_goblin", { mode: "boolean" }).default(false),
  gameMatchup: text("game_matchup"),   // "Away vs Home" — used by mlbTracker to narrow game lookup
  gameStartTime: text("game_start_time"),
  status: text("status").notNull().default("pending"), // pending | live | hit | miss
  actualValue: real("actual_value"),
  hitExplanation: text("hit_explanation"),
  missExplanation: text("miss_explanation"),
  propScore: real("prop_score"),
});

export const insertLegSchema = createInsertSchema(slipLegs).omit({ id: true });
export type InsertLeg = z.infer<typeof insertLegSchema>;
export type SlipLeg = typeof slipLegs.$inferSelect;

// ── Pull Log ─────────────────────────────────────────────────────────────────
export const pullLog = sqliteTable("pull_log", {
  id: integer("id").primaryKey({ autoIncrement: true }),
  league: text("league").notNull(),
  pulledAt: text("pulled_at").notNull(),
  propCount: integer("prop_count").default(0),
  status: text("status").default("ok"),
});
