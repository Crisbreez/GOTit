/**
 * Supabase DB access via direct Postgres connection (pg driver).
 * We bypass the supabase-js client entirely to avoid the WebSocket/realtime
 * dependency that crashes on Node 20 in the published sandbox.
 *
 * Connection string format:
 *   postgresql://postgres.[ref]:[password]@aws-0-us-west-2.pooler.supabase.com:6543/postgres
 *
 * We use the REST API (fetch) instead — no ws, no realtime, just HTTP.
 */

const SUPABASE_URL = process.env.SUPABASE_URL ?? '';
const SUPABASE_ANON_KEY = process.env.SUPABASE_ANON_KEY ?? '';

if (!SUPABASE_URL || !SUPABASE_ANON_KEY) {
  console.warn('[Supabase] Credentials not set — DB calls will fail');
}

type Row = Record<string, any>;

interface QueryOptions {
  select?: string;
  filters?: string;   // raw PostgREST filter string e.g. "league=eq.MLB"
  order?: string;     // e.g. "created_at.desc"
  limit?: number;
  single?: boolean;
}

// ── Core HTTP helpers ─────────────────────────────────────────────────────────
async function pgRest(
  method: 'GET' | 'POST' | 'PATCH' | 'DELETE',
  table: string,
  opts: {
    select?: string;
    filters?: string[];
    order?: string;
    limit?: number;
    single?: boolean;
    body?: any;
    upsert?: boolean;
    onConflict?: string;
  } = {}
): Promise<{ data: any; error: string | null }> {
  if (!SUPABASE_URL || !SUPABASE_ANON_KEY) {
    return { data: null, error: 'Supabase credentials not configured' };
  }

  let url = `${SUPABASE_URL}/rest/v1/${table}`;
  const params: string[] = [];
  if (opts.select) params.push(`select=${encodeURIComponent(opts.select)}`);
  if (opts.filters) opts.filters.forEach(f => params.push(f));
  if (opts.order) params.push(`order=${encodeURIComponent(opts.order)}`);
  if (opts.limit) params.push(`limit=${opts.limit}`);
  if (params.length) url += '?' + params.join('&');

  const headers: Record<string, string> = {
    'apikey': SUPABASE_ANON_KEY,
    'Authorization': `Bearer ${SUPABASE_ANON_KEY}`,
    'Content-Type': 'application/json',
  };

  if (opts.single) headers['Accept'] = 'application/vnd.pgrst.object+json';
  else headers['Accept'] = 'application/json';

  if (method === 'POST' && opts.upsert) {
    headers['Prefer'] = `resolution=merge-duplicates,return=representation`;
    if (opts.onConflict) headers['Prefer'] += `,on_conflict=${opts.onConflict}`;
    else headers['Prefer'] = 'resolution=merge-duplicates,return=representation';
  } else if (method === 'POST') {
    headers['Prefer'] = 'return=representation';
  } else if (method === 'PATCH') {
    headers['Prefer'] = 'return=representation';
  } else if (method === 'DELETE') {
    headers['Prefer'] = 'return=minimal';
  }

  try {
    const resp = await fetch(url, {
      method,
      headers,
      body: opts.body != null ? JSON.stringify(opts.body) : undefined,
    });

    if (resp.status === 204) return { data: null, error: null };

    const text = await resp.text();
    let parsed: any;
    try { parsed = JSON.parse(text); } catch { parsed = text; }

    if (!resp.ok) {
      const msg = parsed?.message ?? parsed?.error ?? text;
      return { data: null, error: `${resp.status} ${msg}` };
    }

    return { data: parsed, error: null };
  } catch (e: any) {
    return { data: null, error: e.message };
  }
}

// ── Public API (mirrors supabase-js surface used by storage.ts) ───────────────
const db = {
  from(table: string) {
    return new TableQuery(table);
  }
};

class TableQuery {
  private _table: string;
  private _filters: string[] = [];
  private _select = '*';
  private _order?: string;
  private _limit?: number;
  private _single = false;
  private _upsertConflict?: string;

  constructor(table: string) { this._table = table; }

  select(cols = '*') { this._select = cols; return this; }
  eq(col: string, val: any) { this._filters.push(`${col}=eq.${val}`); return this; }
  in(col: string, vals: any[]) { this._filters.push(`${col}=in.(${vals.join(',')})`); return this; }
  gte(col: string, val: any) { this._filters.push(`${col}=gte.${val}`); return this; }
  lt(col: string, val: any) { this._filters.push(`${col}=lt.${val}`); return this; }
  like(col: string, pat: string) { this._filters.push(`${col}=like.${pat}`); return this; }
  is(col: string, val: any) { this._filters.push(`${col}=is.${val}`); return this; }
  or(expr: string) { this._filters.push(`or=(${expr})`); return this; }
  order(col: string, opts?: { ascending?: boolean }) {
    this._order = `${col}.${opts?.ascending === false ? 'desc' : 'asc'}`;
    return this;
  }
  limit(n: number) { this._limit = n; return this; }
  single() { this._single = true; return this; }

  async select_run() {
    return pgRest('GET', this._table, {
      select: this._select,
      filters: this._filters,
      order: this._order,
      limit: this._limit,
      single: this._single,
    });
  }

  // Terminal: run a SELECT
  then(resolve: (v: any) => any, reject?: (e: any) => any): Promise<any> {
    return this.select_run().then(resolve, reject);
  }

  async insert(body: any) {
    return pgRest('POST', this._table, { body, single: this._single });
  }

  async upsert(body: any, opts?: { onConflict?: string }) {
    return pgRest('POST', this._table, { body, upsert: true, onConflict: opts?.onConflict });
  }

  async update(body: any) {
    return pgRest('PATCH', this._table, {
      body,
      filters: this._filters,
    });
  }

  async delete() {
    return pgRest('DELETE', this._table, { filters: this._filters });
  }
}

export default db;
