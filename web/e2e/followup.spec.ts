import { expect, test } from "@playwright/test";
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
