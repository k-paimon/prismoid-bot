import { NextResponse } from "next/server";
import { sbConfigured, sbSelect } from "../../../../lib/supabase";

// Incremental console feed, same contract as the local /api/logs:
// GET ?since=N -> { next, lines } where lines are rows with seq >= N.
export async function GET(req) {
  if (!sbConfigured()) return NextResponse.json({ next: 0, lines: [] });
  const since = Math.max(0, Number(req.nextUrl.searchParams.get("since")) || 0);
  try {
    const rows = await sbSelect(
      `bot_logs?seq=gte.${since}&order=seq.asc&limit=500&select=seq,line`);
    const next = rows.length ? rows[rows.length - 1].seq + 1 : since;
    return NextResponse.json({ next, lines: rows.map((r) => r.line) });
  } catch (e) {
    return NextResponse.json({ error: String(e.message || e) }, { status: 502 });
  }
}
