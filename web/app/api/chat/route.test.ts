import { describe, expect, it, vi, beforeEach } from "vitest";

const { logTurnShared, isOverCapShared, planQuery, searchPeople, streamAnswer } =
  vi.hoisted(() => ({
    logTurnShared: vi.fn(async () => true),
    isOverCapShared: vi.fn(async () => false),
    planQuery: vi.fn(),
    searchPeople: vi.fn(() => []),
    streamAnswer: vi.fn(),
  }));

vi.mock("@/lib/chat/cost-guard", () => ({ logTurnShared, isOverCapShared }));
vi.mock("@/lib/chat/plan", () => ({ planQuery }));
vi.mock("@/lib/chat/search", () => ({ searchPeople }));
vi.mock("@/lib/chat/synthesize", () => ({ streamAnswer }));
vi.mock("@/lib/chat/guards", () => ({
  checkInput: () => ({ ok: true }),
  checkTopic: () => ({ ok: true }),
  checkRateShared: async () => ({ ok: true }),
  rejection: () => ({ ok: false, message: "rejected" }),
}));
vi.mock("@/lib/chat/auth", () => ({ checkAuth: () => ({ ok: true }) }));

import { POST } from "./route";

const PLAN_USAGE = { input_tokens: 37, output_tokens: 9 };

function request(content = "Who is in Dallas?"): Request {
  return new Request("http://localhost/api/chat", {
    method: "POST",
    body: JSON.stringify({ messages: [{ role: "user", content }] }),
  });
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
  searchPeople.mockReturnValue([]);
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
