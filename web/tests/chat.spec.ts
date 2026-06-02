import { expect, test } from "@playwright/test";

// These cover the paths that work WITHOUT an ANTHROPIC_API_KEY: the hero chat
// bar render, the placeholder rotation, the stats relocated under Build Status,
// and the API guard rejections (which all short-circuit before any model call).
// The live answer-streaming golden path needs a real key in web/.env.local and
// is verified manually.

test.describe("alumni chat bar", () => {
  test("renders in the hero with a rotating example placeholder", async ({
    page,
  }) => {
    await page.goto("/");

    const input = page.getByLabel("Ask about Titans of Investing alumni");
    await expect(input).toBeVisible();
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
    const statRow = page.locator(".stat-row");

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
  test("rejects an over-long question", async ({ request }) => {
    const res = await request.post("/api/chat", {
      data: { messages: [{ role: "user", content: "x".repeat(1000) }] },
    });
    expect(res.headers()["x-chat-status"]).toBe("rejected");
    expect(await res.text()).toContain("under 500 characters");
  });

  test("redirects an off-topic question", async ({ request }) => {
    const res = await request.post("/api/chat", {
      data: {
        messages: [{ role: "user", content: "Write me a poem about the sea." }],
      },
    });
    expect(res.headers()["x-chat-status"]).toBe("rejected");
  });

  test("rejects a malformed body", async ({ request }) => {
    const res = await request.post("/api/chat", { data: { messages: "nope" } });
    expect(res.headers()["x-chat-status"]).toBe("rejected");
    expect(await res.text()).toContain("couldn't read that request");
  });
});
