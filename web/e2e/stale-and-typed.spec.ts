import { expect, test } from "@playwright/test";
import {
  OPENER,
  createdBookings,
  latestPendingCard,
  loadWorld,
  newErrand,
  say,
  waitIdle,
} from "./helpers";

test("a click on a stale card shows details changed and the refetched card is the new one", async ({
  page,
  request,
}) => {
  await loadWorld(request, "S21_stale_card_after_correction");
  const id = await newErrand(page);
  await say(page, OPENER);
  await expect(page.getByTestId("msg-action")).toContainText("9:00");
  const stale = await latestPendingCard(request, id);

  // The correction arrives behind the page's back (not through the UI), so the browser
  // still shows the old card as pending.
  const correction = await request.post(`/api/conversations/${id}/messages`, {
    data: { client_msg_id: "cm_correction_1", text: "actually make it at 11" },
  });
  expect(correction.status()).toBe(202);
  await waitIdle(request, id);
  await expect(page.getByTestId("msg-action")).toHaveCount(1);
  await expect(page.getByTestId("msg-action")).toContainText("9:00");

  await page.getByTestId("approve-button").click();

  await expect(page.getByTestId("card-note")).toContainText("details changed");
  // The conversation was refetched: the new card replaced the old one as the live card.
  const fresh = await latestPendingCard(request, id);
  expect(fresh.approval_id).not.toBe(stale.approval_id);
  const pending = page.locator('[data-testid="msg-action"][data-approval-status="pending"]');
  await expect(pending).toHaveCount(1);
  await expect(pending).toContainText("11:00");
  expect(await createdBookings(request)).toHaveLength(0);
});

test('typing "book it" with the synced clock is routed as a grant', async ({ page, request }) => {
  await loadWorld(request, "S01_happy_path_plumber");
  // Put the agent clock a day ahead of the wall clock, whatever the time of day: a client
  // that stamps Date.now() then composes before the card was requested and is never
  // granted. (Behind the wall clock it would be refused as composed in the future, M4F.)
  const moved = await request.post("/api/dev/clock/advance", { data: { seconds: 86_400 } });
  expect(moved.ok()).toBe(true);
  const id = await newErrand(page);
  await say(page, OPENER);
  await expect(page.getByTestId("msg-action")).toBeVisible();
  // Give the card's render ack a moment to land before composing.
  await page.waitForTimeout(500);

  const posted = page.waitForResponse(
    (r) => r.url().endsWith(`/api/conversations/${id}/messages`) && r.request().method() === "POST",
  );
  await say(page, "book it");
  const response = await posted;
  expect(((await response.json()) as { routed_as: string }).routed_as).toBe("grant");

  await expect(page.getByTestId("verified-badge")).toBeVisible();
  expect(await createdBookings(request)).toHaveLength(1);
});
