import { describe, expect, it, vi, beforeEach } from "vitest";

const {
  logTurnShared,
  isOverCapShared,
  planQuery,
  retrievePeople,
  countMatches,
  directoryStats,
  streamAnswer,
} = vi.hoisted(() => ({
  logTurnShared: vi.fn(async () => true),
  isOverCapShared: vi.fn(async () => false),
  planQuery: vi.fn(),
  retrievePeople: vi.fn(async () => []),
  countMatches: vi.fn(() => 42),
  directoryStats: vi.fn(() => ({ total: 1056, enriched: 110 })),
  streamAnswer: vi.fn(),
}));

vi.mock("@/lib/chat/cost-guard", () => ({ logTurnShared, isOverCapShared }));
vi.mock("@/lib/chat/plan", () => ({ planQuery }));
vi.mock("@/lib/chat/search", () => ({ retrievePeople, countMatches }));
vi.mock("@/lib/db", () => ({ directoryStats }));
vi.mock("@/lib/chat/synthesize", () => ({ streamAnswer }));
vi.mock("@/lib/chat/guards", () => ({
  MAX_INPUT_CHARS: 500,
  checkInput: () => ({ ok: true }),
  checkTopic: () => ({ ok: true }),
  checkRateShared: async () => ({ ok: true }),
  rejection: () => ({ ok: false, message: "rejected" }),
}));
vi.mock("@/lib/chat/auth", () => ({ checkAuth: () => ({ ok: true }) }));

import { POST } from "./route";

const PLAN_USAGE = { input_tokens: 37, output_tokens: 9 };

type Turn = { role: "user" | "assistant"; content: string };

function requestWith(messages: Turn[]): Request {
  return new Request("http://localhost/api/chat", {
    method: "POST",
    body: JSON.stringify({ messages }),
  });
}

function request(content = "Who is in Dallas?"): Request {
  return requestWith([{ role: "user", content }]);
}

async function drain(res: Response): Promise<void> {
  const reader = res.body!.getReader();
  while (true) {
    const { done } = await reader.read();
    if (done) break;
  }
}

beforeEach(() => {
  vi.clearAllMocks();
  isOverCapShared.mockResolvedValue(false);
  retrievePeople.mockResolvedValue([]);
  planQuery.mockResolvedValue({ usage: PLAN_USAGE, params: {} });
});

describe("chat route cost accounting", () => {
  it("logs combined usage on a normal stream", async () => {
    streamAnswer.mockImplementation(async function* () {
      yield { type: "text", text: "hi" };
      yield { type: "usage", usage: { input_tokens: 100, output_tokens: 50 } };
    });

    await drain(await POST(request()));

    expect(logTurnShared).toHaveBeenCalledTimes(1);
    expect(logTurnShared).toHaveBeenCalledWith({
      input_tokens: PLAN_USAGE.input_tokens + 100,
      output_tokens: PLAN_USAGE.output_tokens + 50,
    });
  });

  it("still logs the already-spent plan cost when synthesis throws before usage", async () => {
    streamAnswer.mockImplementation(async function* () {
      yield { type: "text", text: "partial" };
      throw new Error("stream blew up before usage event");
    });

    await drain(await POST(request()));

    expect(logTurnShared).toHaveBeenCalledTimes(1);
    expect(logTurnShared).toHaveBeenCalledWith(PLAN_USAGE);
  });

  it("logs plan cost even if synthesis yields nothing at all", async () => {
    streamAnswer.mockImplementation(async function* () {
      // immediately throws with no events
      throw new Error("died instantly");
    });

    await drain(await POST(request()));

    expect(logTurnShared).toHaveBeenCalledTimes(1);
    expect(logTurnShared).toHaveBeenCalledWith(PLAN_USAGE);
  });
});

describe("chat route body schema", () => {
  it("rejects an oversized EARLIER user turn with 400 before any model call", async () => {
    const res = await POST(
      requestWith([
        { role: "user", content: "x".repeat(501) },
        { role: "assistant", content: "earlier answer" },
        { role: "user", content: "Who is in Dallas?" },
      ])
    );
    expect(res.status).toBe(400);
    expect(res.headers.get("x-chat-status")).toBe("rejected");
    expect(planQuery).not.toHaveBeenCalled();
    expect(streamAnswer).not.toHaveBeenCalled();
    expect(logTurnShared).not.toHaveBeenCalled();
  });

  it("rejects an oversized assistant turn (> 4x the user limit) with 400", async () => {
    const res = await POST(
      requestWith([
        { role: "user", content: "Who is in Dallas?" },
        { role: "assistant", content: "a".repeat(2001) },
        { role: "user", content: "And Houston?" },
      ])
    );
    expect(res.status).toBe(400);
    expect(planQuery).not.toHaveBeenCalled();
  });

  it("accepts an assistant turn within the cap", async () => {
    streamAnswer.mockImplementation(async function* () {
      yield { type: "usage", usage: { input_tokens: 1, output_tokens: 1 } };
    });
    const res = await POST(
      requestWith([
        { role: "user", content: "Who is in Dallas?" },
        { role: "assistant", content: "a".repeat(2000) },
        { role: "user", content: "And Houston?" },
      ])
    );
    expect(res.status).toBe(200);
    await drain(res);
    expect(planQuery).toHaveBeenCalledTimes(1);
  });

  it("returns 400 (rejected) for a malformed body", async () => {
    const res = await POST(
      new Request("http://localhost/api/chat", {
        method: "POST",
        body: JSON.stringify({ messages: "nope" }),
      })
    );
    expect(res.status).toBe(400);
    expect(res.headers.get("x-chat-status")).toBe("rejected");
    expect(await res.text()).toContain("couldn't read that request");
  });
});

describe("directory scale passed to synthesis", () => {
  // The counts must reach the synthesizer, or it answers "how many" from the
  // handful of retrieved rows (it once reported 12 for a 1,056-person roster).
  it("hands the real totals and the filtered match count to streamAnswer", async () => {
    planQuery.mockResolvedValue({ params: { city: "Dallas" }, usage: PLAN_USAGE });
    streamAnswer.mockReturnValue(
      (async function* () {
        yield { type: "text", text: "ok" };
      })()
    );

    await drain(await POST(request()));

    expect(streamAnswer).toHaveBeenCalledWith(expect.anything(), [], {
      total: 1056,
      enriched: 110,
      matched: 42,
    });
    expect(countMatches).toHaveBeenCalledWith({ city: "Dallas" });
  });
});
