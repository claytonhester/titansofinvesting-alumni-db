// Shared, multi-instance state for the public chat endpoint.
//
// On a single always-on server the in-memory rate limiter and file-based cost
// log are fine. On a serverless / multi-instance host (Vercel) they are not:
// each instance has its own memory and an ephemeral, unshared filesystem, so
// the per-IP limit multiplies by instance count and the monthly spend cap
// undercounts. This module backs both with Upstash Redis (atomic INCR over
// HTTP) when configured, and is a no-op otherwise so local dev and tests fall
// back to the in-process implementations without any external service.

import { Redis } from "@upstash/redis";

let _redis: Redis | null | undefined;

// Lazily construct the client once. Returns null when Upstash env vars are
// absent — callers MUST treat null as "no shared store, use the local path".
export function redisClient(): Redis | null {
  if (_redis !== undefined) return _redis;
  const url = process.env.UPSTASH_REDIS_REST_URL;
  const token = process.env.UPSTASH_REDIS_REST_TOKEN;
  _redis = url && token ? new Redis({ url, token }) : null;
  return _redis;
}

export function hasSharedStore(): boolean {
  return redisClient() !== null;
}

// Store money as integer micro-USD so INCR never accumulates float drift.
const USD_SCALE = 1_000_000;
// Keep a month's counter ~70 days: long enough to span the active month with
// slack, short enough that stale months self-evict.
const COST_TTL_SECONDS = 70 * 24 * 60 * 60;

function costKey(month: string): string {
  return `chat:cost:${month}`;
}

// Atomically add one turn's spend to the month counter. First write on a fresh
// key sets the TTL so the counter can't live forever.
export async function redisAddCostUsd(month: string, usd: number): Promise<void> {
  const r = redisClient();
  if (!r) return;
  const micros = Math.round(usd * USD_SCALE);
  if (micros <= 0) return;
  const total = await r.incrby(costKey(month), micros);
  if (total === micros) await r.expire(costKey(month), COST_TTL_SECONDS);
}

export async function redisMonthCostUsd(month: string): Promise<number> {
  const r = redisClient();
  if (!r) return 0;
  const micros = await r.get<number>(costKey(month));
  return Number(micros ?? 0) / USD_SCALE;
}

// Fixed-window per-IP counter. Returns true if the request is ALLOWED, false if
// it would exceed `limit` within the current window. Atomic: INCR is the source
// of truth; the first hit in a window arms the TTL so the key self-expires.
export async function redisCheckRate(
  ip: string,
  limit: number,
  windowMs: number,
  now: number
): Promise<boolean> {
  const r = redisClient();
  if (!r) return true;
  const windowId = Math.floor(now / windowMs);
  const key = `chat:rl:${ip}:${windowId}`;
  const count = await r.incr(key);
  if (count === 1) await r.expire(key, Math.ceil(windowMs / 1000) + 1);
  return count <= limit;
}
