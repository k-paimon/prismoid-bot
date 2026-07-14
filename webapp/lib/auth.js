// Signed-cookie sessions with Web Crypto only, so the same code runs in the
// edge middleware and in node route handlers. Pre-Supabase auth: one admin
// identity from env vars (ADMIN_USERNAME / ADMIN_PASSWORD), sessions signed
// with SESSION_SECRET.

const COOKIE = "gridbot_session";
const TTL_MS = 12 * 60 * 60 * 1000; // 12h

const enc = new TextEncoder();

async function hmacHex(payload) {
  const key = await crypto.subtle.importKey(
    "raw", enc.encode(process.env.SESSION_SECRET || "dev-only-secret"),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(payload));
  return Array.from(new Uint8Array(sig))
    .map((b) => b.toString(16).padStart(2, "0")).join("");
}

export async function createToken(username) {
  const payload = `${encodeURIComponent(username)}.${Date.now() + TTL_MS}`;
  return `${payload}.${await hmacHex(payload)}`;
}

// --- password hashing (PBKDF2 via Web Crypto, for webapp_users rows) ---

const PBKDF2_ITERS = 100_000;

function hex(buf) {
  return Array.from(new Uint8Array(buf))
    .map((b) => b.toString(16).padStart(2, "0")).join("");
}

function fromHex(s) {
  return new Uint8Array((s.match(/../g) || []).map((h) => parseInt(h, 16)));
}

async function pbkdf2Hex(password, salt, iterations) {
  const key = await crypto.subtle.importKey(
    "raw", enc.encode(password), "PBKDF2", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits(
    { name: "PBKDF2", hash: "SHA-256", salt, iterations }, key, 256);
  return hex(bits);
}

export async function hashPassword(password) {
  const salt = crypto.getRandomValues(new Uint8Array(16));
  const digest = await pbkdf2Hex(password, salt, PBKDF2_ITERS);
  return `pbkdf2$${PBKDF2_ITERS}$${hex(salt)}$${digest}`;
}

export async function verifyPassword(password, stored) {
  const [scheme, iters, saltHex, hashHex] = String(stored || "").split("$");
  if (scheme !== "pbkdf2" || !saltHex || !hashHex) return false;
  const got = await pbkdf2Hex(password, fromHex(saltHex), Number(iters));
  if (got.length !== hashHex.length) return false;
  let diff = 0;
  for (let i = 0; i < got.length; i++)
    diff |= got.charCodeAt(i) ^ hashHex.charCodeAt(i);
  return diff === 0;
}

export async function verifyToken(token) {
  if (!token) return null;
  const lastDot = token.lastIndexOf(".");
  if (lastDot < 0) return null;
  const payload = token.slice(0, lastDot);
  const sig = token.slice(lastDot + 1);
  const expected = await hmacHex(payload);
  if (sig.length !== expected.length) return null;
  let diff = 0; // constant-time-ish compare
  for (let i = 0; i < expected.length; i++)
    diff |= sig.charCodeAt(i) ^ expected.charCodeAt(i);
  if (diff !== 0) return null;
  const [user, expiry] = payload.split(".");
  if (!expiry || Date.now() > Number(expiry)) return null;
  return decodeURIComponent(user);
}

export { COOKIE, TTL_MS };
