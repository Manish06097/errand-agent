import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { OPENER, createdBookings, loadWorld, newErrand, say, WORLD } from "./helpers";

// A provider cancels a verified booking. With no message sent, the watcher notices, the
// bell shows one entry, and the follow-up conversation holds a Rebook card that only the
// user can approve. The world is the S39 scenario's: two forced book successes.

test.beforeEach(async ({ request }) => {
  await loadWorld(request, "S39_provider_cancels_and_user_rebooks");
});

test("provider cancels: the bell shows 1 with no message sent, the Rebook card books once", async ({
  page,
  request,
}) => {
  await newErrand(page);
  await say(page, OPENER);
  await page.getByTestId("approve-button").click();
  await expect(page.getByTestId("verified-badge")).toBeVisible();
  const booked = await createdBookings(request);
  expect(booked).toHaveLength(1);

  // The dev drawer's "Provider cancels booking" fires the world event; nothing else.
  await page.getByTestId("dev-open").click();
  await page.getByTestId("dev-booking").selectOption(booked[0]!.booking_id);
  await page.getByTestId("dev-provider-cancel").click();
  await expect(page.getByTestId("dev-message")).toContainText("done");
  await page.keyboard.press("Escape");

  // No message was sent, yet the bell shows one entry and the origin chat has its line.
  await expect(page.getByTestId("bell-count")).toHaveText("1");
  await expect(page.getByTestId("msg-proactive")).toContainText("cancelled");

  // Open the follow-up from the bell: the notice, then the Rebook card as the last message.
  await page.getByTestId("bell").click();
  await page.getByTestId("bell-item").first().click();
  await page.waitForURL(/\/c\/cf_/);
  await expect(page.getByTestId("card-header")).toContainText("Replaces");
  await expect(page.getByTestId("approve-button")).toContainText("Rebook for");
  await expect(page.getByTestId("decline-button")).toHaveText("Not this one");
  await expect(page.getByTestId("messages").getByTestId("assistant-message").last()).toContainText(
    "Rebook",
  );
  expect(await createdBookings(request)).toHaveLength(1); // the card alone books nothing

  await page.getByTestId("approve-button").click();
  await expect(page.getByTestId("verified-badge")).toBeVisible();

  // The original stays cancelled_by_provider and exactly one new booking exists.
  const all = await createdBookings(request);
  expect(all.map((b) => b.status).sort()).toEqual(["cancelled_by_provider", "confirmed"]);
  const dev = await request.get(`${WORLD}/dev/bookings`);
  expect(((await dev.json()) as { status: string }[]).filter((b) => b.status === "confirmed").length).toBeGreaterThan(0);

  // Another watcher interval adds no notification.
  await page.waitForTimeout(3000);
  const bell = await request.get("/api/notifications");
  expect(((await bell.json()) as { notifications: unknown[] }).notifications).toHaveLength(1);
});

// Gate 3 decision 7: an urgent cancel noticed outside the saved contact window is delivered
// at once, with the badge only. The control test is the same cancel with no window saved,
// where the banner shows.

async function cancelBookedPlumber(page: Page, request: APIRequestContext): Promise<void> {
  await newErrand(page);
  await say(page, OPENER);
  await page.getByTestId("approve-button").click();
  await expect(page.getByTestId("verified-badge")).toBeVisible();
  const booked = await createdBookings(request);
  await page.getByTestId("dev-open").click();
  await page.getByTestId("dev-booking").selectOption(booked[0]!.booking_id);
  await page.getByTestId("dev-provider-cancel").click();
  await expect(page.getByTestId("dev-message")).toContainText("done");
  await page.keyboard.press("Escape");
}

/** A one-hour contact window about twelve hours from the agent's clock, so it never holds it. */
async function saveWindowAwayFromNow(request: APIRequestContext): Promise<string> {
  const health = await request.get("/api/health");
  const header = health.headers()["x-agent-clock"] ?? "";
  const nowMs = Number(/now_ms=(\d+)/.exec(header)?.[1]);
  expect(Number.isFinite(nowMs), header).toBe(true);
  const istHour = new Date(nowMs + 5.5 * 3600_000).getUTCHours();
  const start = Math.min((istHour + 12) % 24, 22);
  const hh = (h: number) => `${String(h).padStart(2, "0")}:00`;
  const saved = await request.post("/api/memory", {
    data: {
      key: "contact_window",
      scope: "global",
      value: { from: hh(start), to: hh(start + 1) },
      client_action_id: `e2e-window-${Date.now()}`,
    },
  });
  expect(saved.ok(), await saved.text()).toBe(true);
  const body = (await saved.json()) as { memory_id?: string; memory?: { memory_id: string } };
  return (body.memory_id ?? body.memory?.memory_id) as string;
}

test("an urgent cancel with no contact window saved shows the banner", async ({ page, request }) => {
  await cancelBookedPlumber(page, request);
  await expect(page.getByTestId("bell-count")).toHaveText("1");
  await expect(page.getByTestId("urgent-banner")).toBeVisible();
});

test("an urgent cancel outside the saved contact window is quiet: the badge, no toast, no banner", async ({
  page,
  request,
}) => {
  const memoryId = await saveWindowAwayFromNow(request);
  try {
    await cancelBookedPlumber(page, request);
    await expect(page.getByTestId("bell-count")).toHaveText("1");
    // the bell polls every five seconds and the stream nudges it: give a loud notice time to show
    await page.waitForTimeout(2000);
    await expect(page.getByTestId("urgent-banner")).toHaveCount(0);
    await expect(page.getByTestId("toast")).toHaveCount(0);
    const bell = (await (await request.get("/api/notifications")).json()) as {
      notifications: { urgency: string; quiet: boolean }[];
    };
    expect(bell.notifications).toHaveLength(1);
    expect(bell.notifications[0]).toMatchObject({ urgency: "urgent", quiet: true });
  } finally {
    // the data directory outlives the test: leave no contact window behind
    await request.delete(`/api/memory/${memoryId}?client_action_id=e2e-window-forget-${Date.now()}`);
  }
});
