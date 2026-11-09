import { expect, test } from "@playwright/test";
import {
  OPENER,
  bookCalls,
  conversation,
  createdBookings,
  latestPendingCard,
  loadWorld,
  newErrand,
  say,
  waitIdle,
} from "./helpers";

test.beforeEach(async ({ request }) => {
  await loadWorld(request, "S01_happy_path_plumber");
});

test("approve to confirmed: the outcome card says verified and the world has one booking", async ({
  page,
  request,
}) => {
  await newErrand(page);
  await say(page, OPENER);
  await expect(page.getByTestId("msg-action")).toBeVisible();
  expect(await createdBookings(request)).toHaveLength(0);

  await page.getByTestId("approve-button").click();

  await expect(page.getByTestId("verified-badge")).toBeVisible();
  await expect(page.getByTestId("msg-outcome")).toContainText("verified");
  expect(await createdBookings(request)).toHaveLength(1);
  expect(await bookCalls(request)).toBe(1);
});

test("a double click books once, and a second decision POST that skips the UI books nothing more", async ({
  page,
  request,
}) => {
  const id = await newErrand(page);
  await say(page, OPENER);
  await expect(page.getByTestId("approve-button")).toBeEnabled();
  const card = await latestPendingCard(request, id);
  const approvalId = card.approval_id as string;
  const fingerprint = card.body.fingerprint as string;

  // A real double click: two clicks on the button, whatever the UI does with the second,
  // while a third decision (same fingerprint, fresh client_action_id) is fired from the
  // test at the same moment, before the run has finished. That is the race the server
  // guard exists for: the answer is already_approved (200), accepted (202, the racer won
  // and started the run) or run_in_progress (409), and never a second booking.
  const [, racer] = await Promise.all([
    page.getByTestId("approve-button").dblclick(),
    request.post(`/api/approvals/${approvalId}/decision`, {
      data: { decision: "approve", fingerprint, client_action_id: `ca_race_${Date.now()}` },
    }),
  ]);
  expect([200, 202, 409]).toContain(racer.status());
  if (racer.status() === 200) {
    expect(((await racer.json()) as { status: string }).status).toBe("already_approved");
  }
  if (racer.status() === 409) {
    expect(((await racer.json()) as { error: string }).error).toBe("run_in_progress");
  }
  await expect(page.getByTestId("verified-badge")).toBeVisible();
  await waitIdle(request, id);

  // The UI's disabled state is no guard. The same fingerprint with a fresh
  // client_action_id, posted by the test after the booking, must be answered by the
  // server alone: already approved, no new run, nothing booked.
  const second = await request.post(`/api/approvals/${approvalId}/decision`, {
    data: { decision: "approve", fingerprint, client_action_id: `ca_test_${Date.now()}` },
  });
  expect(second.status()).toBe(200);
  expect(((await second.json()) as { status: string }).status).toBe("already_approved");
  await waitIdle(request, id);
  const convo = await conversation(request, id);
  // One approval, however many messages (the card and the outcome) point at it.
  const consumed = convo.messages.filter((m) => m.approval?.status === "consumed");
  expect(new Set(consumed.map((m) => m.approval_id))).toEqual(new Set([approvalId]));
  expect(await bookCalls(request)).toBe(1);
  expect(await createdBookings(request)).toHaveLength(1);
});
