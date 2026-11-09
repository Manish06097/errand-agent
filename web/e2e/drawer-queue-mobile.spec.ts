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

test("the dev drawer moves the clock and the hold countdown switches to Refresh hold", async ({
  page,
  request,
}) => {
  await loadWorld(request, "S01_happy_path_plumber");
  await newErrand(page);
  await say(page, OPENER);
  await expect(page.getByTestId("hold-countdown")).toContainText("Hold expires in");

  await page.getByTestId("dev-open").click();
  await page.getByTestId("dev-advance").click();
  await expect(page.getByTestId("dev-message")).toContainText("done");
  await page.keyboard.press("Escape");

  await expect(page.getByTestId("hold-countdown")).toContainText("Hold expired · Refresh hold");
  await page.getByTestId("refresh-hold").click();
  await expect(page.getByTestId("hold-countdown")).toContainText("Hold expires in");
  await expect(page.getByTestId("approve-button")).toBeEnabled();
});

test("next book times out after success: the world books, the status check finds it, the card is verified with one book call", async ({
  page,
  request,
}) => {
  await loadWorld(request, "S01_happy_path_plumber");
  await newErrand(page);
  await say(page, OPENER);
  await expect(page.getByTestId("approve-button")).toBeEnabled();

  await page.getByTestId("dev-open").click();
  await page.getByTestId("dev-next-book").click();
  await expect(page.getByTestId("dev-message")).toContainText("done");
  await page.keyboard.press("Escape");

  const id = new URL(page.url()).pathname.split("/").pop() as string;
  const card = await latestPendingCard(request, id);
  await page.getByTestId("approve-button").click();
  // While the booking call is hanging, the composer stays enabled and a message sent now
  // is shown as queued, with its own stamp, to be posted after the run finishes.
  await expect(page.getByTestId("working")).toBeVisible();
  const hangingRun = (await conversation(request, id)).active_run_id;
  expect(hangingRun).toBeTruthy();
  const posted = page.waitForRequest(
    (r) => r.method() === "POST" && r.url().endsWith(`/api/conversations/${id}/messages`),
  );
  await say(page, "thanks");
  await expect(page.getByTestId("queued-message")).toContainText("thanks");

  await expect(page.getByTestId("queued-message")).toHaveCount(0, { timeout: 45_000 });
  // The queued message kept what it had at Send: the agent-clock stamp is earlier than the
  // end of the run it waited for (a stamp taken at post time would be later), and the card
  // it saw is that approval or nothing (it had already been decided).
  const sent = (await posted).postDataJSON() as {
    client_composed_at: string;
    visible_card: { approval_id: string; fingerprint: string } | null;
  };
  const trace = (await (await request.get(`/api/runs/${hangingRun}/trace`)).json()) as {
    records: { type: string; ts: string }[];
  };
  const runEnd = trace.records.find((r) => r.type === "run.end");
  expect(runEnd).toBeTruthy();
  // The book call hangs about 8 s, so a Send-time stamp is seconds before run.end; a stamp
  // taken at post time lands within the clock sync lag of it. Require a margin that cannot flip.
  expect(Date.parse(runEnd?.ts as string) - Date.parse(sent.client_composed_at)).toBeGreaterThan(
    2_000,
  );
  if (sent.visible_card !== null) {
    expect(sent.visible_card.approval_id).toBe(card.approval_id);
    expect(sent.visible_card.fingerprint).toBe(card.body.fingerprint);
  }
  await expect(page.getByTestId("verified-badge")).toBeVisible();
  expect(await bookCalls(request)).toBe(1);
  expect(await createdBookings(request)).toHaveLength(1);
  await expect(page.getByTestId("user-message").filter({ hasText: "thanks" })).toHaveCount(1);
});

test.describe("mobile", () => {
  test.use({ viewport: { width: 390, height: 800 } });

  test("no horizontal scroll, the inspector is a bottom sheet and the sidebar a drawer", async ({
    page,
    request,
  }) => {
    await loadWorld(request, "S01_happy_path_plumber");
    await newErrand(page);
    await say(page, OPENER);
    await expect(page.getByTestId("msg-action")).toBeVisible();
    const noOverflow = await page.evaluate(
      () => document.documentElement.scrollWidth <= document.documentElement.clientWidth,
    );
    expect(noOverflow).toBe(true);
    await expect(page.getByTestId("inspector")).toBeHidden();

    await page.getByTestId("open-inspector").click();
    const sheet = page.getByTestId("inspector-sheet");
    await expect(sheet).toBeVisible();
    await expect(sheet.getByTestId("trace-panel")).toBeVisible();
    const box = await sheet.boundingBox();
    expect(box?.width).toBeLessThanOrEqual(390);
    expect(box?.y ?? 0).toBeGreaterThan(0);
    await page.keyboard.press("Escape");

    await page.getByTestId("open-sidebar").click();
    await expect(page.getByTestId("sidebar-drawer").getByTestId("new-errand")).toBeVisible();
    await page.keyboard.press("Escape");
    await waitIdle(request, new URL(page.url()).pathname.split("/").pop() as string);
  });
});
