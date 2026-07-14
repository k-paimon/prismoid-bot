import { NextResponse } from "next/server";
import { sbConfigured, sbSelect, sbUpsert } from "../../../../lib/supabase";

const EXCHANGES = new Set(["binance", "gate", "gate_futures"]);

export async function GET() {
  if (!sbConfigured()) return NextResponse.json({ settings: null });
  const rows = await sbSelect("bot_settings?id=eq.1");
  return NextResponse.json({ settings: rows[0] ?? null });
}

// Persist the defaults the VM agent merges into every start/check/backtest.
export async function POST(req) {
  if (!sbConfigured()) {
    return NextResponse.json({ ok: false, message: "Supabase env vars not set" },
                             { status: 500 });
  }
  let body = {};
  try { body = await req.json(); } catch { /* validated below */ }
  const exchange = String(body.exchange || "").toLowerCase();
  const symbol = String(body.symbol || "").trim().toUpperCase();
  if (!EXCHANGES.has(exchange) || !symbol) {
    return NextResponse.json(
      { ok: false, message: "exchange and symbol are required" }, { status: 400 });
  }
  try {
    await sbUpsert("bot_settings", {
      id: 1, exchange, symbol,
      params: body.params && typeof body.params === "object" ? body.params : {},
      updated_at: new Date().toISOString(),
    });
    return NextResponse.json({ ok: true });
  } catch (e) {
    return NextResponse.json({ ok: false, message: String(e.message || e) },
                             { status: 502 });
  }
}
