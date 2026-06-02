import { z } from "zod";
import { planQuery, type ChatTurn } from "@/lib/chat/plan";
import { searchPeople } from "@/lib/chat/search";
import { streamAnswer } from "@/lib/chat/synthesize";
import { checkInput, checkRate, checkTopic, rejection } from "@/lib/chat/guards";
import { isOverCap, logTurn } from "@/lib/chat/cost-guard";

// better-sqlite3 + the Anthropic SDK need the Node.js runtime.
export const runtime = "nodejs";

const MAX_HISTORY_TURNS = 8;

const bodySchema = z.object({
  messages: z
    .array(
      z.object({
        role: z.enum(["user", "assistant"]),
        content: z.string(),
      })
    )
    .min(1)
    .max(40),
});

function clientIp(req: Request): string {
  const fwd = req.headers.get("x-forwarded-for");
  if (fwd) return fwd.split(",")[0].trim();
  return req.headers.get("x-real-ip") ?? "unknown";
}

function textResponse(message: string, rejected: boolean): Response {
  return new Response(message, {
    status: 200,
    headers: {
      "content-type": "text/plain; charset=utf-8",
      "x-chat-status": rejected ? "rejected" : "ok",
      "cache-control": "no-store",
    },
  });
}

export async function POST(req: Request): Promise<Response> {
  let parsed: z.infer<typeof bodySchema>;
  try {
    parsed = bodySchema.parse(await req.json());
  } catch {
    return textResponse(
      "Sorry — I couldn't read that request. Please try again.",
      true
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

  const rateCheck = checkRate(clientIp(req));
  if (!rateCheck.ok) return textResponse(rateCheck.message!, true);

  // Hard kill switch: if month-to-date spend has hit the cap, make NO API call.
  if (isOverCap()) {
    return textResponse(rejection("over_cap").message!, true);
  }

  // Plan (cheap Haiku JSON call) -> retrieve (no model) -> stream synthesis.
  let planUsage = { input_tokens: 0, output_tokens: 0 };
  let rows;
  try {
    const plan = await planQuery(history);
    planUsage = plan.usage;
    rows = searchPeople(plan.params);
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
      try {
        for await (const event of streamAnswer(history, rows)) {
          if (event.type === "text") {
            controller.enqueue(encoder.encode(event.text));
          } else if (event.type === "usage") {
            logTurn({
              input_tokens: planUsage.input_tokens + event.usage.input_tokens,
              output_tokens:
                planUsage.output_tokens + event.usage.output_tokens,
            });
          }
        }
      } catch {
        controller.enqueue(
          encoder.encode("\n\n(Sorry — the answer was cut short. Please try again.)")
        );
      } finally {
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
