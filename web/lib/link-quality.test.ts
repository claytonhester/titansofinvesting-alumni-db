import { describe, expect, it } from "vitest";
import { isNameOnlyTitle, usefulLinks } from "./link-quality";

describe("isNameOnlyTitle", () => {
  it("flags a bare name + credentials as a bio heading", () => {
    expect(isNameOnlyTitle("Ross Willmann, CFA", "Ross Willmann")).toBe(true);
    expect(isNameOnlyTitle("Ross Willmann", "Ross Willmann")).toBe(true);
    expect(isNameOnlyTitle("Komson Silapachai, CFA®", "Komson Silapachai")).toBe(true);
  });

  it("keeps a real headline with words beyond the name", () => {
    expect(isNameOnlyTitle("Ross Willmann Named CIO of the Year", "Ross Willmann")).toBe(false);
    expect(isNameOnlyTitle("Podcast with Komson Silapachai on ETFs", "Komson Silapachai")).toBe(false);
  });
});

describe("usefulLinks", () => {
  const name = "Ross Willmann";

  it("drops data-broker / directory / social-noise hosts", () => {
    const links = [
      { label: "Ross profile", url: "https://www.advisorcheck.com/ross" },
      { label: "Ross", url: "https://indyfin.com/advisor/ross" },
      { label: "Ross", url: "https://app.getwarmer.com/ross" },
      { label: "Ross on TheOrg", url: "https://theorg.com/org/x/ross" },
      { label: "Klout", url: "https://klout.com/ross" },
      { label: "Pinterest", url: "https://pinterest.com/ross" },
      { label: "LoopNet listing", url: "https://www.loopnet.com/ross" },
    ];
    expect(usefulLinks(links, name)).toEqual([]);
  });

  it("drops the redundant LinkedIn link (header button covers it)", () => {
    const links = [{ label: "LinkedIn", url: "https://www.linkedin.com/in/ross" }];
    expect(usefulLinks(links, name)).toEqual([]);
  });

  it("drops firm boilerplate, filings, and name-only bio headings", () => {
    const links = [
      { label: "Meet Our Team", url: "https://firm.com/team" },
      { label: "[PDF] Form ADV Part 2B", url: "https://firm.com/adv.pdf" },
      { label: "Company Overview", url: "https://firm.com/about" },
      { label: "Ross Willmann, CFA", url: "https://warwickpartners.net/ross" },
    ];
    expect(usefulLinks(links, name)).toEqual([]);
  });

  it("keeps genuine appearances (firm bio with a real title, podcast, press)", () => {
    const links = [
      { label: "Emily Jugle - TPH&Co.", url: "https://www.tphco.com/team/emily" },
      { label: "Podcast: Using ETFs", url: "https://www.etf.com/podcasts/x" },
      { label: "Brighton Park Capital Announces Promotions", url: "https://www.bpc.com/insights/y" },
    ];
    expect(usefulLinks(links, "Emily Jugle").length).toBe(3);
  });

  it("de-duplicates by canonical URL (trailing slash + www)", () => {
    const links = [
      { label: "Profile", url: "https://www.firm.com/team/jane/" },
      { label: "Profile again", url: "https://firm.com/team/jane" },
    ];
    expect(usefulLinks(links, "Jane Doe").length).toBe(1);
  });

  it("drops newer data brokers (wiza, nfx signal, evalyze, me.sh)", () => {
    const links = [
      { label: "Eric Heglie - Partner at IGP", url: "https://wiza.co/d/x" },
      { label: "Jane on NFX", url: "https://signal.nfx.com/investors/jane" },
      { label: "Jane | Evalyze", url: "https://evalyze.ai/p/jane" },
      { label: "Jane profile", url: "https://me.sh/profile/jane" },
    ];
    expect(usefulLinks(links, "Jane Doe")).toEqual([]);
  });

  it("drops public-records / salary-database hosts (texastaxpayers.com)", () => {
    // A government-salary disclosure is not a personal appearance; surfacing it
    // (even just the link) is the same privacy/quality problem as in the news feed.
    const links = [
      {
        label: "Majority of Highest Paid State Employees Work for TRS",
        url: "https://www.texastaxpayers.com/majority-of-highest-paid-state-employees/",
      },
    ];
    expect(usefulLinks(links, "Kimberly Carey")).toEqual([]);
  });

  it("drops data-broker SUBDOMAINS (app./api./profiles.)", () => {
    // hostOf must compare the registrable domain, not just strip www. — otherwise
    // app.apollo.io / api.crunchbase.com slip past the DROP_HOSTS check.
    const links = [
      { label: "Jane on Apollo", url: "https://app.apollo.io/contact/jane" },
      { label: "Jane on CrunchBase", url: "https://api.crunchbase.com/person/jane" },
      { label: "Jane on ZoomInfo", url: "https://profiles.zoominfo.com/jane" },
    ];
    expect(usefulLinks(links, "Jane Doe")).toEqual([]);
  });

  it("drops a link whose label is a bare URL (no real title)", () => {
    const links = [
      { label: "https://www.instagram.com/nick ganye", url: "https://forbes.com/x" },
    ];
    expect(usefulLinks(links, "Nicholas Gagnet")).toEqual([]);
  });

  it("tolerates malformed urls without throwing", () => {
    const links = [{ label: "broken", url: "not a url" }];
    expect(() => usefulLinks(links, name)).not.toThrow();
  });
});
