import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
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
    if (p && fs.existsSync(p)) fs.rmSync(p, { recursive: true, force: true });
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

  it("is false (zero spent) when the log is genuinely missing", () => {
    expect(isOverCap(new Date("2026-06-10T00:00:00Z"), tmpLog())).toBe(false);
  });

  it("fails CLOSED when the log is a directory (read error)", () => {
    const dir = path.join(
      os.tmpdir(),
      `chat_cost_dir_${Date.now()}_${Math.random().toString(36).slice(2)}`
    );
    fs.mkdirSync(dir);
    tmpFiles.push(dir);
    expect(isOverCap(new Date("2026-06-10T00:00:00Z"), dir)).toBe(true);
  });

  it("fails CLOSED when the log is wholly unparseable garbage", () => {
    const logPath = tmpLog();
    fs.writeFileSync(logPath, "%%not json%%\n\x00\x01garbage\n", "utf8");
    expect(isOverCap(new Date("2026-06-10T00:00:00Z"), logPath)).toBe(true);
  });
});

describe("monthToDateUsd fail-closed", () => {
  it("throws on a wholly-unparseable non-empty log", () => {
    const logPath = tmpLog();
    fs.writeFileSync(logPath, "not json\nstill not json\n", "utf8");
    expect(() =>
      monthToDateUsd(new Date("2026-06-10T00:00:00Z"), logPath)
    ).toThrow();
  });

  it("still tolerates a single malformed line among valid entries", () => {
    const logPath = tmpLog();
    fs.writeFileSync(
      logPath,
      [
        JSON.stringify({ month: "2026-06", usd: 2 }),
        "garbage line",
        JSON.stringify({ month: "2026-06", usd: 1 }),
      ].join("\n") + "\n",
      "utf8"
    );
    expect(
      monthToDateUsd(new Date("2026-06-10T00:00:00Z"), logPath)
    ).toBeCloseTo(3, 10);
  });
});

describe("price env overrides", () => {
  const ENV_KEYS = ["HAIKU_USD_PER_MTOK_IN", "HAIKU_USD_PER_MTOK_OUT"] as const;
  const saved: Record<string, string | undefined> = {};

  afterEach(() => {
    for (const k of ENV_KEYS) {
      if (saved[k] === undefined) delete process.env[k];
      else process.env[k] = saved[k];
    }
    vi.resetModules();
  });

  it("uses env-provided prices when set", async () => {
    for (const k of ENV_KEYS) saved[k] = process.env[k];
    process.env.HAIKU_USD_PER_MTOK_IN = "2";
    process.env.HAIKU_USD_PER_MTOK_OUT = "10";
    vi.resetModules();
    const fresh = await import("./cost-guard");
    expect(
      fresh.usdForTokens({ input_tokens: 1_000_000, output_tokens: 1_000_000 })
    ).toBeCloseTo(12, 10);
  });

  it("falls back to defaults for a non-positive or non-numeric value", async () => {
    for (const k of ENV_KEYS) saved[k] = process.env[k];
    process.env.HAIKU_USD_PER_MTOK_IN = "0";
    process.env.HAIKU_USD_PER_MTOK_OUT = "abc";
    vi.resetModules();
    const fresh = await import("./cost-guard");
    expect(
      fresh.usdForTokens({ input_tokens: 1_000_000, output_tokens: 1_000_000 })
    ).toBeCloseTo(6, 10);
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
