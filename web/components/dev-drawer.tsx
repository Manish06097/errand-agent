"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Wrench } from "lucide-react";
import { useState } from "react";
import { api } from "@/lib/api";
import { Button } from "./ui/button";
import { Sheet } from "./ui/sheet";

// Only rendered when GET /api/meta says the API runs with ERRAND_DEV=1. "Kill agent
// mid-booking" is not here: it needs M6's fault points and supervisor restart.
export function DevDrawer() {
  const [open, setOpen] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [scenario, setScenario] = useState("");
  const [booking, setBooking] = useState("");
  const qc = useQueryClient();
  const scenarios = useQuery({ queryKey: ["dev", "scenarios"], queryFn: api.dev.scenarios, enabled: open });
  const bookings = useQuery({ queryKey: ["dev", "bookings"], queryFn: api.dev.bookings, enabled: open });

  async function run(label: string, fn: () => Promise<{ ok: boolean; status: number }>) {
    const result = await fn();
    setMessage(result.ok ? `${label}: done` : `${label}: failed (${result.status})`);
    void qc.invalidateQueries({ queryKey: ["dev"] });
  }

  const confirmed = (bookings.data ?? []).filter((b) => b.status === "confirmed");

  return (
    <>
      <Button
        variant="outline"
        size="sm"
        data-testid="dev-open"
        className="shrink-0"
        onClick={() => setOpen(true)}
      >
        <Wrench className="h-3.5 w-3.5" aria-hidden /> Dev
      </Button>
      <Sheet open={open} onOpenChange={setOpen} title="Dev drawer" side="left" testId="dev-drawer">
        <div className="flex flex-col gap-4 text-sm">
          <section className="flex flex-col gap-2">
            <label htmlFor="dev-scenario" className="text-xs font-medium">
              Scenario
            </label>
            <select
              id="dev-scenario"
              data-testid="dev-scenario"
              value={scenario}
              onChange={(e) => setScenario(e.target.value)}
              className="rounded-md border border-neutral-300 bg-white px-2 py-1.5 dark:border-neutral-700 dark:bg-neutral-900"
            >
              <option value="">Choose a scenario</option>
              {(scenarios.data?.scenarios ?? []).map((s) => (
                <option key={s.id} value={s.id}>
                  {s.id}
                </option>
              ))}
            </select>
            <Button
              size="sm"
              data-testid="dev-load"
              disabled={!scenario}
              onClick={() => run("Load scenario", () => api.dev.load(scenario))}
            >
              Load world
            </Button>
          </section>
          <Button data-testid="dev-advance" onClick={() => run("Clock +11 min", () => api.dev.advance(660))}>
            +11 min
          </Button>
          <Button
            data-testid="dev-next-book"
            onClick={() => run("Next book", () => api.dev.nextBook("timeout_booked"))}
          >
            Next book times out after success
          </Button>
          <section className="flex flex-col gap-2">
            <label htmlFor="dev-booking" className="text-xs font-medium">
              Booking
            </label>
            <select
              id="dev-booking"
              data-testid="dev-booking"
              value={booking}
              onChange={(e) => setBooking(e.target.value)}
              className="rounded-md border border-neutral-300 bg-white px-2 py-1.5 dark:border-neutral-700 dark:bg-neutral-900"
            >
              <option value="">Choose a booking</option>
              {confirmed.map((b) => (
                <option key={b.booking_id} value={b.booking_id}>
                  {b.booking_id} ({b.provider_id})
                </option>
              ))}
            </select>
            <Button
              size="sm"
              data-testid="dev-provider-cancel"
              disabled={!booking}
              onClick={() => run("Provider cancels", () => api.dev.providerCancel(booking))}
            >
              Provider cancels booking
            </Button>
          </section>
          {message && (
            <p role="status" data-testid="dev-message" className="text-xs text-neutral-600 dark:text-neutral-400">
              {message}
            </p>
          )}
        </div>
      </Sheet>
    </>
  );
}
