import { NextResponse } from "next/server";
import { sbConfigured, sbSelect, sbUpsert } from "../../../../lib/supabase";

const EXCHANGES = new Set(["binance", "gate", "gate_futures"]);
const SYMBOL_RE = /^[A-Z0-9]{4,20}$/;

export async function GET() {
  if (!sbConfigured()) return NextResponse.json({ settings: null });
  const rows = await sbSelect("bot_settings?id=eq.1");
  return NextResponse.json({ settings: rows[0] ?? null });
}

// Partial update: only the fields present in the body change; the settings
// page sends exchange/symbol, the dashboard sends params.
export async function POST(req) {
  if (!sbConfigured()) {
    return NextResponse.json({ ok: false, message: "Supabase env vars not set" },
                             { status: 500 });
  }
  let body = {};
  try { body = await req.json(); } catch { /* validated below */ }

  const rows = await sbSelect("bot_settings?id=eq.1");
  const cur = rows[0] ?? { exchange: "gate_futures", symbol: "BTCUSDT", params: {} };

  let { exchange, symbol, params } = cur;
  if (body.exchange !== undefined) {
    exchange = String(body.exchange).toLowerCase();
    if (!EXCHANGES.has(exchange)) {
      return NextResponse.json(
        { ok: false, message: `unknown exchange: ${body.exchange}` }, { status: 400 });
    }
  }
  if (body.symbol !== undefined) {
    symbol = String(body.symbol).trim().toUpperCase();
    if (!SYMBOL_RE.test(symbol)) {
      return NextResponse.json(
        { ok: false, message: "trading pair should be letters/digits like BTCUSDT" },
        { status: 400 });
    }
  }
  if (body.params !== undefined) {
    if (!body.params || typeof body.params !== "object") {
      return NextResponse.json({ ok: false, message: "params must be an object" },
                               { status: 400 });
    }
    params = body.params;
  }

  try {
    await sbUpsert("bot_settings", {
      id: 1, exchange, symbol, params,
      updated_at: new Date().toISOString(),
    });
    return NextResponse.json({ ok: true, settings: { exchange, symbol, params } });
  } catch (e) {
    return NextResponse.json({ ok: false, message: String(e.message || e) },
                             { status: 502 });
  }
}
