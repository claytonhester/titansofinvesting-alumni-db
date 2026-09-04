import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// redisClient() memoises its client, so each case needs a fresh module.
const REDIS_VARS = [
  "UPSTASH_REDIS_REST_URL",
  "UPSTASH_REDIS_REST_TOKEN",
  "KV_REST_API_URL",
  "KV_REST_API_TOKEN",
] as const;

const saved: Record<string, string | undefined> = {};

async function freshStore() {
  vi.resetModules();
  return import("./store");
}

beforeEach(() => {
  for (const key of REDIS_VARS) {
    saved[key] = process.env[key];
    delete process.env[key];
  }
});

afterEach(() => {
  for (const key of REDIS_VARS) {
    if (saved[key] === undefined) delete process.env[key];
    else process.env[key] = saved[key];
  }
});

describe("shared store credentials", () => {
  it("has no store when neither pair is set", async () => {
    const store = await freshStore();
    expect(store.hasSharedStore()).toBe(false);
    expect(store.redisClient()).toBeNull();
  });

  // What the Upstash dashboard hands out.
  it("uses the UPSTASH_* pair", async () => {
    process.env.UPSTASH_REDIS_REST_URL = "https://upstash.example.test";
    process.env.UPSTASH_REDIS_REST_TOKEN = "upstash-token";
    const store = await freshStore();
    expect(store.hasSharedStore()).toBe(true);
  });

  // What provisioning Upstash through the Vercel Marketplace injects — the
  // production setup. Before this was accepted the cap silently had no shared
  // store, which on Vercel means chat refuses every request.
  it("uses the KV_REST_API_* pair", async () => {
    process.env.KV_REST_API_URL = "https://kv.example.test";
    process.env.KV_REST_API_TOKEN = "kv-token";
    const store = await freshStore();
    expect(store.hasSharedStore()).toBe(true);
  });

  it("ignores a URL with no token", async () => {
    process.env.KV_REST_API_URL = "https://kv.example.test";
    const store = await freshStore();
    expect(store.hasSharedStore()).toBe(false);
  });
});
