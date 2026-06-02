import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import {
  isOverCap,
  logTurn,
  MONTHLY_CAP_USD,
  monthToDateUsd,
  usdForTokens,
} from "./cost-guard";

const tmpFiles: string[] = [];

function tmpLog(): string {
  const p = path.join(
    os.tmpdir(),
    `chat_cost_log_${Date.now()}_${Math.random().toString(36).slice(2)}.jsonl`
  );
  tmpFiles.push(p);
  return p;
}

afterEach(() => {
  while (tmpFiles.length) {
    const p = tmpFiles.pop();
    if (p && fs.existsSync(p)) fs.rmSync(p);
  }
});

describe("usdForTokens", () => {
  it("prices input at $1/MTok and output at $5/MTok", () => {
    expect(usdForTokens({ input_tokens: 1_000_000, output_tokens: 0 })).toBe(1);
    expect(usdForTokens({ input_tokens: 0, output_tokens: 1_000_000 })).toBe(5);
  });

  it("sums input and output", () => {
    expect(
      usdForTokens({ input_tokens: 500_000, output_tokens: 200_000 })
    ).toBeCloseTo(0.5 + 1.0, 10);
  });

  it("is zero for no usage", () => {
    expect(usdForTokens({ input_tokens: 0, output_tokens: 0 })).toBe(0);
  });
});

describe("monthToDateUsd", () => {
  it("returns 0 when the log file is missing", () => {
    expect(monthToDateUsd(new Date("2026-06-02T00:00:00Z"), tmpLog())).toBe(0);
  });

  it("sums only entries in the current month and skips malformed lines", () => {
    const logPath = tmpLog();
    const lines = [
      JSON.stringify({ month: "2026-06", usd: 1.5 }),
      JSON.stringify({ month: "2026-06", usd: 2.25 }),
      JSON.stringify({ month: "2026-05", usd: 99 }), // other month
      "not json at all", // malformed
      JSON.stringify({ month: "2026-06" }), // usd missing
      "", // blank
    ];
    fs.writeFileSync(logPath, lines.join("\n") + "\n", "utf8");
    expect(monthToDateUsd(new Date("2026-06-15T00:00:00Z"), logPath)).toBeCloseTo(
      3.75,
      10
    );
  });
});

describe("isOverCap", () => {
  it("is false below the cap", () => {
    const logPath = tmpLog();
    fs.writeFileSync(
      logPath,
      JSON.stringify({ month: "2026-06", usd: MONTHLY_CAP_USD - 0.01 }) + "\n",
      "utf8"
    );
    expect(isOverCap(new Date("2026-06-10T00:00:00Z"), logPath)).toBe(false);
  });

  it("is true at exactly the cap", () => {
    const logPath = tmpLog();
    fs.writeFileSync(
      logPath,
      JSON.stringify({ month: "2026-06", usd: MONTHLY_CAP_USD }) + "\n",
      "utf8"
    );
    expect(isOverCap(new Date("2026-06-10T00:00:00Z"), logPath)).toBe(true);
  });
});

describe("logTurn", () => {
  it("appends a turn that monthToDateUsd then reads back", () => {
    const logPath = tmpLog();
    const now = new Date("2026-06-02T12:00:00Z");
    const ok = logTurn(
      { input_tokens: 1_000_000, output_tokens: 1_000_000 },
      now,
      logPath
    );
    expect(ok).toBe(true);
    expect(monthToDateUsd(now, logPath)).toBeCloseTo(6, 10);
  });

  it("accumulates across multiple appends", () => {
    const logPath = tmpLog();
    const now = new Date("2026-06-02T12:00:00Z");
    logTurn({ input_tokens: 1_000_000, output_tokens: 0 }, now, logPath);
    logTurn({ input_tokens: 1_000_000, output_tokens: 0 }, now, logPath);
    expect(monthToDateUsd(now, logPath)).toBeCloseTo(2, 10);
  });

  it("returns false when the path is unwritable", () => {
    const ok = logTurn(
      { input_tokens: 10, output_tokens: 10 },
      new Date(),
      "/nonexistent-dir-xyz/cost.jsonl"
    );
    expect(ok).toBe(false);
  });
});
