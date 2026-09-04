import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Mock the Redis primitives so these tests exercise the branching logic in
// cost-guard / guards without any live Upstash connection.
vi.mock("./store", () => ({
  hasSharedStore: vi.fn(),
  redisAddCostUsd: vi.fn(),
  redisMonthCostUsd: vi.fn(),
  redisCheckRate: vi.fn(),
}));

import {
  hasSharedStore,
  redisAddCostUsd,
  redisMonthCostUsd,
  redisCheckRate,
} from "./store";
import { isOverCapShared, logTurnShared, MONTHLY_CAP_USD } from "./cost-guard";
import { checkRateShared } from "./guards";

const mockHas = vi.mocked(hasSharedStore);
const mockAdd = vi.mocked(redisAddCostUsd);
const mockMonth = vi.mocked(redisMonthCostUsd);
const mockRate = vi.mocked(redisCheckRate);

beforeEach(() => {
  vi.clearAllMocks();
});

describe("isOverCapShared (Redis path)", () => {
  it("is false below the cap", async () => {
    mockHas.mockReturnValue(true);
    mockMonth.mockResolvedValue(MONTHLY_CAP_USD - 1);
    expect(await isOverCapShared()).toBe(false);
  });

  it("is true at/above the cap", async () => {
    mockHas.mockReturnValue(true);
    mockMonth.mockResolvedValue(MONTHLY_CAP_USD);
    expect(await isOverCapShared()).toBe(true);
  });

  it("fails CLOSED when the shared counter read throws", async () => {
    mockHas.mockReturnValue(true);
    mockMonth.mockRejectedValue(new Error("redis down"));
    expect(await isOverCapShared()).toBe(true);
  });

  it("delegates to the local file path when no shared store", async () => {
    mockHas.mockReturnValue(false);
    const result = await isOverCapShared();
    expect(typeof result).toBe("boolean");
    expect(mockMonth).not.toHaveBeenCalled();
  });
});

describe("isOverCapShared on Vercel without a shared store", () => {
  afterEach(() => vi.unstubAllEnvs());

  it("fails CLOSED (over cap) — the file-backed cap is unenforceable there", async () => {
    vi.stubEnv("VERCEL", "1");
    mockHas.mockReturnValue(false);
    expect(await isOverCapShared()).toBe(true);
    expect(mockMonth).not.toHaveBeenCalled();
  });

  it("still uses the shared counter when Upstash is configured on Vercel", async () => {
    vi.stubEnv("VERCEL", "1");
    mockHas.mockReturnValue(true);
    mockMonth.mockResolvedValue(0);
    expect(await isOverCapShared()).toBe(false);
  });

  it("keeps the local file path off Vercel", async () => {
    vi.stubEnv("VERCEL", "");
    mockHas.mockReturnValue(false);
    // A fresh month with no local log reads as $0 spent (below cap).
    expect(await isOverCapShared(new Date("2099-01-01T00:00:00Z"))).toBe(false);
  });
});

describe("logTurnShared (Redis path)", () => {
  it("records spend to the shared counter and reports success", async () => {
    mockHas.mockReturnValue(true);
    mockAdd.mockResolvedValue(undefined);
    const ok = await logTurnShared({ input_tokens: 1_000_000, output_tokens: 0 });
    expect(ok).toBe(true);
    expect(mockAdd).toHaveBeenCalledTimes(1);
    const [, usd] = mockAdd.mock.calls[0];
    expect(usd).toBeCloseTo(1, 10); // $1/MTok input
  });

  it("returns false when the shared write throws (does not throw)", async () => {
    mockHas.mockReturnValue(true);
    mockAdd.mockRejectedValue(new Error("redis down"));
    expect(await logTurnShared({ input_tokens: 10, output_tokens: 10 })).toBe(false);
  });
});

describe("checkRateShared (Redis path)", () => {
  it("allows when the shared counter is under the limit", async () => {
    mockHas.mockReturnValue(true);
    mockRate.mockResolvedValue(true);
    expect((await checkRateShared("1.2.3.4")).ok).toBe(true);
  });

  it("rejects when the shared counter is over the limit", async () => {
    mockHas.mockReturnValue(true);
    mockRate.mockResolvedValue(false);
    const res = await checkRateShared("1.2.3.4");
    expect(res.ok).toBe(false);
    expect(res.reason).toBe("rate_limited");
  });

  it("fails OPEN when the shared counter read throws", async () => {
    mockHas.mockReturnValue(true);
    mockRate.mockRejectedValue(new Error("redis down"));
    expect((await checkRateShared("1.2.3.4")).ok).toBe(true);
  });
});
