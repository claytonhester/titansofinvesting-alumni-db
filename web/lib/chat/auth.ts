// Lightweight gate for the public chat endpoint. A public alumni page can't hide
// a secret in the browser, so this is deliberately "raise the bar", not "lock
// it down" — it pairs with the per-IP rate limit and the hard monthly spend cap
// (the real backstop). Two cheap, header-only checks run before any model call:
//
//   1. Same-origin — the POST's Origin must match the deployment (or an allowed
//      origin). Blocks naive cross-site / curl abuse that sends no Origin.
//   2. Signed page token — an HMAC token minted during server render of the page
//      (see mintChatToken, embedded by app/page.tsx) and echoed back in the
//      x-chat-token header. A caller must at least load the real page to get one,
//      and tokens expire, so a fire-and-forget script can't pound the endpoint.

import crypto from "node:crypto";

export interface AuthResult {
  ok: boolean;
  message?: string;
}

// 2 hours: long enough for a real reading session without re-minting, short
// enough that a scraped token goes stale quickly.
const TOKEN_TTL_MS = 2 * 60 * 60 * 1000;

const REJECT_MESSAGE =
  "This chat can only be used from the Titans alumni site. Please reload the page and try again.";

// Dev fallback keeps local/test working without config; production MUST set
// CHAT_TOKEN_SECRET (an unset secret in prod means forgeable tokens).
function secret(): string {
  return process.env.CHAT_TOKEN_SECRET || "dev-insecure-chat-token-secret";
}

function sign(payload: string): string {
  return crypto.createHmac("sha256", secret()).update(payload).digest("base64url");
}

// `${expiryMs}.${signature}` — the signature covers the expiry so it can't be
// extended by the client.
export function mintChatToken(now: number = Date.now()): string {
  const exp = String(now + TOKEN_TTL_MS);
  return `${exp}.${sign(exp)}`;
}

export function verifyChatToken(
  token: string | null | undefined,
  now: number = Date.now()
): boolean {
  if (!token) return false;
  const dot = token.indexOf(".");
  if (dot <= 0) return false;
  const expStr = token.slice(0, dot);
  const sig = token.slice(dot + 1);
  const exp = Number(expStr);
  if (!Number.isFinite(exp) || exp < now) return false;
  const expected = sign(expStr);
  const a = Buffer.from(sig);
  const b = Buffer.from(expected);
  if (a.length !== b.length) return false;
  return crypto.timingSafeEqual(a, b);
}

// The set of origins allowed to call the endpoint: any explicitly configured via
// ALLOWED_ORIGIN (comma-separated), plus the request's own host derived from the
// platform-trusted Host + forwarded-proto headers (same-origin).
function allowedOrigins(req: Request): Set<string> {
  const set = new Set<string>();
  const configured = process.env.ALLOWED_ORIGIN;
  if (configured) {
    for (const o of configured.split(",")) {
      const trimmed = o.trim();
      if (trimmed) set.add(trimmed);
    }
  }
  const host = req.headers.get("host");
  if (host) {
    const proto = req.headers.get("x-forwarded-proto") || "https";
    set.add(`${proto}://${host}`);
    set.add(`http://${host}`); // local dev over http
  }
  return set;
}

export function checkOrigin(req: Request): boolean {
  const origin = req.headers.get("origin");
  // Browsers always send Origin on a cross-origin-capable POST; a missing Origin
  // is a non-browser client and is rejected for this browser-only endpoint.
  if (!origin) return false;
  return allowedOrigins(req).has(origin);
}

export function checkAuth(req: Request, now: number = Date.now()): AuthResult {
  if (!checkOrigin(req)) return { ok: false, message: REJECT_MESSAGE };
  if (!verifyChatToken(req.headers.get("x-chat-token"), now)) {
    return { ok: false, message: REJECT_MESSAGE };
  }
  return { ok: true };
}
