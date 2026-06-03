import { describe, expect, it, vi } from "vitest";
import type { RetrievedPerson } from "./search";

const streamFn = vi.fn();

vi.mock("./anthropic", () => ({
  HAIKU_MODEL: "claude-haiku-test",
  anthropic: () => ({ messages: { stream: streamFn } }),
}));

import { streamAnswer } from "./synthesize";

function fakeStream(deltas: string[], usage = { input_tokens: 40, output_tokens: 12 }) {
  const events = [
    ...deltas.map((text) => ({
      type: "content_block_delta",
      delta: { type: "text_delta", text },
    })),
    // a non-text event that must be ignored
    { type: "content_block_delta", delta: { type: "input_json_delta" } },
  ];
  return {
    async *[Symbol.asyncIterator]() {
      for (const e of events) yield e;
    },
    finalMessage: async () => ({ usage }),
  };
}

function person(over: Partial<RetrievedPerson> = {}): RetrievedPerson {
  return {
    full_name: "Jane Doe",
    name_slug: "jane-doe",
    titan_class: 12,
    school: "Texas",
    initial_company: "Acme Capital",
    city: "Dallas",
    source_url: "https://example.com/jane",
    claims: [],
    ...over,
  };
}

async function collect(rows: RetrievedPerson[]) {
  streamFn.mockReturnValue(fakeStream(["Hello ", "world"]));
  const text: string[] = [];
  let usage: { input_tokens: number; output_tokens: number } | undefined;
  for await (const ev of streamAnswer(
    [{ role: "user", content: "Who is in Dallas?" }],
    rows
  )) {
    if (ev.type === "text") text.push(ev.text);
    if (ev.type === "usage") usage = ev.usage;
  }
  return { text: text.join(""), usage };
}

describe("streamAnswer", () => {
  it("yields text deltas then a final usage event", async () => {
    const { text, usage } = await collect([person()]);
    expect(text).toBe("Hello world");
    expect(usage).toEqual({ input_tokens: 40, output_tokens: 12 });
  });

  it("builds a grounded context from the rows", async () => {
    await collect([person({ claims: [] })]);
    const callArg = streamFn.mock.calls.at(-1)![0];
    const userMsg = callArg.messages[callArg.messages.length - 1];
    expect(userMsg.content).toContain("ALUMNI RECORDS");
    expect(userMsg.content).toContain("jane-doe");
    expect(userMsg.content).toContain("VISITOR QUESTION: Who is in Dallas?");
  });

  it("signals an empty directory when no rows match", async () => {
    await collect([]);
    const callArg = streamFn.mock.calls.at(-1)![0];
    const userMsg = callArg.messages[callArg.messages.length - 1];
    expect(userMsg.content).toContain("none matched");
  });

  it("renders the full verified claim set without truncating to four", async () => {
    const claim = (claim_type: string, value: string) => ({
      claim_type,
      value,
      source_url: "https://example.com/x",
    });
    await collect([
      person({
        claims: [
          claim("current_title", "Partner"),
          claim("current_employer", "Acme PE"),
          claim("career_history", "Analyst at Bank (2010-2012)"),
          claim("education", "MBA from Texas"),
          claim("location", "Dallas, TX"),
          claim("short_bio", "Focuses on growth equity."),
        ],
      }),
    ]);
    const callArg = streamFn.mock.calls.at(-1)![0];
    const userMsg = callArg.messages[callArg.messages.length - 1];
    // All six survive — the old code dropped everything past the first four.
    expect(userMsg.content).toContain("short_bio: Focuses on growth equity.");
    expect(userMsg.content).toContain("location: Dallas, TX");
  });

  it("orders claims current-role-first and excludes news mentions", async () => {
    const claim = (claim_type: string, value: string) => ({
      claim_type,
      value,
      source_url: "https://example.com/x",
    });
    await collect([
      person({
        claims: [
          claim("news_mention", "2021 — Jane named to 30 under 30"),
          claim("short_bio", "Bio text."),
          claim("current_title", "Partner"),
        ],
      }),
    ]);
    const callArg = streamFn.mock.calls.at(-1)![0];
    const content: string = callArg.messages[callArg.messages.length - 1]
      .content;
    expect(content).not.toContain("news_mention");
    expect(content).not.toContain("30 under 30");
    expect(content.indexOf("current_title")).toBeLessThan(
      content.indexOf("short_bio")
    );
  });
});
