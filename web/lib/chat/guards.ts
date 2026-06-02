// Visitor-facing guardrails for the public chat endpoint. All limits are cheap
// pre-checks that run BEFORE any Anthropic call.

export const MAX_INPUT_CHARS = 500;
export const RATE_LIMIT_PER_MIN = 8;
const RATE_WINDOW_MS = 60_000;

export type RejectReason =
  | "empty"
  | "too_long"
  | "rate_limited"
  | "off_topic"
  | "over_cap";

export interface GuardResult {
  ok: boolean;
  reason?: RejectReason;
  message?: string;
}

const REJECT_MESSAGES: Record<RejectReason, string> = {
  empty: "Ask me something about Titans of Investing alumni to get started.",
  too_long: `Please keep your question under ${MAX_INPUT_CHARS} characters.`,
  rate_limited: "You're going a little fast — give it a moment and try again.",
  off_topic:
    "I can only help with Titans of Investing alumni — their careers, firms, and who you might connect with. Try asking about a city, sector, or firm.",
  over_cap:
    "The alumni chat is resting for the month while we keep costs in check. Please check back next month.",
};

export function rejection(reason: RejectReason): GuardResult {
  return { ok: false, reason, message: REJECT_MESSAGES[reason] };
}

export function checkInput(message: string): GuardResult {
  const trimmed = message.trim();
  if (trimmed.length === 0) return rejection("empty");
  if (message.length > MAX_INPUT_CHARS) return rejection("too_long");
  return { ok: true };
}

// In-memory, per-instance sliding-window rate limiter keyed by IP. NOTE: this
// is per server instance — on a multi-instance deploy the effective limit is
// RATE_LIMIT_PER_MIN * instances. The hard monthly cost cap is the real
// backstop; this just blunts a single abuser.
const hits = new Map<string, number[]>();
let lastSweep = 0;

// Drop IPs whose entire window has expired so the Map can't grow unbounded from
// one-shot visitors. Runs at most once per window to keep checkRate near O(1).
function sweep(now: number): void {
  if (now - lastSweep < RATE_WINDOW_MS) return;
  lastSweep = now;
  const windowStart = now - RATE_WINDOW_MS;
  for (const [ip, times] of hits) {
    if (times.every((t) => t <= windowStart)) hits.delete(ip);
  }
}

export function checkRate(ip: string, now: number = Date.now()): GuardResult {
  sweep(now);
  const windowStart = now - RATE_WINDOW_MS;
  const recent = (hits.get(ip) ?? []).filter((t) => t > windowStart);
  if (recent.length >= RATE_LIMIT_PER_MIN) {
    hits.set(ip, recent);
    return rejection("rate_limited");
  }
  hits.set(ip, [...recent, now]);
  return { ok: true };
}

// Cheap topic pre-check: looks for any alumni/career/finance-ish signal. This is
// intentionally permissive (the system prompt is the real topic guard) and only
// catches obviously off-topic prompts so we skip the model call. Returns ok for
// short greetings so first-time visitors aren't rejected.
const TOPIC_HINTS = [
  "alumni",
  "alum",
  "titan",
  "connect",
  "network",
  "career",
  "job",
  "intern",
  "firm",
  "company",
  "bank",
  "invest",
  "finance",
  "consult",
  "account",
  "equity",
  "hedge",
  "asset",
  "energy",
  "advis",
  "wealth",
  "recruit",
  "mentor",
  "school",
  "class",
  "city",
  "move",
  "moving",
  "start",
  "who",
  "where",
  "which",
  "recommend",
  "introduc",
  "work",
];

export function checkTopic(message: string): GuardResult {
  const lower = message.toLowerCase();
  // Allow very short messages (greetings, follow-ups like "what about NYC?").
  if (lower.trim().length <= 24) return { ok: true };
  if (TOPIC_HINTS.some((h) => lower.includes(h))) return { ok: true };
  return rejection("off_topic");
}
