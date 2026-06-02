import fs from "node:fs";
import path from "node:path";

// Haiku 4.5 rates — kept in sync with pipeline/cost_log.py.
const HAIKU_USD_PER_MTOK_IN = 1.0;
const HAIKU_USD_PER_MTOK_OUT = 5.0;

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

// Sum this calendar month's spend from the append-only log. Missing file or a
// malformed line is treated as zero / skipped — never throws to the caller.
export function monthToDateUsd(
  now: Date = new Date(),
  logPath: string = LOG_PATH
): number {
  let raw: string;
  try {
    raw = fs.readFileSync(logPath, "utf8");
  } catch {
    return 0;
  }
  const month = currentMonth(now);
  let total = 0;
  for (const line of raw.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    try {
      const entry = JSON.parse(trimmed) as Partial<CostEntry>;
      if (entry.month === month && typeof entry.usd === "number") {
        total += entry.usd;
      }
    } catch {
      // skip malformed line
    }
  }
  return total;
}

export function isOverCap(
  now: Date = new Date(),
  logPath: string = LOG_PATH
): boolean {
  return monthToDateUsd(now, logPath) >= MONTHLY_CAP_USD;
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
    fs.appendFileSync(logPath, JSON.stringify(entry) + "\n", "utf8");
    return true;
  } catch {
    return false;
  }
}
