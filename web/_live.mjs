import { chromium } from "@playwright/test";
const base = "http://127.0.0.1:9272";
const OPENER = "Book a plumber in Indiranagar for my kitchen tap this week, mornings only, under ₹1,500.";
const load = await fetch(`${base}/api/dev/scenario`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ scenario_id: "S01_happy_path_plumber" }) });
console.log("load world", load.status);
const b = await chromium.launch();
const p = await b.newPage({ viewport: { width: 1480, height: 1000 } });
p.on("pageerror", (e) => console.log("pageerror:", e.message));
await p.goto(base + "/");
await p.waitForSelector('html[data-hydrated="true"]', { state: "attached" });
await p.getByTestId("new-errand").click();
await p.waitForURL(/\/c\//);
await p.getByTestId("composer-input").fill(OPENER);
const t0 = Date.now();
await p.getByTestId("send-button").click();
await p.waitForSelector('[data-testid="slot-button"], [data-testid="approve-button"], [data-testid="quick-reply"]', { timeout: 120000 });
console.log("first answer after", Date.now() - t0, "ms");
if (await p.getByTestId("slot-button").count()) {
  await p.getByTestId("slot-button").first().click();
  await p.getByTestId("approve-button").waitFor({ timeout: 120000 });
}
await p.getByTestId("approve-button").click();
await p.getByTestId("verified-badge").waitFor({ timeout: 120000 });
// expand the first model call to show its context manifest
const row = p.locator('[data-testid="trace-run"][data-run-id] [data-testid="trace-row"][data-type="model.call"] button').last();
await row.click();
await p.waitForTimeout(500);
await p.screenshot({ path: "../docs/engineering/m7_live_run.png" });
console.log("DONE", await p.getByTestId("msg-outcome").innerText());
await b.close();
