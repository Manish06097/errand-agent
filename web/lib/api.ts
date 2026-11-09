import { agentClock, AGENT_CLOCK_HEADER, parseClockHeader } from "./agent-clock";
import type { MemoryGroups, MemoryRow } from "./memory";
import type {
  ActionIn,
  ConversationOut,
  ConversationSummary,
  DecisionIn,
  DevBooking,
  MetaOut,
  NewMessage,
  NotificationsOut,
  ScenariosOut,
  TraceOut,
} from "./types";

// Every /api response carries the agent clock; reading it here keeps the clock fresh
// without any component having to ask.
export type ApiResult<T> = { ok: boolean; status: number; body: T };

type ErrorBody = { error?: string; detail?: string; status?: string; run_id?: string | null };

async function call<T>(path: string, init?: RequestInit): Promise<ApiResult<T>> {
  const response = await fetch(path, {
    cache: "no-store",
    ...init,
    headers: { "content-type": "application/json", ...init?.headers },
  });
  const reading = parseClockHeader(response.headers.get(AGENT_CLOCK_HEADER));
  if (reading) agentClock.sync(reading);
  let body: unknown = null;
  const text = await response.text();
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = null;
    }
  }
  return { ok: response.ok, status: response.status, body: body as T };
}

function post<T>(path: string, payload: unknown): Promise<ApiResult<T>> {
  return call<T>(path, { method: "POST", body: JSON.stringify(payload) });
}

type MemoryAnswer = { memory?: MemoryRow; error?: string; detail?: string; current?: MemoryRow };

export async function getOk<T>(path: string): Promise<T> {
  const result = await call<T>(path);
  if (!result.ok) throw new Error(`GET ${path} answered ${result.status}`);
  return result.body;
}

export const api = {
  meta: () => getOk<MetaOut>("/api/meta"),
  health: () => getOk<{ status: string; langfuse?: { state?: string; host?: string } }>("/api/health"),
  listConversations: () => getOk<ConversationSummary[]>("/api/conversations"),
  createConversation: () => post<{ conversation_id: string }>("/api/conversations", undefined),
  conversation: async (id: string): Promise<ConversationOut> => {
    const body = await getOk<ConversationOut>(`/api/conversations/${id}`);
    agentClock.sync({
      mode: body.clock.mode,
      nowMs: body.clock.now_ms,
      offsetMs: body.clock.offset_ms,
    });
    return body;
  },
  trace: (runId: string) => getOk<TraceOut>(`/api/runs/${runId}/trace`),
  sendMessage: (conversationId: string, body: NewMessage) =>
    post<{ run_id?: string; routed_as?: string } & ErrorBody>(
      `/api/conversations/${conversationId}/messages`,
      body,
    ),
  decide: (approvalId: string, body: DecisionIn) =>
    post<{ run_id?: string } & ErrorBody>(
      `/api/approvals/${approvalId}/decision`,
      body,
    ),
  action: (conversationId: string, body: ActionIn) =>
    post<{ run_id?: string } & ErrorBody>(`/api/conversations/${conversationId}/actions`, body),
  memory: {
    list: () => getOk<MemoryGroups>("/api/memory"),
    correct: (id: string, value: unknown, clientActionId: string) =>
      call<MemoryAnswer>(`/api/memory/${id}`, {
        method: "PATCH",
        body: JSON.stringify({ value, client_action_id: clientActionId }),
      }),
    forget: (id: string, clientActionId: string) =>
      call<MemoryAnswer>(
        `/api/memory/${id}?client_action_id=${encodeURIComponent(clientActionId)}`,
        { method: "DELETE" },
      ),
    keep: (id: string, clientActionId: string) =>
      post<MemoryAnswer>(`/api/memory/${id}/confirm`, { client_action_id: clientActionId }),
    dismiss: (id: string, clientActionId: string) =>
      post<MemoryAnswer>(`/api/memory/${id}/dismiss`, { client_action_id: clientActionId }),
    undo: (id: string, clientActionId: string) =>
      post<MemoryAnswer>(`/api/memory/${id}/undo`, { client_action_id: clientActionId }),
  },
  notifications: () => getOk<NotificationsOut>("/api/notifications"),
  markRead: (notificationId: string) =>
    post<{ unread?: number }>(`/api/notifications/${notificationId}/read`, undefined),
  rendered: (messageId: string, clientTs: string) =>
    post<unknown>(`/api/messages/${messageId}/rendered`, { client_ts: clientTs }),
  dev: {
    scenarios: () => getOk<ScenariosOut>("/api/dev/scenarios"),
    load: (scenarioId: string | null) => post<unknown>("/api/dev/scenario", { scenario_id: scenarioId }),
    advance: (seconds: number) => post<unknown>("/api/dev/clock/advance", { seconds }),
    nextBook: (outcome: "timeout_booked") => post<unknown>("/api/dev/next-book", { outcome }),
    bookings: () => getOk<DevBooking[]>("/api/dev/bookings"),
    providerCancel: (bookingId: string) =>
      post<unknown>("/api/dev/provider-cancel", { booking_id: bookingId }),
    reconcile: () => post<unknown>("/api/dev/reconciler/tick", {}),
  },
};
