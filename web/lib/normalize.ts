// Display-layer text normalization. This is the last line of defense for
// professional casing: it runs on whatever the DB holds, so titles, companies,
// degrees, and institutions read cleanly regardless of how a source captured
// them (lowercase scrapes, ALL-CAPS headlines, "kbre", "llc", "texas a&m").
//
// It mirrors pipeline/normalize.py::smart_title so the DB write path and the
// render path agree. The render path is authoritative for what the user sees.

// Minor words stay lowercase unless they open the string.
const MINOR = new Set([
  "a", "an", "the", "and", "but", "or", "for", "nor",
  "on", "at", "to", "by", "in", "of", "up", "as", "vs",
]);

// Roman-numeral suffixes read as uppercase ("iii" -> "III"). Single "i" is
// excluded — it's ambiguous and capitalizes to "I" anyway.
const ROMAN = new Set(["ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x"]);

// Tokens that must render fully uppercase even when a source lowercased them.
// Curated for this finance dataset: credentials, corporate suffixes that are
// genuinely acronyms (LLC/LP — but NOT Inc./Corp./Ltd./Co. which read as words),
// C-suite, and recurring orgs that appear lowercased in scraped text.
const ACRONYMS = new Set([
  // credentials / degrees
  "cfa", "caia", "cpa", "cfp", "frm", "mba", "bba", "emba", "llm", "phd",
  // corporate suffixes that are acronyms
  "llc", "lp", "llp", "plc", "pllc", "lllp",
  // C-suite
  "ceo", "cfo", "coo", "cto", "cio", "cmo", "cro", "evp", "svp",
  // recurring orgs / finance terms seen lowercased in this data
  "kkr", "kbre", "trs", "lse", "ccmp", "reit", "etf", "ipo",
]);

const PUNCT = ".,;:()[]\"'";

interface TokenParts {
  prefix: string;
  core: string;
  suffix: string;
}

// Peel leading/trailing punctuation so we inspect the word itself, e.g.
// "(2004)," -> {prefix:"(", core:"2004", suffix:"),"}.
function splitToken(token: string): TokenParts {
  let start = 0;
  let end = token.length;
  while (start < end && PUNCT.includes(token[start])) start += 1;
  while (end > start && PUNCT.includes(token[end - 1])) end -= 1;
  return {
    prefix: token.slice(0, start),
    core: token.slice(start, end),
    suffix: token.slice(end),
  };
}

// Capitalize each "&"-separated segment so "a&m" -> "A&M", "r&d" -> "R&D",
// while a plain word capitalizes normally ("finance" -> "Finance").
function capitalizeWord(word: string): string {
  return word
    .split("&")
    .map((seg) => (seg ? seg[0].toUpperCase() + seg.slice(1).toLowerCase() : seg))
    .join("&");
}

function isAllCapsAcronym(core: string): boolean {
  return (
    core.length > 1 &&
    /^[A-Za-z]+$/.test(core) &&
    core === core.toUpperCase()
  );
}

function isAlreadyMixedCase(core: string): boolean {
  // An uppercase letter after the first char means the source set it
  // deliberately (McCallum, DeVos, "A&M").
  return /[A-Z]/.test(core.slice(1));
}

/**
 * Title-case a string with investment-domain awareness. Idempotent.
 *
 * Priority: all-caps acronyms and already-mixed-case tokens are preserved;
 * curated acronyms and roman numerals go uppercase; minor words stay lowercase
 * unless first; everything else is capitalized (&-aware).
 *
 * Never pass verbatim source quotes here — they must stay untouched.
 */
export function smartTitle(value: string | null | undefined): string {
  if (!value) return value ?? "";
  const tokens = value.trim().split(/\s+/);
  return tokens
    .map((token, i) => {
      const { prefix, core, suffix } = splitToken(token);
      if (!core) return token;
      if (isAllCapsAcronym(core)) return token;
      if (isAlreadyMixedCase(core)) return token;

      const lower = core.toLowerCase();
      if (ACRONYMS.has(lower)) return prefix + lower.toUpperCase() + suffix;
      if (ROMAN.has(lower)) return prefix + lower.toUpperCase() + suffix;
      if (i > 0 && MINOR.has(lower)) return prefix + lower + suffix;
      return prefix + capitalizeWord(lower) + suffix;
    })
    .join(" ");
}
