import { beforeEach, describe, expect, it, vi } from "vitest";

const dbSearchPeople = vi.fn();
const claimsForSlugs = vi.fn();

vi.mock("@/lib/db", () => ({
  searchPeople: (...args: unknown[]) => dbSearchPeople(...args),
  claimsForSlugs: (...args: unknown[]) => claimsForSlugs(...args),
}));

import { searchPeople } from "./search";

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
