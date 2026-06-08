import fs from "node:fs";
import path from "node:path";
import {
  hasSharedStore,
  redisAddCostUsd,
  redisMonthCostUsd,
} from "./store";

// Haiku 4.5 rates — kept in sync with pipeline/cost_log.py. Overridable via env
// so a published price change can't silently make the spend cap wrong; an
// absent, non-numeric, or non-positive value falls back to the default.
function priceFromEnv(value: string | undefined, fallback: number): number {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? n : fallback;
}
const HAIKU_USD_PER_MTOK_IN = priceFromEnv(
  process.env.HAIKU_USD_PER_MTOK_IN,
  1.0
);
const HAIKU_USD_PER_MTOK_OUT = priceFromEnv(
  process.env.HAIKU_USD_PER_MTOK_OUT,
  5.0
);

// Hard monthly spend cap (USD) for the public chat endpoint. At/above this,
// the kill switch fires and NO Anthropic call is made.
export const MONTHLY_CAP_USD = 100;

// Separate log from the pipeline's cost_log.jsonl so the two never collide.
const LOG_PATH = path.join(
  process.cwd(),
  "..",
  "pipeline",
  "data",
  "chat_cost_log.jsonl"
);

export interface TurnUsage {
  input_tokens: number;
  output_tokens: number;
}

interface CostEntry {
  ts: string; // ISO timestamp
  month: string; // YYYY-MM (UTC) for fast month filtering
  input_tokens: number;
  output_tokens: number;
  usd: number;
}

export function usdForTokens(usage: TurnUsage): number {
  const inUsd = (usage.input_tokens / 1_000_000) * HAIKU_USD_PER_MTOK_IN;
  const outUsd = (usage.output_tokens / 1_000_000) * HAIKU_USD_PER_MTOK_OUT;
  return inUsd + outUsd;
}

function currentMonth(now: Date = new Date()): string {
  return now.toISOString().slice(0, 7); // YYYY-MM
}

// Sum this calendar month's spend from the append-only log.
//
// Fail-CLOSED on anything that means we can't trust the total: a genuinely
// missing file is the only "this is fine, $0 spent" case (returns 0). A read
// error (permissions, I/O, a directory) or a non-empty file whose every line
// fails to parse is corruption — it throws, and isOverCap() converts that into
// "over cap" so a broken log can never silently disable the kill switch. A
// single malformed line in an otherwise valid log is still tolerated/skipped.
export function monthToDateUsd(
  now: Date = new Date(),
  logPath: string = LOG_PATH
): number {
  let raw: string;
  try {
    raw = fs.readFileSync(logPath, "utf8");
  } catch (err: unknown) {
    if ((err as NodeJS.ErrnoException)?.code === "ENOENT") return 0;
    throw err;
  }
  const month = currentMonth(now);
  let total = 0;
  let parsed = 0;
  let malformed = 0;
  for (const line of raw.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    try {
      const entry = JSON.parse(trimmed) as Partial<CostEntry>;
      parsed += 1;
      if (entry.month === month && typeof entry.usd === "number") {
        total += entry.usd;
      }
    } catch {
      malformed += 1;
    }
  }
  if (parsed === 0 && malformed > 0) {
    throw new Error("chat cost log is unparseable — refusing to trust total");
  }
  return total;
}

// Fail-closed: any inability to read/parse the log reads as "over cap".
export function isOverCap(
  now: Date = new Date(),
  logPath: string = LOG_PATH
): boolean {
  try {
    return monthToDateUsd(now, logPath) >= MONTHLY_CAP_USD;
  } catch {
    return true;
  }
}

// Append one completed turn's tokens + cost. Best-effort: a logging failure
// must not break the user's answer, but it is surfaced via the return flag.
export function logTurn(
  usage: TurnUsage,
  now: Date = new Date(),
  logPath: string = LOG_PATH
): boolean {
  const entry: CostEntry = {
    ts: now.toISOString(),
    month: currentMonth(now),
    input_tokens: usage.input_tokens,
    output_tokens: usage.output_tokens,
    usd: usdForTokens(usage),
  };
  try {
    // Single-writer assumption: one server instance owns this append-only log,
    // so an unlocked appendFileSync is atomic enough. On a multi-instance host
    // use the Redis-backed logTurnShared() instead (see below) — this file path
    // is only the single-instance backend and the serverless fallback.
    fs.appendFileSync(logPath, JSON.stringify(entry) + "\n", "utf8");
    return true;
  } catch {
    return false;
  }
}

// ── Multi-instance entry points ──────────────────────────────────────────────
// These are what the route calls. When Upstash is configured they use an atomic
// Redis counter that is correct across instances; otherwise they delegate to the
// in-process file functions above so local dev and tests need no external store.

// Hard kill switch, fail-CLOSED: any error reading the shared counter reads as
// "over cap" so a broken store can never silently disable the spend limit.
export async function isOverCapShared(now: Date = new Date()): Promise<boolean> {
  if (!hasSharedStore()) return isOverCap(now);
  try {
    return (await redisMonthCostUsd(currentMonth(now))) >= MONTHLY_CAP_USD;
  } catch {
    return true;
  }
}

// Record one completed turn's spend. Best-effort: a store failure must not break
// the user's answer, but it is surfaced via the boolean so callers can log it.
export async function logTurnShared(
  usage: TurnUsage,
  now: Date = new Date()
): Promise<boolean> {
  if (!hasSharedStore()) return logTurn(usage, now);
  try {
    await redisAddCostUsd(currentMonth(now), usdForTokens(usage));
    return true;
  } catch {
    return false;
  }
}
