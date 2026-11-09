"use client";

import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import { api } from "@/lib/api";
import { RunTrace } from "./trace-panel";
import { Button } from "./ui/button";

// /runs/[id]: the whole trace of one run, and its JSONL download.
export function RunPage({ runId }: { runId: string }) {
  const trace = useQuery({ queryKey: ["trace", runId], queryFn: () => api.trace(runId) });
  return (
    <main className="mx-auto flex max-w-3xl flex-col gap-4 px-4 py-8">
      <header className="flex flex-wrap items-center justify-between gap-2">
        <h1 className="text-lg font-semibold">Run {runId}</h1>
        <div className="flex gap-2">
          {trace.data && (
            <Button asChild size="sm">
              <Link href={`/c/${trace.data.conversation_id}`}>Back to the chat</Link>
            </Button>
          )}
          <Button asChild size="sm" variant="primary">
            <a href={`/api/runs/${runId}/trace.jsonl`} download data-testid="download-jsonl">
              Download JSONL
            </a>
          </Button>
        </div>
      </header>
      {trace.isError && <p role="alert">Could not load this run.</p>}
      {trace.data && (
        <>
          <p className="text-sm text-neutral-600 dark:text-neutral-400">
            {trace.data.status}
            {trace.data.outcome ? ` · ${trace.data.outcome}` : ""} · {trace.data.records.length} records
            {trace.data.truncated ? " · last line cut off" : ""}
          </p>
          <RunTrace runId={runId} live={{}} running={false} turn={1} />
        </>
      )}
    </main>
  );
}
