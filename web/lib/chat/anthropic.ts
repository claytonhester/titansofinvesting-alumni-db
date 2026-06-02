import Anthropic from "@anthropic-ai/sdk";

// Haiku 4.5 — cheapest tier that handles disciplined planning + grounded
// synthesis well. Mirrors pipeline/structuring.HAIKU_MODEL so cost math and
// behavior stay aligned with the enrichment pipeline.
export const HAIKU_MODEL = "claude-haiku-4-5-20251001";

let _client: Anthropic | null = null;

// Fail fast with a clear message when the key is absent, mirroring
// pipeline/config.require_key. The key is read ONLY from the environment and
// must never be hardcoded or committed.
export function anthropic(): Anthropic {
  if (_client) return _client;
  const apiKey = process.env.ANTHROPIC_API_KEY;
  if (!apiKey) {
    throw new Error(
      "ANTHROPIC_API_KEY not configured — set it in web/.env.local (never commit it)."
    );
  }
  _client = new Anthropic({ apiKey });
  return _client;
}
