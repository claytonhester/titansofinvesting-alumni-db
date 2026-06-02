import { anthropic, HAIKU_MODEL } from "./anthropic";
import { SECTOR_NAMES } from "@/lib/db";
import type { SearchParams } from "./search";

export interface ChatTurn {
  role: "user" | "assistant";
  content: string;
}

export interface PlanResult {
  params: SearchParams;
  usage: { input_tokens: number; output_tokens: number };
}

// Static system prefix — cached (ephemeral) so repeated turns only pay for the
// short per-turn delta. The planner ONLY emits typed params; it never writes SQL
// and never answers the user.
const PLAN_SYSTEM = `You translate a visitor's question about Titans of Investing alumni into structured database search parameters. You do NOT answer the question and you do NOT invent data.

Output ONLY a JSON object with these optional keys (omit a key when not applicable):
{
  "city": <string>,            // a city to match, e.g. "Dallas", "New York"
  "school": <string>,          // a school name fragment if mentioned
  "titanClass": <number>,      // a Titans class number if mentioned
  "companyKeyword": <string>,  // a specific firm/employer keyword if named
  "sector": <one of the allowed sectors below>,
  "intent": <short phrase describing what the visitor wants>
}

Allowed sectors (use EXACTLY one of these strings, or omit):
${SECTOR_NAMES.map((s) => `- ${s}`).join("\n")}

Rules:
- Infer city and sector from natural language (e.g. "wealth management in Dallas" -> city "Dallas", sector "Hedge Funds & Asset Mgmt").
- Only set "sector" to one of the allowed strings above. If unsure, omit it.
- Keep values short. Output ONLY valid JSON, no prose, no code fences.`;

// Robust JSON extraction mirroring pipeline/structuring._parse_json: strip code
// fences, then fall back to slicing between the first { and last }.
export function parsePlanJson(raw: string): Record<string, unknown> {
  const text = raw.trim().replace(/^```(?:json)?/i, "").replace(/```$/, "").trim();
  try {
    return JSON.parse(text) as Record<string, unknown>;
  } catch {
    const start = text.indexOf("{");
    const end = text.lastIndexOf("}");
    if (start !== -1 && end !== -1 && end > start) {
      try {
        return JSON.parse(text.slice(start, end + 1)) as Record<string, unknown>;
      } catch {
        return {};
      }
    }
    return {};
  }
}

// Narrow loose JSON into typed, validated SearchParams. Unknown sectors and
// non-finite class numbers are dropped rather than passed to SQL.
export function coerceParams(obj: Record<string, unknown>): SearchParams {
  const params: SearchParams = {};
  if (typeof obj.city === "string" && obj.city.trim()) params.city = obj.city.trim();
  if (typeof obj.school === "string" && obj.school.trim()) params.school = obj.school.trim();
  if (typeof obj.companyKeyword === "string" && obj.companyKeyword.trim()) {
    params.companyKeyword = obj.companyKeyword.trim();
  }
  if (typeof obj.intent === "string" && obj.intent.trim()) params.intent = obj.intent.trim();
  const cls = typeof obj.titanClass === "number" ? obj.titanClass : Number(obj.titanClass);
  if (Number.isFinite(cls) && cls > 0) params.titanClass = Math.floor(cls);
  if (typeof obj.sector === "string" && SECTOR_NAMES.includes(obj.sector)) {
    params.sector = obj.sector;
  }
  return params;
}

const PLAN_MAX_TOKENS = 200;

// One cheap Haiku call. History is trimmed to the recent turns by the caller.
export async function planQuery(history: ChatTurn[]): Promise<PlanResult> {
  const response = await anthropic().messages.create({
    model: HAIKU_MODEL,
    max_tokens: PLAN_MAX_TOKENS,
    system: [
      { type: "text", text: PLAN_SYSTEM, cache_control: { type: "ephemeral" } },
    ],
    messages: history.map((t) => ({ role: t.role, content: t.content })),
  });

  const block = response.content[0];
  const raw = block && block.type === "text" ? block.text : "";
  const params = coerceParams(parsePlanJson(raw));

  return {
    params,
    usage: {
      input_tokens: response.usage.input_tokens,
      output_tokens: response.usage.output_tokens,
    },
  };
}
