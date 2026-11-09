import { expect, test } from "@playwright/test";
import { OPENER, loadWorld, newErrand, say } from "./helpers";

test("the trace panel updates while the agent is still working", async ({ page, request }) => {
  await loadWorld(request, "S01_happy_path_plumber");
  await newErrand(page);
  await expect(page.getByTestId("trace-panel")).toBeVisible();
  await say(page, OPENER);

  // Rows arrive over SSE while the run is going: some are on screen, the run has not
  // ended, and the working indicator is still up.
  await page.waitForFunction(() => {
    const rows = document.querySelectorAll('[data-testid="trace-row"]').length;
    const ended = document.querySelector('[data-testid="trace-row"][data-type="run.end"]');
    const working = document.querySelector('[data-testid="working"], [data-testid="activity"]');
    return rows >= 3 && !ended && working !== null;
  });
  await expect(page.getByTestId("activity-line").first()).toBeVisible();

  // Then it finishes: the run.end row and the totals are there.
  await expect(page.locator('[data-testid="trace-row"][data-type="run.end"]')).toBeVisible();
  await expect(page.getByTestId("trace-totals").first()).toContainText("tok");
  await expect(page.getByTestId("msg-action")).toBeVisible();
});
