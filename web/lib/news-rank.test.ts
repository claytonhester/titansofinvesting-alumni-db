import { describe, it, expect } from "vitest";
import { byEditorialRank, editorialScore } from "./news-rank";
import type { NewsCategory, NewsItem } from "./news-types";

function item(category: NewsCategory, relevance: number, date = ""): NewsItem {
  return {
    id: Math.random().toString(36),
    nameSlug: "s",
    personName: "n",
    school: "sch",
    titanClass: 1,
    category,
    headline: "h",
    snippet: "",
    sourceUrl: "",
    sourceHost: "",
    date,
    relevance,
  };
}

describe("news editorial rank (All feed)", () => {
  it("ranks a Recognition above a routine, higher-importance Leadership Move", () => {
    const recog = item("Recognition", 0.6);
    const move = item("Leadership Moves", 0.75);
    expect([move, recog].sort(byEditorialRank)[0]).toBe(recog);
  });

  it("keeps Recognition on top even vs a max-importance Leadership Move", () => {
    const recog = item("Recognition", 0.6); // 1.6
    const maxMove = item("Leadership Moves", 1.0); // 1.3
    expect([maxMove, recog].sort(byEditorialRank)[0]).toBe(recog);
  });

  it("lets a major Leadership Move outrank a weak Market View", () => {
    const bigMove = item("Leadership Moves", 0.95); // 1.25
    const weakView = item("Market Views", 0.4); // 0.85
    expect([weakView, bigMove].sort(byEditorialRank)[0]).toBe(bigMove);
  });

  it("orders Recognition > Funding > Market > Leadership at equal importance", () => {
    const r = item("Recognition", 0.7);
    const f = item("Funding & Deals", 0.7);
    const m = item("Market Views", 0.7);
    const l = item("Leadership Moves", 0.7);
    const sorted = [l, m, r, f].sort(byEditorialRank).map((x) => x.category);
    expect(sorted).toEqual([
      "Recognition",
      "Funding & Deals",
      "Market Views",
      "Leadership Moves",
    ]);
  });

  it("breaks ties within a category by recency (newest first)", () => {
    const older = item("Recognition", 0.7, "2024-01-01");
    const newer = item("Recognition", 0.7, "2025-01-01");
    expect([older, newer].sort(byEditorialRank)[0]).toBe(newer);
  });

  it("editorialScore blends category weight + importance", () => {
    expect(editorialScore({ category: "Recognition", relevance: 0.5 })).toBeCloseTo(1.5);
    expect(editorialScore({ category: "Leadership Moves", relevance: 0.5 })).toBeCloseTo(0.8);
  });
});
