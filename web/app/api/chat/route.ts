import { z } from "zod";
import { planQuery, type ChatTurn } from "@/lib/chat/plan";
import { retrievePeople } from "@/lib/chat/search";
import { streamAnswer } from "@/lib/chat/synthesize";
import {
  checkInput,
  checkRateShared,
  checkTopic,
  MAX_INPUT_CHARS,
  rejection,
} from "@/lib/chat/guards";
import { isOverCapShared, logTurnShared } from "@/lib/chat/cost-guard";
import { checkAuth } from "@/lib/chat/auth";

// better-sqlite3 + the Anthropic SDK need the Node.js runtime.
export const runtime = "nodejs";

const MAX_HISTORY_TURNS = 8;

// Per-turn size caps enforced at the schema boundary, so an oversized EARLIER
// turn is rejected (400) before any model call — the checkInput guard below
// only covers the latest turn. User turns share the ChatBar's 500-char limit
// (MAX_INPUT_CHARS); assistant turns are echoes of our own ≤500-token answers,
// so they get 4× that. Anything larger is not a real conversation transcript.
const MAX_USER_TURN_CHARS = MAX_INPUT_CHARS;
const MAX_ASSISTANT_TURN_CHARS = MAX_INPUT_CHARS * 4;

const turnSchema = z.discriminatedUnion("role", [
  z.object({
    role: z.literal("user"),
    content: z.string().max(MAX_USER_TURN_CHARS),
  }),
  z.object({
    role: z.literal("assistant"),
    content: z.string().max(MAX_ASSISTANT_TURN_CHARS),
  }),
]);

const bodySchema = z.object({
  messages: z.array(turnSchema).min(1).max(40),
});

function clientIp(req: Request): string {
  // Defense-in-depth for the per-IP limiter (the monthly cap is the real
  // backstop). A client can stuff arbitrary values into x-forwarded-for, but
  // on the deploy target (Vercel) x-real-ip is injected by the platform edge
  // and cannot be overridden by the caller — prefer it, fall back to the first
  // forwarded token only when the platform header is absent (e.g. local dev).
  const real = req.headers.get("x-real-ip");
  if (real) return real.trim();
  const fwd = req.headers.get("x-forwarded-for");
  if (fwd) return fwd.split(",")[0].trim();
  return "unknown";
}

function textResponse(
  message: string,
  rejected: boolean,
  status = 200
): Response {
  return new Response(message, {
    status,
    headers: {
      "content-type": "text/plain; charset=utf-8",
      "x-chat-status": rejected ? "rejected" : "ok",
      "cache-control": "no-store",
    },
  });
}

export async function POST(req: Request): Promise<Response> {
  // Lightweight gate first (header-only, no body read): same-origin + a signed
  // page token. Cheapest possible rejection for off-site/scripted callers.
  const auth = checkAuth(req);
  if (!auth.ok) return textResponse(auth.message!, true);

  let parsed: z.infer<typeof bodySchema>;
  try {
    parsed = bodySchema.parse(await req.json());
  } catch {
    // Malformed or oversized body: a client-side error, so 400 (not 200) —
    // the ChatBar still renders the text, and scripted callers see the status.
    return textResponse(
      "Sorry — I couldn't read that request. Please try again.",
      true,
      400
    );
  }

  const history: ChatTurn[] = parsed.messages.slice(-MAX_HISTORY_TURNS);
  const latest = history[history.length - 1];
  if (!latest || latest.role !== "user") {
    return textResponse("Ask me a question to get started.", true);
  }

  const inputCheck = checkInput(latest.content);
  if (!inputCheck.ok) return textResponse(inputCheck.message!, true);

  const topicCheck = checkTopic(latest.content);
  if (!topicCheck.ok) return textResponse(topicCheck.message!, true);

  const rateCheck = await checkRateShared(clientIp(req));
  if (!rateCheck.ok) return textResponse(rateCheck.message!, true);

  // Hard kill switch: if month-to-date spend has hit the cap, make NO API call.
  if (await isOverCapShared()) {
    return textResponse(rejection("over_cap").message!, true);
  }

  // Plan (cheap Haiku JSON call) -> retrieve (no model) -> stream synthesis.
  let planUsage = { input_tokens: 0, output_tokens: 0 };
  let rows;
  try {
    const plan = await planQuery(history);
    planUsage = plan.usage;
    rows = await retrievePeople(plan.params, latest.content);
  } catch (error: unknown) {
    const msg = error instanceof Error ? error.message : "Unexpected error";
    // Surface config problems (missing key) clearly; keep other detail private.
    const friendly = msg.includes("ANTHROPIC_API_KEY")
      ? "The chat isn't configured yet. (Server is missing its API key.)"
      : "Something went wrong reaching the alumni data. Please try again.";
    return textResponse(friendly, true);
  }

  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    async start(controller) {
      // The plan call's tokens are already spent before streaming begins. Track
      // whether the combined usage was logged; if synthesis throws before its
      // usage event, the finally block still records the incurred plan cost so
      // the monthly kill-switch can't be undercounted.
      let logged = false;
      try {
        for await (const event of streamAnswer(history, rows)) {
          if (event.type === "text") {
            controller.enqueue(encoder.encode(event.text));
          } else if (event.type === "usage") {
            await logTurnShared({
              input_tokens: planUsage.input_tokens + event.usage.input_tokens,
              output_tokens:
                planUsage.output_tokens + event.usage.output_tokens,
            });
            logged = true;
          }
        }
      } catch {
        controller.enqueue(
          encoder.encode("\n\n(Sorry — the answer was cut short. Please try again.)")
        );
      } finally {
        if (!logged) await logTurnShared(planUsage);
        controller.close();
      }
    },
  });

  return new Response(stream, {
    status: 200,
    headers: {
      "content-type": "text/plain; charset=utf-8",
      "x-chat-status": "ok",
      "cache-control": "no-store",
    },
  });
}
