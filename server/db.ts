import Database from "better-sqlite3";
import { drizzle } from "drizzle-orm/better-sqlite3";
import * as schema from "@shared/schema";
import path from "path";

const DB_PATH = process.env.DB_PATH || path.resolve(process.cwd(), "data.db");
const sqlite = new Database(DB_PATH);

sqlite.pragma("journal_mode = WAL");
sqlite.pragma("foreign_keys = ON");

// Init tables
sqlite.exec(`
  CREATE TABLE IF NOT EXISTS props (
    id TEXT PRIMARY KEY,
    league TEXT NOT NULL,
    player_name TEXT NOT NULL,
    team_abbr TEXT,
    stat_type TEXT NOT NULL,
    line_score REAL NOT NULL,
    direction TEXT NOT NULL DEFAULT 'over',
    is_demon INTEGER NOT NULL DEFAULT 0,
    is_goblin INTEGER NOT NULL DEFAULT 0,
    game_id TEXT,
    game_matchup TEXT,
    game_start_time TEXT,
    confidence_level INTEGER DEFAULT 3,
    prop_score REAL DEFAULT 0,
    reject_reason TEXT,
    script_label TEXT,
    pulled_at TEXT
  );

  CREATE TABLE IF NOT EXISTS slips (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    league TEXT NOT NULL,
    game_matchup TEXT,
    game_start_time TEXT,
    script_label TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    quality_score REAL,
    correlation_score REAL,
    created_at TEXT NOT NULL,
    settled_at TEXT
  );

  CREATE TABLE IF NOT EXISTS slip_legs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slip_id INTEGER NOT NULL,
    prop_id TEXT,
    player_name TEXT NOT NULL,
    team_abbr TEXT,
    stat_type TEXT NOT NULL,
    line_score REAL NOT NULL,
    direction TEXT NOT NULL DEFAULT 'over',
    is_demon INTEGER DEFAULT 0,
    is_goblin INTEGER DEFAULT 0,
    game_start_time TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    actual_value REAL,
    hit_explanation TEXT,
    miss_explanation TEXT,
    prop_score REAL
  );

  CREATE TABLE IF NOT EXISTS pull_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    league TEXT NOT NULL,
    pulled_at TEXT NOT NULL,
    prop_count INTEGER DEFAULT 0,
    status TEXT DEFAULT 'ok'
  );
`);

// ── Runtime migrations (idempotent) ─────────────────────────────────────────────────
const MIGRATIONS = [
  // game_matchup on slip_legs — needed for mlbTracker to narrow to correct game
  `ALTER TABLE slip_legs ADD COLUMN game_matchup TEXT`,
  // pp_display_* columns on props — added in later dev cycle
  `ALTER TABLE props ADD COLUMN source TEXT`,
  `ALTER TABLE props ADD COLUMN pp_display_matchup TEXT`,
  `ALTER TABLE props ADD COLUMN pp_display_player TEXT`,
  `ALTER TABLE props ADD COLUMN pp_display_stat TEXT`,
  `ALTER TABLE props ADD COLUMN pp_display_team TEXT`,
  `ALTER TABLE props ADD COLUMN pp_event_title TEXT`,
  // Directional scorer columns — two-sided EV model
  `ALTER TABLE props ADD COLUMN true_prob REAL`,
  `ALTER TABLE props ADD COLUMN edge REAL`,
  `ALTER TABLE props ADD COLUMN confidence REAL`,
  `ALTER TABLE props ADD COLUMN fragility REAL`,
  `ALTER TABLE props ADD COLUMN correlation REAL`,
  `ALTER TABLE props ADD COLUMN ev REAL`,
  // slip_legs direction column — persists chosen over/under per leg
  `ALTER TABLE slip_legs ADD COLUMN direction TEXT CHECK (direction IN ('over','under'))`,
];
for (const sql of MIGRATIONS) {
  try { sqlite.exec(sql); } catch (_) { /* column already exists */ }
}

// ── One-time data fix: normalize game_start_time to UTC ISO (Z suffix) ──────────
// PP returns times like "2026-07-07T18:40:00.000-04:00".
// SQLite text-compares these against our UTC date filter, causing all props
// with tz offsets to fail the >= today check and be silently dropped.
// This runs once on startup and fixes any existing rows in-place.
(() => {
  const rows = sqlite.prepare(
    `SELECT id, game_start_time FROM props WHERE game_start_time IS NOT NULL AND game_start_time NOT LIKE '%Z'`
  ).all() as { id: string; game_start_time: string }[];

  if (rows.length === 0) return;

  const update = sqlite.prepare(`UPDATE props SET game_start_time = ? WHERE id = ?`);
  const run = sqlite.transaction(() => {
    for (const row of rows) {
      try {
        const utc = new Date(row.game_start_time).toISOString();
        update.run(utc, row.id);
      } catch (_) { /* skip unparseable */ }
    }
  });
  run();
  console.log(`[DB] Normalized ${rows.length} game_start_time values to UTC`);
})();

export const db = drizzle(sqlite, { schema });
export { sqlite };
