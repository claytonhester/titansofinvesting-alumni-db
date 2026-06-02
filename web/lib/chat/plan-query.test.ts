import { describe, expect, it, vi } from "vitest";

const create = vi.fn();

vi.mock("./anthropic", () => ({
  HAIKU_MODEL: "claude-haiku-test",
  anthropic: () => ({ messages: { create } }),
}));

import { planQuery } from "./plan";

describe("planQuery", () => {
  it("parses the model's JSON into coerced params and returns usage", async () => {
    create.mockResolvedValue({
      content: [{ type: "text", text: '{"city":"Dallas","titanClass":12}' }],
      usage: { input_tokens: 120, output_tokens: 18 },
    });

    const res = await planQuery([{ role: "user", content: "PE in Dallas?" }]);

    expect(res.params).toEqual({ city: "Dallas", titanClass: 12 });
    expect(res.usage).toEqual({ input_tokens: 120, output_tokens: 18 });
  });

  it("returns empty params when the model emits no text block", async () => {
    create.mockResolvedValue({
      content: [{ type: "tool_use" }],
      usage: { input_tokens: 5, output_tokens: 0 },
    });

    const res = await planQuery([{ role: "user", content: "hi" }]);
    expect(res.params).toEqual({});
  });
});
