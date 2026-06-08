import { describe, expect, it } from "vitest";
import { checkAuth, checkOrigin, mintChatToken, verifyChatToken } from "./auth";

const NOW = 1_750_000_000_000;

describe("chat token", () => {
  it("verifies a freshly minted token", () => {
    const token = mintChatToken(NOW);
    expect(verifyChatToken(token, NOW)).toBe(true);
  });

  it("rejects a token after it expires", () => {
    const token = mintChatToken(NOW);
    // TTL is 2h; jump past it.
    expect(verifyChatToken(token, NOW + 3 * 60 * 60 * 1000)).toBe(false);
  });

  it("rejects a tampered expiry (signature no longer matches)", () => {
    const token = mintChatToken(NOW);
    const [, sig] = token.split(".");
    const forged = `${NOW + 10 * 60 * 60 * 1000}.${sig}`;
    expect(verifyChatToken(forged, NOW)).toBe(false);
  });

  it("rejects a tampered signature", () => {
    const token = mintChatToken(NOW);
    const [exp] = token.split(".");
    expect(verifyChatToken(`${exp}.deadbeef`, NOW)).toBe(false);
  });

  it("rejects empty / malformed tokens", () => {
    expect(verifyChatToken(null, NOW)).toBe(false);
    expect(verifyChatToken("", NOW)).toBe(false);
    expect(verifyChatToken("no-dot-here", NOW)).toBe(false);
    expect(verifyChatToken(".onlysig", NOW)).toBe(false);
  });
});

function reqWith(headers: Record<string, string>): Request {
  return new Request("http://localhost/api/chat", { method: "POST", headers });
}

describe("checkOrigin", () => {
  it("accepts a same-origin request derived from host + proto", () => {
    expect(
      checkOrigin(
        reqWith({ host: "titans.example", "x-forwarded-proto": "https", origin: "https://titans.example" })
      )
    ).toBe(true);
  });

  it("accepts http same-origin for local dev", () => {
    expect(
      checkOrigin(reqWith({ host: "localhost:3210", origin: "http://localhost:3210" }))
    ).toBe(true);
  });

  it("rejects a mismatched origin", () => {
    expect(
      checkOrigin(reqWith({ host: "titans.example", origin: "https://evil.example" }))
    ).toBe(false);
  });

  it("rejects a missing Origin header (non-browser client)", () => {
    expect(checkOrigin(reqWith({ host: "titans.example" }))).toBe(false);
  });
});

describe("checkAuth", () => {
  it("passes with matching origin and a valid token", () => {
    const token = mintChatToken(NOW);
    const res = checkAuth(
      reqWith({ host: "localhost:3210", origin: "http://localhost:3210", "x-chat-token": token }),
      NOW
    );
    expect(res.ok).toBe(true);
  });

  it("fails when the token is absent even if origin matches", () => {
    const res = checkAuth(
      reqWith({ host: "localhost:3210", origin: "http://localhost:3210" }),
      NOW
    );
    expect(res.ok).toBe(false);
    expect(res.message).toBeTruthy();
  });

  it("fails when the origin is wrong even with a valid token", () => {
    const token = mintChatToken(NOW);
    const res = checkAuth(
      reqWith({ host: "titans.example", origin: "https://evil.example", "x-chat-token": token }),
      NOW
    );
    expect(res.ok).toBe(false);
  });
});
