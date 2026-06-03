import { anthropic, HAIKU_MODEL } from "./anthropic";
import type { ChatTurn } from "./plan";
import type { RetrievedPerson } from "./search";

export type StreamEvent =
  | { type: "text"; text: string }
  | { type: "usage"; usage: { input_tokens: number; output_tokens: number } };

// Static system prefix — cached (ephemeral). Encodes the hard grounding rule,
// topic restriction, link format, and tone. The model may ONLY use the rows we
// hand it; it must never assert a career fact not present in those rows.
const SYNTH_SYSTEM = `You are the Titans of Investing alumni concierge. You help a visitor find specific alumni to connect with, name the firms/organizations those alumni are at, and offer brief, practical framing.

ABSOLUTE RULES:
- Use ONLY the alumni records provided to you in the user message. NEVER use outside knowledge about any person, firm, or industry.
- NEVER invent names, employers, titles, cities, or career facts. If a detail is not in the records, do not state it.
- When the provided records are thin or empty, say so plainly (e.g. "The directory is thin on this right now") and offer what little is grounded, rather than guessing.
- Stay strictly on the topic of Titans of Investing alumni, their careers, firms, and professional connections. For anything off-topic, briefly redirect.

ANSWER STYLE:
- Recommend a few specific alumni by name. For each, link to their profile using markdown exactly like: [Full Name](/person/name-slug) using the record's name_slug.
- When a record carries enriched detail (current_title, current_employer, career_history, education, location), lead with the person's current role and ground your framing in that detail — it is richer and more current than their first employer.
- When a record has only base fields, name their first employer and say what little is known, rather than padding it out.
- Keep it concise and warm — a short intro line, then the recommendations, then one line of framing or a next step.
- Do not output JSON. Write for a person.`;

const SYNTH_MAX_TOKENS = 500;

// News mentions come from an unverified name-search and are kept out of the
// website's verified résumé; the chat excludes them from grounding for the same
// reason — a namesake's headline must never become a stated career fact.
const NEWS_CLAIM = "news_mention";

// Claim types rendered first, in résumé order, so the model leads with a
// person's current role and works backwards — mirroring buildResume's ordering.
const CLAIM_ORDER = [
  "current_title",
  "current_employer",
  "career_history",
  "education",
  "location",
  "short_bio",
];

function orderClaims(
  claims: RetrievedPerson["claims"]
): RetrievedPerson["claims"] {
  const rank = (t: string): number => {
    const i = CLAIM_ORDER.indexOf(t);
    return i === -1 ? CLAIM_ORDER.length : i;
  };
  return [...claims].sort((a, b) => rank(a.claim_type) - rank(b.claim_type));
}

// Render the full verified claim set per person (no truncation). The retrieved
// set is bounded (≤12 rows, and only ~8 alumni are enriched at all), so the
// structured detail stays well within the cached-prefix token budget.
function personBlock(r: RetrievedPerson): string {
  const city = r.city && r.city !== "(unknown)" ? r.city : "city unknown";
  const header = `- ${r.full_name} | slug: ${r.name_slug} | first employer: ${r.initial_company} | ${r.school} Titans ${r.titan_class} | ${city}`;

  const verified = orderClaims(
    r.claims.filter((c) => c.claim_type !== NEWS_CLAIM)
  );
  if (verified.length === 0) return header;

  const detail = verified
    .map((c) => `    • ${c.claim_type}: ${c.value}`)
    .join("\n");
  return `${header}\n${detail}`;
}

function rowsToContext(rows: RetrievedPerson[]): string {
  if (rows.length === 0) {
    return "ALUMNI RECORDS: (none matched — the directory has no grounded matches for this query)";
  }
  return `ALUMNI RECORDS (use ONLY these):\n${rows.map(personBlock).join("\n")}`;
}

// Stream the grounded answer token-by-token. Yields text deltas as they arrive,
// then a final usage event so the caller can log cost.
export async function* streamAnswer(
  history: ChatTurn[],
  rows: RetrievedPerson[]
): AsyncGenerator<StreamEvent> {
  const context = rowsToContext(rows);
  const priorTurns = history.slice(0, -1);
  const latest = history[history.length - 1]?.content ?? "";

  const messages = [
    ...priorTurns.map((t) => ({ role: t.role, content: t.content })),
    {
      role: "user" as const,
      content: `${context}\n\nVISITOR QUESTION: ${latest}`,
    },
  ];

  const stream = anthropic().messages.stream({
    model: HAIKU_MODEL,
    max_tokens: SYNTH_MAX_TOKENS,
    system: [
      { type: "text", text: SYNTH_SYSTEM, cache_control: { type: "ephemeral" } },
    ],
    messages,
  });

  for await (const event of stream) {
    if (
      event.type === "content_block_delta" &&
      event.delta.type === "text_delta"
    ) {
      yield { type: "text", text: event.delta.text };
    }
  }

  const final = await stream.finalMessage();
  yield {
    type: "usage",
    usage: {
      input_tokens: final.usage.input_tokens,
      output_tokens: final.usage.output_tokens,
    },
  };
}
