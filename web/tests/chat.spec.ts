import { expect, test, type APIRequestContext } from "@playwright/test";

// These cover the paths that work WITHOUT an ANTHROPIC_API_KEY: the hero chat
// bar render, the placeholder rotation, the stats relocated under Build Status,
// and the API guard rejections (which all short-circuit before any model call).
// The live answer-streaming golden path needs a real key in web/.env.local and
// is verified manually.
//
// /api/chat is gated: it only accepts a same-origin POST carrying the signed
// token the page embeds at render time. The guard tests therefore do what a
// browser does — load `/`, take the token out of the page, and echo it back
// with an Origin header. NOTE: in production mode (`next start`) the server
// must have CHAT_TOKEN_SECRET set, or the chat renders unavailable and no
// token is minted (see lib/chat/auth.ts).

const ORIGIN = "http://localhost:3210";

// The ChatBar's `token` prop travels in the RSC flight payload, JSON-escaped
// inside an inline <script> ("token\":\"<exp>.<sig>\"), so accept either form.
async function pageToken(request: APIRequestContext): Promise<string> {
  const html = await (await request.get("/")).text();
  const match = /token\\?":\\?"(\d+\.[A-Za-z0-9_-]+)/.exec(html);
  if (!match) {
    throw new Error(
      "No chat token in the page HTML — is CHAT_TOKEN_SECRET set for the server under test?"
    );
  }
  return match[1];
}

async function postChat(
  request: APIRequestContext,
  data: unknown,
  headers: Record<string, string>
) {
  return request.post("/api/chat", { data, headers });
}

test.describe("alumni chat bar", () => {
  test("renders in the hero with a rotating example placeholder", async ({
    page,
  }) => {
    await page.goto("/");

    const input = page.getByLabel("Ask about Titans of Investing alumni");
    await expect(input).toBeVisible();
    await expect(input).toBeEnabled();
    await expect(input).toHaveAttribute("maxlength", "500");

    const first = await input.getAttribute("placeholder");
    expect(first).toBeTruthy();

    // The placeholder cycles on a timer; wait long enough for one rotation.
    await expect
      .poll(async () => input.getAttribute("placeholder"), { timeout: 9000 })
      .not.toBe(first);

    await expect(page.getByRole("button", { name: "Ask" })).toBeVisible();
  });

  test("relocates the four stats into the Build Status tab", async ({
    page,
  }) => {
    await page.goto("/");

    // The hero no longer carries a stat strip.
    await expect(page.locator(".hero .hero-stats")).toHaveCount(0);

    const buildTab = page.getByRole("tab", { name: "Build Status" });
    // Build Status has two stat rows (directory glance + profile quality);
    // the directory one comes first.
    const statRow = page.locator(".stat-row").first();

    // Retry the click+assert: the tab is in a client component, so an early
    // click can land before hydration wires up the onClick handler.
    await expect(async () => {
      await buildTab.click();
      await expect(statRow).toBeVisible({ timeout: 1000 });
    }).toPass();
    await expect(statRow.getByText("Alumni", { exact: true })).toBeVisible();
    await expect(statRow.getByText("Schools", { exact: true })).toBeVisible();
    await expect(
      statRow.getByText("Verified claims", { exact: true })
    ).toBeVisible();
  });
});

test.describe("chat API guards (no key required)", () => {
  test("rejects a call with no Origin and no page token (non-browser caller)", async ({
    request,
  }) => {
    const res = await request.post("/api/chat", {
      data: { messages: [{ role: "user", content: "Who is in Dallas?" }] },
    });
    expect(res.headers()["x-chat-status"]).toBe("rejected");
    expect(await res.text()).toContain("only be used from the Titans alumni site");
  });

  test("rejects an over-long question", async ({ request }) => {
    const token = await pageToken(request);
    const res = await postChat(
      request,
      { messages: [{ role: "user", content: "x".repeat(1000) }] },
      { origin: ORIGIN, "x-chat-token": token }
    );
    expect(res.status()).toBe(400);
    expect(res.headers()["x-chat-status"]).toBe("rejected");
    // Anything over the per-turn schema cap is refused before the guards run.
    expect(await res.text()).toContain("couldn't read that request");
  });

  test("redirects an off-topic question", async ({ request }) => {
    const token = await pageToken(request);
    const res = await postChat(
      request,
      { messages: [{ role: "user", content: "Write me a poem about the sea." }] },
      { origin: ORIGIN, "x-chat-token": token }
    );
    expect(res.status()).toBe(200);
    expect(res.headers()["x-chat-status"]).toBe("rejected");
    expect(await res.text()).toContain("only help with Titans of Investing alumni");
  });

  test("rejects a malformed body", async ({ request }) => {
    const token = await pageToken(request);
    const res = await postChat(
      request,
      { messages: "nope" },
      { origin: ORIGIN, "x-chat-token": token }
    );
    expect(res.status()).toBe(400);
    expect(res.headers()["x-chat-status"]).toBe("rejected");
    expect(await res.text()).toContain("couldn't read that request");
  });
});
