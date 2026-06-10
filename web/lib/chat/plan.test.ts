import { describe, expect, it } from "vitest";
import { parsePlanJson, coerceParams } from "./plan";
import { SECTOR_NAMES } from "@/lib/db";

describe("parsePlanJson", () => {
  it("parses plain JSON", () => {
    expect(parsePlanJson('{"city":"Dallas"}')).toEqual({ city: "Dallas" });
  });

  it("strips ```json fences", () => {
    expect(parsePlanJson('```json\n{"city":"NYC"}\n```')).toEqual({
      city: "NYC",
    });
  });

  it("strips bare ``` fences", () => {
    expect(parsePlanJson('```\n{"school":"Texas"}\n```')).toEqual({
      school: "Texas",
    });
  });

  it("slices between first { and last } when wrapped in prose", () => {
    expect(parsePlanJson('Here you go: {"city":"Austin"} thanks')).toEqual({
      city: "Austin",
    });
  });

  it("returns {} for unparseable input", () => {
    expect(parsePlanJson("not json at all")).toEqual({});
  });

  it("returns {} for empty input", () => {
    expect(parsePlanJson("")).toEqual({});
  });
});

describe("coerceParams", () => {
  it("keeps trimmed string fields", () => {
    expect(
      coerceParams({ city: "  Dallas ", school: "Texas A&M", intent: "network" })
    ).toEqual({ city: "Dallas", school: "Texas A&M", intent: "network" });
  });

  it("drops empty/whitespace strings", () => {
    expect(coerceParams({ city: "   ", school: "" })).toEqual({});
  });

  it("coerces a numeric-string titanClass and floors it", () => {
    expect(coerceParams({ titanClass: "12.9" }).titanClass).toBe(12);
  });

  it("keeps a numeric titanClass", () => {
    expect(coerceParams({ titanClass: 7 }).titanClass).toBe(7);
  });

  it("drops non-finite or non-positive titanClass", () => {
    expect(coerceParams({ titanClass: "abc" }).titanClass).toBeUndefined();
    expect(coerceParams({ titanClass: 0 }).titanClass).toBeUndefined();
    expect(coerceParams({ titanClass: -3 }).titanClass).toBeUndefined();
  });

  it("accepts a known sector", () => {
    const known = SECTOR_NAMES[0];
    expect(coerceParams({ sector: known }).sector).toBe(known);
  });

  it("drops an unknown sector", () => {
    expect(coerceParams({ sector: "Crypto Bro Inc" }).sector).toBeUndefined();
  });

  it("ignores unrelated keys", () => {
    expect(coerceParams({ foo: "bar", city: "Houston" })).toEqual({
      city: "Houston",
    });
  });

  it("keeps a trimmed seniority and drops an empty one", () => {
    expect(coerceParams({ seniority: "  partner " }).seniority).toBe("partner");
    expect(coerceParams({ seniority: "   " }).seniority).toBeUndefined();
  });
});
