"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useEffect, useState, type ReactNode } from "react";

export function Providers({ children }: { children: ReactNode }) {
  const [client] = useState(
    () => new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 5_000 } } }),
  );
  // Lets a browser test wait until React is interactive before it clicks.
  useEffect(() => {
    document.documentElement.dataset.hydrated = "true";
  }, []);
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}
