import { expect, test } from "@playwright/test";
import { OPENER, bookCalls, createdBookings, loadWorld, newErrand, say, waitIdle } from "./helpers";

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

test("next book times out after success: the world books, the user is told it is pending", async ({
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

  await page.getByTestId("approve-button").click();
  // While the booking call is hanging, the composer stays enabled and a message sent now
  // is shown as queued, with its own stamp, to be posted after the run finishes.
  await expect(page.getByTestId("working")).toBeVisible();
  await say(page, "thanks");
  await expect(page.getByTestId("queued-message")).toContainText("thanks");

  await expect(page.getByTestId("queued-message")).toHaveCount(0, { timeout: 45_000 });
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
