import { describe, expect, it } from "vitest";
import type { Claim, Person } from "./db";
import { buildResume, reconcileCurrentTitle, splitTitleEmployer } from "./resume";

interface E {
  title: string;
  company: string;
  start: string | null;
  end: string | null;
  current: boolean;
  confidence: number;
  sourceUrl: string;
}
function entry(title: string, company: string, current: boolean, end: string | null = null): E {
  return { title, company, start: "2020", end, current, confidence: 0.9, sourceUrl: "" };
}

describe("reconcileCurrentTitle", () => {
  it("replaces a stale title that matches no role at the current employer", () => {
    const hist = [entry("Vice President, Marketing", "Facteus", true)];
    expect(reconcileCurrentTitle("PhD Student", "Facteus", hist as never)).toBe(
      "Vice President, Marketing"
    );
  });
  it("keeps the title when it matches a role at the employer", () => {
    const hist = [entry("Senior Vice President, Customer Success", "QGenda", true)];
    // current_title is a substring of the timeline role -> recognized as the same
    // role, so it is kept as-is (no override).
    expect(
      reconcileCurrentTitle("Vice President, Customer Success", "QGenda", hist as never)
    ).toBe("Vice President, Customer Success");
  });
  it("does nothing when there is no CURRENT role at the employer", () => {
    const hist = [entry("Analyst", "Old Co", false, "2022")];
    expect(reconcileCurrentTitle("Founder", "New Co", hist as never)).toBe("Founder");
  });
  it("is a no-op without an employer", () => {
    expect(reconcileCurrentTitle("Analyst", null, [])).toBe("Analyst");
  });
});

describe("splitTitleEmployer", () => {
  it("splits an employer packed into the title when employer is empty", () => {
    expect(splitTitleEmployer("Consultant at Boston Consulting Group", null)).toEqual([
      "Consultant",
      "Boston Consulting Group",
    ]);
  });
  it("splits on the LAST ' at ' so multi-at titles resolve", () => {
    expect(splitTitleEmployer("Head of M&A at Apollo", "")).toEqual([
      "Head of M&A",
      "Apollo",
    ]);
  });
  it("keeps the title whole when a real employer already exists", () => {
    expect(splitTitleEmployer("Consultant at BCG", "BCG")).toEqual([
      "Consultant at BCG",
      "BCG",
    ]);
  });
  it("leaves a title without ' at ' untouched", () => {
    expect(splitTitleEmployer("Senior Associate", null)).toEqual([
      "Senior Associate",
      null,
    ]);
  });
});

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

  it("coalesces the same role described two ways (Citi / Citigroup in New York)", () => {
    const { experience, experienceGroups } = build([
      career("Investment Banking Analyst at Citi (2015-2017)"),
      career(
        "Investment Banking Analyst in the Financial Institutions Group at Citigroup in New York (2015-2017)"
      ),
    ]);
    // One real role, not two — same dates, compatible title/company.
    expect(experience).toHaveLength(1);
    expect(experienceGroups).toHaveLength(1);
    // Keeps the more specific title and the cleaner (shorter) company.
    expect(experience[0].title).toBe(
      "Investment Banking Analyst in the Financial Institutions Group"
    );
    expect(experience[0].company).toBe("Citi");
  });

  it("does NOT coalesce a promotion at the same employer (different dates)", () => {
    const { experience } = build([
      career("Associate at Berkshire Partners (2021-2023)"),
      career("Senior Associate at Berkshire Partners (2023-2024)"),
    ]);
    // Different date ranges => two distinct roles, even though 'Associate' is a
    // token-run of 'Senior Associate'.
    expect(experience).toHaveLength(2);
  });

  it("does NOT coalesce two same-date roles at unrelated firms", () => {
    const { experience } = build([
      career("Analyst at Goldman Sachs (2015-2017)"),
      career("Analyst at Morgan Stanley (2015-2017)"),
    ]);
    expect(experience).toHaveLength(2);
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

  it("parses em-dash (U+2014) year ranges without losing dates or polluting company", () => {
    const { experienceGroups } = build([
      career("Senior Manager at Company (2015—2017)"),
    ]);
    expect(experienceGroups).toHaveLength(1);
    expect(experienceGroups[0].company).toBe("Company");
    expect(experienceGroups[0].roles[0].start).toBe("2015");
    expect(experienceGroups[0].roles[0].end).toBe("2017");
  });
});

describe("buildResume sources panel", () => {
  it("excludes public-records / data-broker hosts from the 'Sourced from' list", () => {
    const claims: Claim[] = [
      {
        claim_type: "education",
        value: "BBA From Baylor University",
        source_url: "https://baylor.edu/profile",
        quote: "",
        confidence: 0.9,
        extraction_method: "test",
      },
      {
        claim_type: "public_links",
        value: "Highest Paid State Employees",
        source_url: "https://www.texastaxpayers.com/highest-paid",
        quote: "",
        confidence: 0.9,
        extraction_method: "test",
      },
    ];
    const { sources } = buildResume(PERSON, claims);
    expect(sources.some((u) => u.includes("texastaxpayers"))).toBe(false);
    expect(sources.some((u) => u.includes("baylor.edu"))).toBe(true);
  });
});

describe("reconcileCurrentTitle with multiple current roles", () => {
  it("picks the most recently-started role when several are current at one employer", () => {
    const hist = [
      { title: "Vice President", company: "Acme", start: "2015", end: null, current: true, confidence: 0.9, sourceUrl: "" },
      { title: "Managing Director", company: "Acme", start: "2022", end: null, current: true, confidence: 0.9, sourceUrl: "" },
    ];
    expect(reconcileCurrentTitle("Analyst", "Acme", hist as never)).toBe("Managing Director");
  });
});
