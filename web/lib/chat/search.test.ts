import { beforeEach, describe, expect, it, vi } from "vitest";

const dbSearchPeople = vi.fn();
const claimsForSlugs = vi.fn();
const peopleBySlugs = vi.fn();
const semanticRankSlugs = vi.fn();

vi.mock("@/lib/db", () => ({
  searchPeople: (...args: unknown[]) => dbSearchPeople(...args),
  claimsForSlugs: (...args: unknown[]) => claimsForSlugs(...args),
  peopleBySlugs: (...args: unknown[]) => peopleBySlugs(...args),
}));

vi.mock("./semantic", () => ({
  semanticRankSlugs: (...args: unknown[]) => semanticRankSlugs(...args),
}));

import { searchPeople, retrievePeople } from "./search";

function person(over: Record<string, unknown> = {}) {
  return {
    full_name: "Jane Doe",
    name_slug: "jane-doe",
    titan_class: 12,
    school: "Texas",
    initial_company: "Acme Capital",
    city: "Dallas",
    source_url: "https://example.com/jane",
    ...over,
  };
}

describe("searchPeople", () => {
  beforeEach(() => {
    dbSearchPeople.mockReset();
    claimsForSlugs.mockReset();
    peopleBySlugs.mockReset();
    semanticRankSlugs.mockReset();
  });

  it("passes params through to the db with the bounded limit", () => {
    dbSearchPeople.mockReturnValue([]);
    searchPeople({ city: "Dallas", sector: "Hedge Funds & Asset Mgmt" });
    expect(dbSearchPeople).toHaveBeenCalledWith(
      expect.objectContaining({ city: "Dallas", limit: 12 })
    );
  });

  it("returns [] and skips the claims query when no rows match", () => {
    dbSearchPeople.mockReturnValue([]);
    expect(searchPeople({ city: "Nowhere" })).toEqual([]);
    expect(claimsForSlugs).not.toHaveBeenCalled();
  });

  it("folds claims onto the matching person by slug", () => {
    dbSearchPeople.mockReturnValue([
      person({ name_slug: "jane-doe" }),
      person({ full_name: "Bob Roe", name_slug: "bob-roe" }),
    ]);
    claimsForSlugs.mockReturnValue([
      {
        name_slug: "jane-doe",
        claim_type: "role",
        value: "Partner",
        source_url: "https://example.com/role",
      },
    ]);

    const result = searchPeople({ city: "Dallas" });

    expect(claimsForSlugs).toHaveBeenCalledWith(["jane-doe", "bob-roe"]);
    expect(result[0].claims).toEqual([
      {
        claim_type: "role",
        value: "Partner",
        source_url: "https://example.com/role",
      },
    ]);
    expect(result[1].claims).toEqual([]);
  });
});

describe("retrievePeople (hybrid)", () => {
  beforeEach(() => {
    dbSearchPeople.mockReset();
    claimsForSlugs.mockReset();
    peopleBySlugs.mockReset();
    semanticRankSlugs.mockReset();
    claimsForSlugs.mockReturnValue([]);
  });

  it("leads with keyword rows when structured filters are present, semantic supplements", async () => {
    dbSearchPeople.mockReturnValue([person({ name_slug: "kw-1" })]);
    semanticRankSlugs.mockResolvedValue(["sem-1"]);
    peopleBySlugs.mockReturnValue([person({ name_slug: "sem-1" })]);

    const result = await retrievePeople({ sector: "Investment Banking" }, "any");

    expect(result.map((r) => r.name_slug)).toEqual(["kw-1", "sem-1"]);
  });

  it("leads with semantic rows for a pure natural-language query (no filters)", async () => {
    dbSearchPeople.mockReturnValue([person({ name_slug: "kw-1" })]);
    semanticRankSlugs.mockResolvedValue(["sem-1"]);
    peopleBySlugs.mockReturnValue([person({ name_slug: "sem-1" })]);

    const result = await retrievePeople({}, "who can advise on climate tech");

    expect(result.map((r) => r.name_slug)).toEqual(["sem-1", "kw-1"]);
  });

  it("dedupes a person matched by both paths", async () => {
    dbSearchPeople.mockReturnValue([person({ name_slug: "dup" })]);
    semanticRankSlugs.mockResolvedValue(["dup"]);
    peopleBySlugs.mockReturnValue([person({ name_slug: "dup" })]);

    const result = await retrievePeople({}, "q");

    expect(result.map((r) => r.name_slug)).toEqual(["dup"]);
  });

  it("falls back to keyword rows when semantic returns nothing", async () => {
    dbSearchPeople.mockReturnValue([person({ name_slug: "kw-1" })]);
    semanticRankSlugs.mockResolvedValue([]);

    const result = await retrievePeople({}, "q");

    expect(result.map((r) => r.name_slug)).toEqual(["kw-1"]);
    expect(peopleBySlugs).not.toHaveBeenCalled();
  });
});
