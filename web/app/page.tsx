import { ApiHealth } from "./api-health";

export default function Home() {
  return (
    <main className="mx-auto flex max-w-2xl flex-col gap-6 px-4 py-16">
      <header className="flex flex-col gap-2">
        <h1 className="text-2xl font-semibold tracking-tight">Errand agent</h1>
        <p className="text-sm text-neutral-600 dark:text-neutral-400">
          App shell. Chat, cards and the trace panel arrive in later milestones.
        </p>
      </header>
      <ApiHealth />
    </main>
  );
}
