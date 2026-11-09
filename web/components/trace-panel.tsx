"use client";

import { useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import { api } from "@/lib/api";
import {
  FILTERS,
  buildRows,
  categoryOf,
  moneyUsd,
  num,
  obj,
  rowDetail,
  rowLabel,
  totalsOf,
  type FilterKey,
} from "@/lib/trace";
import type { TraceRecord } from "@/lib/types";
import { cn } from "@/lib/utils";
import { Button } from "./ui/button";

function useTraceRecords(
  runId: string,
  live: Record<string, TraceRecord[]>,
  running: boolean,
): TraceRecord[] {
  const stored = useQuery({
    queryKey: ["trace", runId],
    queryFn: () => api.trace(runId),
    enabled: !running,
  });
  return running ? (live[runId] ?? []) : (stored.data?.records ?? live[runId] ?? []);
}

function Detail({ record, manifest }: { record: TraceRecord; manifest: TraceRecord | null }) {
  const response = obj(record, "response");
  const usage = obj(record, "usage");
  return (
    <div className="mt-1 flex flex-col gap-2 rounded bg-neutral-50 p-2 text-xs dark:bg-neutral-800/60">
      {usage && (
        <p data-testid="model-usage">
          in {Number(usage["input_uncached"] ?? 0)} + cached {Number(usage["cache_read"] ?? 0)}, out{" "}
          {Number(usage["output"] ?? 0)}, {moneyUsd(num(record, "cost_usd") ?? 0)}
        </p>
      )}
      {manifest && (
        <div data-testid="context-manifest">
          <p className="font-medium">Context manifest</p>
          <pre className="overflow-x-auto whitespace-pre-wrap break-all">
            {JSON.stringify(obj(manifest, "manifest"), null, 2)}
          </pre>
        </div>
      )}
      {response && (
        <div>
          <p className="font-medium">Response</p>
          <pre className="overflow-x-auto whitespace-pre-wrap break-all">
            {JSON.stringify(response, null, 2)}
          </pre>
        </div>
      )}
    </div>
  );
}

export function RunTrace({
  runId,
  live,
  running,
  turn,
}: {
  runId: string;
  live: Record<string, TraceRecord[]>;
  running: boolean;
  turn: number;
}) {
  const records = useTraceRecords(runId, live, running);
  const [off, setOff] = useState<ReadonlySet<FilterKey>>(new Set());
  const [open, setOpen] = useState<string | null>(null);
  const rows = useMemo(() => buildRows(records), [records]);
  const totals = useMemo(() => totalsOf(records), [records]);
  const maxLatency = Math.max(1, ...records.map((r) => num(r, "latency_ms") ?? 0));
  const manifests = new Map<string, TraceRecord>();
  for (const r of records) if (r.type === "context.built") manifests.set(r.span_id, r);

  const visible = rows.filter(({ record }) => {
    const category = categoryOf(record.type);
    return category === null || !off.has(category);
  });

  return (
    <section data-testid="trace-run" data-run-id={runId} className="border-b border-neutral-200 py-2 dark:border-neutral-800">
      <header className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-xs font-semibold">
          Turn {turn} <span className="font-normal text-neutral-500">{runId}</span>
          {running && <span className="ml-1 text-indigo-600">running</span>}
        </h3>
        <span data-testid="trace-totals" className="text-xs text-neutral-600 dark:text-neutral-400">
          {totals.tokensIn}/{totals.tokensOut} tok · {moneyUsd(totals.costUsd)}
        </span>
      </header>
      <div className="mt-1 flex flex-wrap gap-1">
        {FILTERS.map((f) => (
          <button
            key={f.key}
            type="button"
            data-testid={`chip-${f.key}`}
            aria-pressed={!off.has(f.key)}
            onClick={() =>
              setOff((prev) => {
                const next = new Set(prev);
                if (next.has(f.key)) next.delete(f.key);
                else next.add(f.key);
                return next;
              })
            }
            className={cn(
              "rounded-full border px-2 py-0.5 text-xs",
              off.has(f.key)
                ? "border-neutral-300 text-neutral-400 line-through dark:border-neutral-700"
                : "border-indigo-300 bg-indigo-50 text-indigo-900 dark:border-indigo-700 dark:bg-indigo-950 dark:text-indigo-200",
            )}
          >
            {f.label}
          </button>
        ))}
      </div>
      <ul className="mt-2 flex flex-col gap-0.5" data-testid="trace-rows">
        {visible.map(({ record, depth }) => {
          const latency = num(record, "latency_ms");
          const expandable = record.type === "model.call";
          const key = `${record.run_id}:${record.seq}`;
          return (
            <li key={key} data-testid="trace-row" data-type={record.type}>
              <button
                type="button"
                disabled={!expandable}
                onClick={() => setOpen((prev) => (prev === key ? null : key))}
                style={{ paddingLeft: `${depth * 12}px` }}
                className="flex w-full items-center gap-2 text-left text-xs enabled:hover:bg-neutral-100 dark:enabled:hover:bg-neutral-800"
              >
                <span className="w-7 shrink-0 text-right text-neutral-400">{record.seq}</span>
                <span className="min-w-0 flex-1 truncate">
                  <span className="font-medium">{rowLabel(record)}</span>
                  {rowDetail(record) && (
                    <span className="text-neutral-500"> {rowDetail(record)}</span>
                  )}
                </span>
                {latency !== null && (
                  <span
                    aria-hidden
                    className="h-1.5 shrink-0 rounded bg-indigo-400"
                    style={{ width: `${Math.max(2, Math.round((latency / maxLatency) * 48))}px` }}
                  />
                )}
              </button>
              {open === key && expandable && (
                <Detail record={record} manifest={manifests.get(record.span_id) ?? null} />
              )}
            </li>
          );
        })}
      </ul>
    </section>
  );
}

export function TracePanel({
  runs,
  live,
  streamRunId,
  langfuseUrl,
}: {
  runs: { run_id: string }[];
  live: Record<string, TraceRecord[]>;
  streamRunId: string | null;
  langfuseUrl: string | null;
}) {
  const ordered = [...runs].reverse();
  return (
    <div data-testid="trace-panel" className="flex flex-col">
      {langfuseUrl && (
        <Button asChild size="sm" variant="ghost" className="self-start">
          <a href={langfuseUrl} target="_blank" rel="noreferrer" data-testid="langfuse-link">
            Open in Langfuse
          </a>
        </Button>
      )}
      {ordered.length === 0 && <p className="text-xs text-neutral-500">No runs yet.</p>}
      {ordered.map((run) => (
        <RunTrace
          key={run.run_id}
          runId={run.run_id}
          live={live}
          running={run.run_id === streamRunId}
          turn={runs.findIndex((r) => r.run_id === run.run_id) + 1}
        />
      ))}
      <p className="mt-2 text-xs text-neutral-500">
        {ordered[0] && (
          <a className="underline" href={`/runs/${ordered[0].run_id}`}>
            Full trace of the latest run
          </a>
        )}
      </p>
    </div>
  );
}

