import { describe, expect, it } from "vitest";
import type { Claim } from "./db";
import { groupEducation } from "./education";

function edu(value: string, confidence = 0.8): Claim {
  return {
    claim_type: "education",
    value,
    source_url: "https://example.test/src",
    quote: "",
    confidence,
    extraction_method: "test",
  };
}

describe("groupEducation", () => {
  it("collapses multiple degrees from the same institution onto one card", () => {
    const groups = groupEducation([
      edu("Master of Science in Finance From Texas A&M University"),
      edu("Bachelor of Business Administration in Marketing From Texas A&M University"),
    ]);
    expect(groups).toHaveLength(1);
    expect(groups[0].institution).toBe("Texas A&M University");
    expect(groups[0].degrees).toEqual([
      "Master of Science in Finance",
      "Bachelor of Business Administration in Marketing",
    ]);
  });

  it("merges a McCombs MBA and a University of Texas MBA into one card+degree", () => {
    const groups = groupEducation([
      edu("Master of Business Administration From Texas McCombs School of Business"),
      edu("MBA From The University of Texas"),
    ]);
    expect(groups).toHaveLength(1);
    expect(groups[0].institution).toBe("The University of Texas at Austin");
    // "MBA" and "Master of Business Administration" are the same degree — one row.
    expect(groups[0].degrees).toEqual(["Master of Business Administration"]);
  });

  it("collapses a bare degree level into its descriptive twin (same school)", () => {
    const groups = groupEducation([
      edu("MBA From Rice University"),
      edu("Master of Business Administration in Real Estate Finance From Rice University"),
    ]);
    expect(groups).toHaveLength(1);
    expect(groups[0].degrees).toEqual([
      "Master of Business Administration in Real Estate Finance",
    ]);
  });

  it("keeps two same-level degrees with distinct fields apart", () => {
    const groups = groupEducation([
      edu("Bachelor of Science in Mathematics From Rice University"),
      edu("Bachelor of Science in Physics From Rice University"),
    ]);
    expect(groups).toHaveLength(1);
    expect(groups[0].degrees).toHaveLength(2);
  });

  it("keeps genuinely different degree levels at one school", () => {
    const groups = groupEducation([
      edu("Bachelor of Business Administration From Texas A&M University"),
      edu("Master of Science From Texas A&M University"),
    ]);
    expect(groups).toHaveLength(1);
    expect(groups[0].degrees).toHaveLength(2);
  });

  it("drops a bare-institution duplicate when a degreed entry exists", () => {
    const groups = groupEducation([
      edu("Bachelor of Business Administration From Texas A&m University"),
      edu("Texas A&m University"),
      edu("Mays Business School - Texas A&m University"),
    ]);
    expect(groups).toHaveLength(1);
    expect(groups[0].institution).toBe("Texas A&M University");
    expect(groups[0].degrees).toEqual(["Bachelor of Business Administration"]);
  });

  it("keeps different institutions on separate cards", () => {
    const groups = groupEducation([
      edu("Bachelor of Business Administration From Texas A&M University"),
      edu("Peking University"),
    ]);
    const names = groups.map((g) => g.institution).sort();
    expect(names).toEqual(["Peking University", "Texas A&M University"]);
  });

  it("gives credentials their own card with no degree", () => {
    const groups = groupEducation([
      edu("CFA Charterholder"),
      edu("CAIA Charterholder"),
    ]);
    expect(groups).toHaveLength(2);
    expect(groups.every((g) => g.degrees.length === 0)).toBe(true);
    expect(groups.map((g) => g.institution).sort()).toEqual([
      "CAIA Charterholder",
      "CFA Charterholder",
    ]);
  });

  it("parses the 'Institution, Degree, honors' comma shape", () => {
    const groups = groupEducation([
      edu("Texas A&M University, BBA in Finance With Minor in Psychology, Magna Cum Laude"),
    ]);
    expect(groups[0].institution).toBe("Texas A&M University");
    expect(groups[0].degrees[0]).toContain("BBA in Finance");
  });

  it("strips trailing year/honors parentheticals", () => {
    const groups = groupEducation([
      edu("Bachelor of Business Administration (BBA) in Finance From Mays Business School at Texas A&M University (2005-2009)"),
    ]);
    expect(groups[0].institution).toBe("Texas A&M University");
    expect(groups[0].degrees[0]).not.toContain("2005");
  });

  it("groups a sub-school alias under its parent institution", () => {
    const groups = groupEducation([
      edu("BBA From Texas A&M University"),
      edu("Bachelor of Business Administration From Arch H. Aplin III Department of Marketing | Mays Business School"),
    ]);
    expect(groups).toHaveLength(1);
    expect(groups[0].institution).toBe("Texas A&M University");
  });

  it("dedupes identical degree strings case-insensitively", () => {
    const groups = groupEducation([
      edu("Bachelor of Business Administration From Texas A&M University"),
      edu("bachelor of business administration from texas a&m university"),
    ]);
    expect(groups).toHaveLength(1);
    expect(groups[0].degrees).toEqual(["Bachelor of Business Administration"]);
  });

  it("returns no cards for an empty claim list", () => {
    expect(groupEducation([])).toEqual([]);
  });
});
