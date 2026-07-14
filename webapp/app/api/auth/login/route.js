import { NextResponse } from "next/server";
import { COOKIE, TTL_MS, createToken, hashPassword, verifyPassword }
  from "../../../../lib/auth";
import { sbConfigured, sbSelect, sbInsert } from "../../../../lib/supabase";

// Auth lives in the webapp_users table once Supabase is connected. Bootstrap:
// while the table is empty, a login matching ADMIN_USERNAME/ADMIN_PASSWORD
// seeds it (hashed) — after that the env pair is no longer consulted. Without
// Supabase configured, the env pair alone is checked (local dev fallback).
async function checkCredentials(username, password) {
  const envUser = process.env.ADMIN_USERNAME;
  const envPass = process.env.ADMIN_PASSWORD;

  if (!sbConfigured()) {
    if (!envUser || !envPass) {
      return { ok: false, status: 500,
               message: "no Supabase and no ADMIN_* env vars — auth is not configured" };
    }
    return { ok: username === envUser && password === envPass };
  }

  const rows = await sbSelect(
    `webapp_users?username=eq.${encodeURIComponent(username)}` +
    "&select=username,password_hash");
  if (rows.length) {
    return { ok: await verifyPassword(password, rows[0].password_hash) };
  }
  const anyUser = await sbSelect("webapp_users?select=username&limit=1");
  if (!anyUser.length && envUser && envPass &&
      username === envUser && password === envPass) {
    await sbInsert("webapp_users", {
      username, password_hash: await hashPassword(password),
    });
    return { ok: true };
  }
  return { ok: false };
}

export async function POST(req) {
  let body = {};
  try {
    body = await req.json();
  } catch {
    /* fall through to the credential check */
  }
  const username = String(body.username || "");
  const password = String(body.password || "");

  let result;
  try {
    result = await checkCredentials(username, password);
  } catch (e) {
    return NextResponse.json(
      { ok: false, message: `auth backend error: ${String(e.message || e).slice(0, 200)}` },
      { status: 502 });
  }
  if (!result.ok) {
    return NextResponse.json(
      { ok: false, message: result.message || "invalid username or password" },
      { status: result.status || 401 });
  }
  const res = NextResponse.json({ ok: true });
  res.cookies.set(COOKIE, await createToken(username), {
    httpOnly: true,
    sameSite: "lax",
    secure: process.env.NODE_ENV === "production",
    maxAge: TTL_MS / 1000,
    path: "/",
  });
  return res;
}
