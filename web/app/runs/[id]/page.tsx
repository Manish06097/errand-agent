import { RunPage } from "@/components/run-page";

export default async function RunRoute({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <RunPage runId={id} />;
}
