import { describe, expect, it } from "vitest";
import {
  checkInput,
  checkRate,
  checkTopic,
  MAX_INPUT_CHARS,
  RATE_LIMIT_PER_MIN,
} from "./guards";

describe("checkInput", () => {
  it("rejects empty / whitespace-only input", () => {
    expect(checkInput("   ").ok).toBe(false);
    expect(checkInput("   ").reason).toBe("empty");
  });

  it("rejects input over the char cap", () => {
    const res = checkInput("a".repeat(MAX_INPUT_CHARS + 1));
    expect(res.ok).toBe(false);
    expect(res.reason).toBe("too_long");
  });

  it("accepts normal input", () => {
    expect(checkInput("Who is in PE in Dallas?").ok).toBe(true);
  });
});

describe("checkRate", () => {
  it("allows up to the limit then rejects within the window", () => {
    const ip = "1.2.3.4";
    const now = 1_000_000;
    for (let i = 0; i < RATE_LIMIT_PER_MIN; i++) {
      expect(checkRate(ip, now).ok).toBe(true);
    }
    const over = checkRate(ip, now);
    expect(over.ok).toBe(false);
    expect(over.reason).toBe("rate_limited");
  });

  it("lets requests through again after the window slides", () => {
    const ip = "5.6.7.8";
    const now = 2_000_000;
    for (let i = 0; i < RATE_LIMIT_PER_MIN; i++) checkRate(ip, now);
    expect(checkRate(ip, now).ok).toBe(false);
    // 61s later the old hits have aged out
    expect(checkRate(ip, now + 61_000).ok).toBe(true);
  });

  it("tracks IPs independently", () => {
    const now = 3_000_000;
    for (let i = 0; i < RATE_LIMIT_PER_MIN; i++) checkRate("9.9.9.9", now);
    expect(checkRate("9.9.9.9", now).ok).toBe(false);
    expect(checkRate("8.8.8.8", now).ok).toBe(true);
  });
});

describe("checkTopic", () => {
  it("allows short messages (greetings / follow-ups)", () => {
    expect(checkTopic("hi").ok).toBe(true);
    expect(checkTopic("what about NYC?").ok).toBe(true);
  });

  it("allows clearly on-topic questions", () => {
    expect(
      checkTopic("Which alumni work in investment banking in Houston?").ok
    ).toBe(true);
  });

  it("rejects long obviously off-topic prompts", () => {
    const res = checkTopic(
      "Please write me a long poem about dragons and wizards in space"
    );
    expect(res.ok).toBe(false);
    expect(res.reason).toBe("off_topic");
  });
});
