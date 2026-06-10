// Quality filter for the profile page's "Mentions & appearances" links.
//
// public_links is an overloaded bucket: it holds genuine appearances (a firm
// profile, a podcast, a press profile) but ALSO data-broker / people-directory
// pages, social-noise networks, regulatory-filing PDFs, and bare bio headings.
// The user's bar: "I don't want to see basic overview profiles linked." So we
// show only links where the person genuinely appears, dropping the rest.
//
// Host + title rules MIRROR pipeline/news_curate.py
// (_SOCIAL_HOSTS / _DIRECTORY_HOSTS / _BOILERPLATE_TITLE_KW / _is_name_only_title).
// Keep the two in sync — see link-quality.test.ts for the shared expectations.

export interface QualifiableLink {
  label: string;
  url: string;
}

// Hosts that never carry professional signal for this directory: people-search /
// data-broker aggregators (a "basic overview profile") plus social-noise networks.
const DROP_HOSTS = new Set<string>([
  // people-directory / data-broker aggregators
  "theorg.com",
  "advisorcheck.com",
  "indyfin.com",
  "getwarmer.com",
  "app.getwarmer.com",
  "crunchbase.com",
  "zoominfo.com",
  "rocketreach.co",
  "signalhire.com",
  "pitchbook.com",
  "spokeo.com",
  "comparably.com",
  "clay.earth",
  "wwana.com",
  "thealumniassociation.com",
  "zoomgov.com",
  "advisor.investedbetter.com",
  // contact / lead / investor-signal data brokers
  "wiza.co",
  "signal.nfx.com",
  "me.sh",
  "evalyze.ai",
  "lusha.com",
  "apollo.io",
  "contactout.com",
  "leadiq.com",
  "seamless.ai",
  "usebadges.com",
  // social-noise networks (not a professional profile)
  "klout.com",
  "foursquare.com",
  "pinterest.com",
  "tiktok.com",
  "threads.net",
  "reddit.com",
  "medium.com",
  "loopnet.com",
  // public-records / government-salary databases — a transparency disclosure, not a
  // personal appearance (mirrors pipeline news_curate._PUBLIC_RECORDS_HOSTS)
  "texastaxpayers.com",
  "governmentsalaries.com",
  "govsalaries.com",
  "openpayrolls.com",
  "transparentcalifornia.com",
  "openthebooks.com",
]);

// Title fragments that mark firm boilerplate / filings / directory headings —
// never a person-specific appearance.
const BOILERPLATE_TITLE_KW = [
  "meet our team",
  "our team",
  "meet the team",
  "team members",
  "our people",
  "leadership team",
  "management team",
  "company overview",
  "about us",
  "about the firm",
  "company profile",
  "firm overview",
  "our firm",
  "form adv",
  "brochure supplement",
  "part 2b",
  "prospectus",
  "fact sheet",
  "annual report",
  "[pdf]",
  "privacy policy",
  "terms of service",
];

// Credentials / generation suffixes that may trail a name on a bio heading
// ("Ross Willmann, CFA") without making it an article.
const CREDENTIAL_TOKENS = new Set<string>([
  "cfa", "cpa", "cfp", "caia", "frm", "mba", "phd", "jd", "cma",
  "jr", "sr", "ii", "iii", "iv", "md", "esq", "cef", "chfc", "clu",
]);

function hostOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "").toLowerCase();
  } catch {
    return "";
  }
}

// True when a host is on the drop list — matching either the full host or its
// registrable domain. DROP_HOSTS holds both bare domains ("apollo.io") and some
// broker sub-domains ("signal.nfx.com"), so we test BOTH forms: this drops
// app.apollo.io (via registrable "apollo.io") AND signal.nfx.com (via full host),
// closing the sub-domain bypass without losing the sub-domain-specific entries.
function isDroppedHost(host: string): boolean {
  if (!host) return false;
  if (DROP_HOSTS.has(host)) return true;
  const registrable = host.split(".").slice(-2).join(".");
  return DROP_HOSTS.has(registrable);
}

// True when a URL's host is a data-broker / records / social-noise host that should
// not be shown as a provenance source on the profile's "Sourced from" panel. Reuses
// the same drop list as the appearance-link filter so the two stay consistent.
export function isLowValueSourceUrl(url: string): boolean {
  return isDroppedHost(hostOf(url));
}

function isBoilerplateTitle(title: string): boolean {
  const t = title.toLowerCase();
  return BOILERPLATE_TITLE_KW.some((kw) => t.includes(kw));
}

function words(s: string): string[] {
  return (s.toLowerCase().match(/[a-z]+/g) ?? []);
}

// A heading that is just the person's name (± credentials) is a bio/profile page,
// never an appearance — e.g. "Ross Willmann, CFA" on his own firm's site.
export function isNameOnlyTitle(title: string, name: string): boolean {
  const nameTokens = new Set(words(name));
  if (nameTokens.size === 0) return false;
  const head = words(title);
  if (head.length === 0) return false;
  return head.every((t) => nameTokens.has(t) || CREDENTIAL_TOKENS.has(t));
}

function canonicalUrl(url: string): string {
  try {
    const u = new URL(url);
    return `${u.hostname.replace(/^www\./, "")}${u.pathname.replace(/\/$/, "")}`.toLowerCase();
  } catch {
    return url.trim().toLowerCase();
  }
}

/**
 * Keep only genuinely useful appearance links. Drops data-broker / directory /
 * social-noise hosts, firm boilerplate + filings, name-only bio headings, and
 * LinkedIn (shown separately as the header button). De-duplicates by URL.
 */
export function usefulLinks<T extends QualifiableLink>(
  links: readonly T[],
  personName: string
): T[] {
  const seen = new Set<string>();
  const out: T[] = [];
  for (const link of links) {
    const url = link.url ?? "";
    const label = link.label ?? "";
    const host = hostOf(url);
    const hay = `${label} ${url}`.toLowerCase();

    if (hay.includes("linkedin.com")) continue; // redundant with header button
    if (isDroppedHost(host)) continue;
    // Some links carry a bare URL as their "label" (no human title was scraped) —
    // low-quality and frequently a wrong-person aggregator link. A URL is never a
    // useful appearance title, so drop it.
    if (/^https?:\/\//i.test(label.trim())) continue;
    if (isBoilerplateTitle(label)) continue;
    if (isNameOnlyTitle(label, personName)) continue;

    const key = canonicalUrl(url);
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(link);
  }
  return out;
}
