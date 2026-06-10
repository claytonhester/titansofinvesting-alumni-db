import { describe, it, expect } from "vitest";
import { parseBoldSegments } from "./markdown-bold";

describe("parseBoldSegments", () => {
  it("returns a single non-bold segment when there are no markers", () => {
    expect(parseBoldSegments("plain text")).toEqual([
      { text: "plain text", bold: false },
    ]);
  });

  it("splits a bold figure in the middle of a sentence", () => {
    expect(parseBoldSegments("verified **87 of 1,056** alumni")).toEqual([
      { text: "verified ", bold: false },
      { text: "87 of 1,056", bold: true },
      { text: " alumni", bold: false },
    ]);
  });

  it("handles multiple bolded figures", () => {
    expect(
      parseBoldSegments("**42%** at MD in **8 years**, **11** partners"),
    ).toEqual([
      { text: "42%", bold: true },
      { text: " at MD in ", bold: false },
      { text: "8 years", bold: true },
      { text: ", ", bold: false },
      { text: "11", bold: true },
      { text: " partners", bold: false },
    ]);
  });

  it("bolds a leading and trailing figure", () => {
    expect(parseBoldSegments("**a** mid **b**")).toEqual([
      { text: "a", bold: true },
      { text: " mid ", bold: false },
      { text: "b", bold: true },
    ]);
  });

  it("treats an unmatched ** as literal text", () => {
    expect(parseBoldSegments("a ** b")).toEqual([{ text: "a ** b", bold: false }]);
  });

  it("returns nothing for an empty string", () => {
    expect(parseBoldSegments("")).toEqual([]);
  });
});
