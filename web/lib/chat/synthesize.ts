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
- Name the firm/organization from the record's initial_company.
- Keep it concise and warm — a short intro line, then the recommendations, then one line of framing or a next step.
- Do not output JSON. Write for a person.`;

const SYNTH_MAX_TOKENS = 500;

function rowsToContext(rows: RetrievedPerson[]): string {
  if (rows.length === 0) {
    return "ALUMNI RECORDS: (none matched — the directory has no grounded matches for this query)";
  }
  const lines = rows.map((r) => {
    const detail = r.claims.length
      ? " | claims: " +
        r.claims
          .slice(0, 4)
          .map((c) => `${c.claim_type}=${c.value}`)
          .join("; ")
      : "";
    const city = r.city && r.city !== "(unknown)" ? r.city : "city unknown";
    return `- ${r.full_name} | slug: ${r.name_slug} | ${r.initial_company} | ${r.school} Titans ${r.titan_class} | ${city}${detail}`;
  });
  return `ALUMNI RECORDS (use ONLY these):\n${lines.join("\n")}`;
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
