import { describe, expect, it } from "vitest";
import { smartTitle } from "./normalize";

describe("smartTitle", () => {
  it("title-cases a lowercase phrase", () => {
    expect(smartTitle("board member")).toBe("Board Member");
    expect(smartTitle("water to thrive")).toBe("Water to Thrive");
  });

  it("keeps minor words lowercase except when leading", () => {
    expect(smartTitle("teacher retirement system of texas")).toBe(
      "Teacher Retirement System of Texas"
    );
    expect(smartTitle("of counsel")).toBe("Of Counsel");
  });

  it("uppercases curated acronyms regardless of source case", () => {
    expect(smartTitle("kbre properties")).toBe("KBRE Properties");
    expect(smartTitle("assent llc")).toBe("Assent LLC");
    expect(smartTitle("secondee, private equity @ kkr".replace("@ ", ""))).toContain("KKR");
    expect(smartTitle("cfa charterholder")).toBe("CFA Charterholder");
  });

  it("does not over-uppercase word-like company suffixes", () => {
    expect(smartTitle("caltex energy inc.")).toBe("Caltex Energy Inc.");
    expect(smartTitle("acme corp")).toBe("Acme Corp");
  });

  it("capitalizes both sides of an ampersand", () => {
    expect(smartTitle("texas a&m university")).toBe("Texas A&M University");
    expect(smartTitle("r&d lead")).toBe("R&D Lead");
  });

  it("preserves already-correct acronyms and mixed-case names", () => {
    expect(smartTitle("Texas A&M University")).toBe("Texas A&M University");
    expect(smartTitle("McCallum Capital")).toBe("McCallum Capital");
    expect(smartTitle("KKR")).toBe("KKR");
  });

  it("uppercases roman-numeral suffixes", () => {
    expect(smartTitle("partner iii")).toBe("Partner III");
    expect(smartTitle("managing director ii")).toBe("Managing Director II");
  });

  it("preserves surrounding punctuation", () => {
    expect(smartTitle("(former) cfo")).toBe("(Former) CFO");
  });

  it("is idempotent", () => {
    const once = smartTitle("assistant portfolio manager at alpine global management, llc");
    expect(smartTitle(once)).toBe(once);
    expect(once).toBe("Assistant Portfolio Manager at Alpine Global Management, LLC");
  });

  it("handles empty / nullish input", () => {
    expect(smartTitle("")).toBe("");
    expect(smartTitle(null)).toBe("");
    expect(smartTitle(undefined)).toBe("");
  });
});
