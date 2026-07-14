import { NextResponse } from "next/server";
import { sbConfigured, sbSelect } from "../../../../lib/supabase";

// Everything the dashboard needs in one poll: agent state, saved settings,
// masked credentials, and recent command history (the activity feed).
export async function GET() {
  if (!sbConfigured()) {
    return NextResponse.json({ configured: false });
  }
  try {
    const [state, settings, creds, recent] = await Promise.all([
      sbSelect("bot_state?id=eq.1"),
      sbSelect("bot_settings?id=eq.1"),
      sbSelect("bot_credentials?select=exchange,api_key,updated_at"),
      sbSelect("bot_commands?select=id,action,status,result,requested_by," +
               "created_at,handled_at&order=id.desc&limit=10"),
    ]);
    return NextResponse.json({
      configured: true,
      state: state[0] ?? null,
      settings: settings[0] ?? null,
      credentials: creds.map((c) => ({
        exchange: c.exchange,
        api_key_masked: c.api_key.length > 8
          ? `${c.api_key.slice(0, 4)}...${c.api_key.slice(-4)}` : "****",
        updated_at: c.updated_at,
      })),
      recent,
      queue: recent.filter((c) => c.status === "pending" || c.status === "running"),
    });
  } catch (e) {
    return NextResponse.json({ configured: true, error: String(e.message || e) },
                             { status: 502 });
  }
}
