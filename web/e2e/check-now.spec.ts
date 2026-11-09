import { expect, test } from "@playwright/test";
import { OPENER, bookCalls, createdBookings, loadWorld, newErrand, say } from "./helpers";

// S24: `book` times out after the provider committed, and the status endpoint is down for
// the first pass of four checks. The user is told it is pending; the booking exists.

async function toPending(page: import("@playwright/test").Page, request: import("@playwright/test").APIRequestContext) {
  await loadWorld(request, "S24_status_unavailable_after_timeout");
  await newErrand(page);
  await say(page, OPENER);
  await expect(page.getByTestId("approve-button")).toBeEnabled();
  await page.getByTestId("approve-button").click();
  // Eight seconds of timeout, then a pass of four status checks: the card appears late.
  await expect(page.getByTestId("msg-verifying")).toBeVisible({ timeout: 45_000 });
  expect(await createdBookings(request)).toHaveLength(1);
  expect(await bookCalls(request)).toBe(1);
}

test("Check now on the pending card turns it into the verified card, with no second book", async ({
  page,
  request,
}) => {
  await toPending(page, request);
  await expect(page.getByTestId("verified-badge")).toHaveCount(0);
  await page.getByTestId("notice-action-check_now").click();

  await expect(page.getByTestId("verified-badge")).toBeVisible();
  await expect(page.getByTestId("msg-outcome")).toContainText("verified");
  expect(await createdBookings(request)).toHaveLength(1);
  expect(await bookCalls(request)).toBe(1);
});

test("Reconcile now in the dev drawer settles the same pending card", async ({
  page,
  request,
}) => {
  await toPending(page, request);
  await page.getByTestId("dev-open").click();
  await page.getByTestId("dev-reconcile").click();
  await expect(page.getByTestId("dev-message")).toContainText("Reconcile: done");

  await expect(page.getByTestId("verified-badge")).toBeVisible();
  expect(await createdBookings(request)).toHaveLength(1);
  expect(await bookCalls(request)).toBe(1);
});
