import { NextResponse } from "next/server";
import { sbConfigured, sbInsert } from "../../../../lib/supabase";
import { COOKIE, verifyToken } from "../../../../lib/auth";

const ACTIONS = new Set(["start", "stop", "check", "backtest"]);

// Queue an action for the VM agent. params (optional) are merged over
// bot_settings by the agent, so the webapp can send just the overrides.
export async function POST(req) {
  if (!sbConfigured()) {
    return NextResponse.json({ ok: false, message: "Supabase env vars not set" },
                             { status: 500 });
  }
  let body = {};
  try { body = await req.json(); } catch { /* validated below */ }
  const action = String(body.action || "");
  if (!ACTIONS.has(action)) {
    return NextResponse.json({ ok: false, message: `bad action: ${action}` },
                             { status: 400 });
  }
  const user = await verifyToken(req.cookies.get(COOKIE)?.value);
  try {
    const rows = await sbInsert("bot_commands", {
      action,
      params: body.params && typeof body.params === "object" ? body.params : {},
      requested_by: user || null,
    });
    return NextResponse.json({ ok: true, id: rows[0]?.id });
  } catch (e) {
    return NextResponse.json({ ok: false, message: String(e.message || e) },
                             { status: 502 });
  }
}
