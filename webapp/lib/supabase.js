// Server-side Supabase (PostgREST) helper. Uses the service-role key, so it
// must only ever be imported from route handlers / server components — the
// key bypasses RLS and must never reach the browser bundle.
//
//   sbSelect("bot_state?id=eq.1")
//   sbInsert("bot_commands", { action: "stop" })
//   sbUpsert("bot_settings", { id: 1, symbol: "BTCUSDT" })

export function sbConfigured() {
  return Boolean(process.env.SUPABASE_URL && process.env.SUPABASE_SERVICE_ROLE_KEY);
}

async function sb(pathAndQuery, { method = "GET", body, prefer } = {}) {
  const key = process.env.SUPABASE_SERVICE_ROLE_KEY;
  // sb_secret_ keys are rejected for browser-looking clients; this runs
  // server-side only, so say so explicitly
  const headers = { apikey: key, Authorization: `Bearer ${key}`,
                    "User-Agent": "gridbot-webapp/0.1" };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (prefer) headers.Prefer = prefer;
  const res = await fetch(
    `${process.env.SUPABASE_URL.replace(/\/$/, "")}/rest/v1/${pathAndQuery}`,
    {
      method, headers, cache: "no-store",
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  const text = await res.text();
  if (!res.ok) throw new Error(`supabase ${res.status}: ${text.slice(0, 300)}`);
  return text ? JSON.parse(text) : null;
}

export const sbSelect = (pathAndQuery) => sb(pathAndQuery);

export const sbInsert = (table, rows) =>
  sb(table, { method: "POST", body: rows, prefer: "return=representation" });

export const sbUpsert = (table, row) =>
  sb(table, {
    method: "POST", body: row,
    prefer: "return=minimal,resolution=merge-duplicates",
  });

export const sbDelete = (pathAndQuery) =>
  sb(pathAndQuery, { method: "DELETE", prefer: "return=minimal" });
