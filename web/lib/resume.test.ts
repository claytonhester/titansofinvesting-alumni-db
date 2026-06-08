import { describe, expect, it } from "vitest";
import type { Claim, Person } from "./db";
import { buildResume } from "./resume";

const PERSON: Person = {
  id: 1,
  full_name: "Test Person",
  name_slug: "test-person",
  titan_class: 0,
  school: "Texas A&M",
  initial_company: "Acme",
  city: "Austin",
  source_url: "https://example.test/dir",
  needs_review: 0,
};

function career(value: string, quote = "", confidence = 0.8): Claim {
  return {
    claim_type: "career_history",
    value,
    source_url: "https://example.test/src",
    quote,
    confidence,
    extraction_method: "test",
  };
}

function build(claims: Claim[]) {
  return buildResume(PERSON, claims);
}

describe("buildResume experience dedup + grouping", () => {
  it("drops a dateless duplicate of a dated role, keeping the dated one", () => {
    const { experienceGroups } = build([
      career("Vice President at Barclays (2009-2009)"),
      career("Vice President at Barclays"),
    ]);
    expect(experienceGroups).toHaveLength(1);
    expect(experienceGroups[0].company).toBe("Barclays");
    expect(experienceGroups[0].roles).toHaveLength(1);
    expect(experienceGroups[0].roles[0].start).toBe("2009");
  });

  it("treats a single-year form as the same role as its date-range twin", () => {
    const { experienceGroups } = build([
      career("Secondee, Private Equity at KKR (2019)"),
      career("Secondee, Private Equity at KKR (2019-2019)"),
    ]);
    expect(experienceGroups).toHaveLength(1);
    expect(experienceGroups[0].roles).toHaveLength(1);
  });

  it("dedupes across case/punctuation differences in title or company", () => {
    const { experienceGroups } = build([
      career("Senior Analyst at Och-Ziff Capital Management"),
      career("Senior Analyst at Och-ziff Capital Management (2006-2009)"),
    ]);
    expect(experienceGroups).toHaveLength(1);
    expect(experienceGroups[0].roles).toHaveLength(1);
  });

  it("groups multiple distinct roles at one employer into a single card", () => {
    const { experienceGroups } = build([
      career("Investment Manager at Teacher Retirement System of Texas (2015-2018)"),
      career(
        "Director, Private Equity Principal Investments at Teacher Retirement System of Texas (2020-present)"
      ),
    ]);
    expect(experienceGroups).toHaveLength(1);
    expect(experienceGroups[0].company).toBe("Teacher Retirement System of Texas");
    expect(experienceGroups[0].roles).toHaveLength(2);
  });

  it("merges a parenthetical company variant into its dated group", () => {
    const { experienceGroups } = build([
      career("Investment Manager at Teacher Retirement System of Texas (2017-2020)"),
      career("Allocator at Texas Teachers (Teacher Retirement System of Texas)"),
    ]);
    expect(experienceGroups).toHaveLength(1);
    expect(experienceGroups[0].company).toBe("Teacher Retirement System of Texas");
    const titles = experienceGroups[0].roles.map((r) => r.title);
    expect(titles).toContain("Investment Manager");
    expect(titles).toContain("Allocator");
  });

  it("parses a dateless 'Title at Company' so the company shows", () => {
    const { experienceGroups } = build([
      career("Managing Partner / Co-Founder at Kortright Capital"),
    ]);
    expect(experienceGroups[0].company).toBe("Kortright Capital");
    expect(experienceGroups[0].roles[0].title).toBe("Managing Partner / Co-Founder");
  });

  it("drops a prose entry that names a company already dated, keeps a new one", () => {
    const { experienceGroups } = build([
      career("Investment Banking Analyst at Lehman Brothers (2006-2009)"),
      career("Investment Banking Division of Lehman Brothers"),
      career("Chief Financial Officer of Falcon Minerals (NASDAQ: FLMN)"),
    ]);
    const labels = experienceGroups.map((g) =>
      g.roles.length === 1 && !g.roles[0].company ? g.roles[0].title : g.company
    );
    expect(labels).toContain("Lehman Brothers");
    expect(
      experienceGroups.some((g) =>
        g.roles.some((r) => r.title.includes("Falcon Minerals"))
      )
    ).toBe(true);
    // The Lehman prose restatement is gone — only the dated Lehman role remains.
    expect(
      experienceGroups.some((g) =>
        g.roles.some((r) => r.title.includes("Investment Banking Division"))
      )
    ).toBe(false);
  });

  it("does not merge two genuinely different employers", () => {
    const { experienceGroups } = build([
      career("Analyst at Goldman Sachs (2004-2006)"),
      career("Vice President at Barclays (2009-2009)"),
    ]);
    expect(experienceGroups).toHaveLength(2);
  });
});
