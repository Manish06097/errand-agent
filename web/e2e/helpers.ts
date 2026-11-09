import { expect, type APIRequestContext, type Page } from "@playwright/test";

export const WORLD = `http://127.0.0.1:${process.env.ERRAND_WORLD_PORT ?? "8765"}`;

export const OPENER =
  "Book a plumber in Indiranagar for my kitchen tap this week, mornings only, under ₹1,500.";

type Booking = { booking_id: string; status: string; created_by_key: string | null };
type OpRow = { op: string };

/** A fresh world for the scenario (the dev drawer's scenario picker, through its route). */
export async function loadWorld(request: APIRequestContext, scenario: string): Promise<void> {
  const response = await request.post("/api/dev/scenario", { data: { scenario_id: scenario } });
  expect(response.ok(), await response.text()).toBe(true);
}

/** Bookings the agent created (the seeded history has no creating key). */
export async function createdBookings(request: APIRequestContext): Promise<Booking[]> {
  const response = await request.get(`${WORLD}/dev/bookings`);
  const all = (await response.json()) as Booking[];
  return all.filter((b) => b.created_by_key !== null);
}

export async function bookCalls(request: APIRequestContext): Promise<number> {
  const response = await request.get(`${WORLD}/dev/op_log`);
  return ((await response.json()) as OpRow[]).filter((r) => r.op === "book").length;
}

export async function newErrand(page: Page): Promise<string> {
  await page.goto("/");
  await page.waitForSelector('html[data-hydrated="true"]', { state: "attached" });
  await page.getByTestId("new-errand").click();
  await page.waitForURL(/\/c\/[^/]+$/);
  const id = new URL(page.url()).pathname.split("/").pop();
  expect(id).toBeTruthy();
  await expect(page.getByTestId("composer-input")).toBeEnabled();
  return id as string;
}

export async function say(page: Page, text: string): Promise<void> {
  await page.getByTestId("composer-input").fill(text);
  await page.getByTestId("send-button").click();
}

type ApprovalMessage = {
  approval_id: string | null;
  body: { type: string; fingerprint?: string; approval_id?: string; slot_label?: string };
  approval?: { status: string } | null;
};

export async function conversation(request: APIRequestContext, id: string) {
  const response = await request.get(`/api/conversations/${id}`);
  return (await response.json()) as {
    active_run_id: string | null;
    messages: ApprovalMessage[];
  };
}

export async function latestPendingCard(request: APIRequestContext, id: string) {
  const convo = await conversation(request, id);
  const cards = convo.messages.filter(
    (m) => m.body.type === "action" && m.approval?.status === "pending",
  );
  const card = cards[cards.length - 1];
  expect(card, "no pending action card").toBeTruthy();
  return card as ApprovalMessage;
}

export async function waitIdle(request: APIRequestContext, id: string): Promise<void> {
  await expect
    .poll(async () => (await conversation(request, id)).active_run_id, { timeout: 30_000 })
    .toBeNull();
}
