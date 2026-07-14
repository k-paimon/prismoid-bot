import { NextResponse } from "next/server";
import { sbConfigured, sbSelect } from "../../../../lib/supabase";

// Incremental console feed for the dashboard's live feed:
// GET ?since=N -> { next, lines: [{seq, line, at}] } for rows with seq >= N.
export async function GET(req) {
  if (!sbConfigured()) return NextResponse.json({ next: 0, lines: [] });
  const since = Math.max(0, Number(req.nextUrl.searchParams.get("since")) || 0);
  try {
    const rows = await sbSelect(
      `bot_logs?seq=gte.${since}&order=seq.asc&limit=500&select=seq,line,created_at`);
    const next = rows.length ? rows[rows.length - 1].seq + 1 : since;
    return NextResponse.json({
      next,
      lines: rows.map((r) => ({ seq: r.seq, line: r.line, at: r.created_at })),
    });
  } catch (e) {
    return NextResponse.json({ error: String(e.message || e) }, { status: 502 });
  }
}
