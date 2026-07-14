import { NextResponse } from "next/server";
import { sbConfigured, sbUpsert, sbDelete } from "../../../../lib/supabase";

// Gate spot and futures share one Gate key pair (same split as the local
// api_server's GATE_*/BINANCE_* env vars), so keys are stored per family.
const CRED_KEYS = new Set(["binance", "gate"]);

export async function POST(req) {
  if (!sbConfigured()) {
    return NextResponse.json({ ok: false, message: "Supabase env vars not set" },
                             { status: 500 });
  }
  let body = {};
  try { body = await req.json(); } catch { /* validated below */ }
  const exchange = String(body.exchange || "").toLowerCase();
  const key = exchange.startsWith("gate") ? "gate" : exchange;
  if (!CRED_KEYS.has(key) || !body.api_key || !body.api_secret) {
    return NextResponse.json(
      { ok: false, message: "exchange, api_key and api_secret are required" },
      { status: 400 });
  }
  try {
    await sbUpsert("bot_credentials", {
      exchange: key,
      api_key: String(body.api_key).trim(),
      api_secret: String(body.api_secret).trim(),
      updated_at: new Date().toISOString(),
    });
    return NextResponse.json({ ok: true });
  } catch (e) {
    return NextResponse.json({ ok: false, message: String(e.message || e) },
                             { status: 502 });
  }
}

export async function DELETE(req) {
  if (!sbConfigured()) {
    return NextResponse.json({ ok: false, message: "Supabase env vars not set" },
                             { status: 500 });
  }
  const exchange = String(
    req.nextUrl.searchParams.get("exchange") || "").toLowerCase();
  const key = exchange.startsWith("gate") ? "gate" : exchange;
  if (!CRED_KEYS.has(key)) {
    return NextResponse.json({ ok: false, message: `bad exchange: ${exchange}` },
                             { status: 400 });
  }
  try {
    await sbDelete(`bot_credentials?exchange=eq.${key}`);
    return NextResponse.json({ ok: true });
  } catch (e) {
    return NextResponse.json({ ok: false, message: String(e.message || e) },
                             { status: 502 });
  }
}
