import { expect, test, type Request } from "@playwright/test";
import { loadWorld, newErrand, say } from "./helpers";

// "Answer" and "Shorten it" are asks for the user to type, never messages: nobody wrote
// those words. Clicking them sends nothing.

function messagePosts(page: import("@playwright/test").Page): Request[] {
  const posts: Request[] = [];
  page.on("request", (r) => {
    if (r.method() === "POST" && r.url().endsWith("/messages")) posts.push(r);
  });
  return posts;
}

test("Answer on the ask-budget-spent notice focuses the composer and sends nothing", async ({
  page,
  request,
}) => {
  await loadWorld(request, "S67_area_dodged_twice");
  await newErrand(page);
  await say(page, "Find a plumber for my kitchen tap this week, mornings only, under ₹1,500.");
  await expect(page.getByTestId("msg-clarify")).toHaveCount(1);
  await say(page, "whatever");
  await expect(page.getByTestId("msg-clarify")).toHaveCount(2);
  await say(page, "not sure");
  const answer = page.getByTestId("notice-action-answer");
  await expect(answer).toBeVisible();

  const posts = messagePosts(page);
  await page.getByTestId("composer-input").blur();
  await answer.click();
  await expect(page.getByTestId("composer-input")).toBeFocused();
  await page.waitForTimeout(500);
  expect(posts).toHaveLength(0);
  await expect(page.getByTestId("composer-input")).toHaveValue("");
  await expect(page.getByText("Answer", { exact: true })).toHaveCount(1); // the button only
});

test("Shorten it on a too-long message puts it back in the composer and sends nothing", async ({
  page,
  request,
}) => {
  await loadWorld(request, "S01_happy_path_plumber");
  await newErrand(page);
  const long = `Book a plumber in Indiranagar. ${"The kitchen tap keeps dripping at night. ".repeat(2500)}`;
  await say(page, long);
  const shorten = page.getByTestId("notice-action-shorten");
  await expect(shorten).toBeVisible();

  const posts = messagePosts(page);
  await shorten.click();
  await expect(page.getByTestId("composer-input")).toBeFocused();
  // Compared without printing 100 KB on a failure.
  await expect
    .poll(async () => (await page.getByTestId("composer-input").inputValue()) === long.trim())
    .toBe(true);
  await page.waitForTimeout(500);
  expect(posts).toHaveLength(0);
});
