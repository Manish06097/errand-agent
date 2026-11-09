import { expect, test } from "@playwright/test";
import { newErrand, say } from "./helpers";

const FIND = "Find a plumber for my kitchen tap this week, mornings only, under ₹1,500.";

test("forgetting a fact on /memory shows it excluded as forgotten in the next turn's Context view", async ({
  page,
  request,
}) => {
  // The row is written the way the page's own add would (an explicit user action).
  const added = await request.post("/api/memory", {
    data: { key: "home_area", scope: "global", value: "indiranagar", client_action_id: "e2e-add-home" },
  });
  expect(added.status()).toBe(201);

  await page.goto("/memory");
  await page.waitForSelector('html[data-hydrated="true"]', { state: "attached" });
  const row = page.locator('[data-testid="memory-row"][data-key="home_area"]');
  await expect(row).toContainText(/indiranagar/i);
  await expect(row.getByTestId("memory-source")).toHaveText("You told me");
  const memoryId = await row.getAttribute("data-memory-id");
  expect(memoryId).toBeTruthy();

  // Forget asks first; Cancel changes nothing.
  await row.getByTestId("memory-forget").click();
  await page.getByTestId("confirm-cancel").click();
  await expect(row).toHaveCount(1);
  await row.getByTestId("memory-forget").click();
  await page.getByTestId("confirm-ok").click();

  await expect(row).toHaveCount(0);
  const history = page.locator('[data-testid="memory-history-row"][data-key="home_area"]');
  await expect(history).toContainText("Forgotten on");
  await expect(history).not.toContainText(/indiranagar/i);

  // The next turn: its Context view lists the row as excluded, reason forgotten.
  await newErrand(page);
  await say(page, FIND);
  await expect(page.locator('[data-testid="trace-row"][data-type="run.end"]')).toBeVisible();
  await page.locator('[data-testid="trace-row"][data-type="model.call"]').first().click();
  const excluded = page.locator(
    `[data-testid="context-excluded-row"][data-memory-id="${memoryId}"]`,
  );
  await expect(excluded).toHaveAttribute("data-reason", "forgotten");
  await expect(page.getByTestId("context-view")).not.toContainText(/indiranagar/i);
  // and the forgotten area is asked for again, not filled from memory
  await expect(page.getByTestId("messages")).not.toContainText("(from memory)");
});
