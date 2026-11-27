import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { Center, Loader } from "@mantine/core";
import type { PortalApi } from "./types";
import { initApi } from "./index";

interface ApiCtx {
  api: PortalApi;
  dataSource: "mock" | "live";
  fallback: boolean;
}

const Ctx = createContext<ApiCtx | null>(null);

/** Resolves the PortalApi (mock or live) once, then renders children with it in context. */
export function ApiProvider({ children }: { children: ReactNode }) {
  const [ctx, setCtx] = useState<ApiCtx | null>(null);

  useEffect(() => {
    initApi().then(setCtx);
  }, []);

  if (!ctx) {
    return (
      <Center h="100vh">
        <Loader />
      </Center>
    );
  }
  return <Ctx.Provider value={ctx}>{children}</Ctx.Provider>;
}

export function useApi(): PortalApi {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useApi must be used within ApiProvider");
  return ctx.api;
}

export function useDataSource(): "mock" | "live" {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useDataSource must be used within ApiProvider");
  return ctx.dataSource;
}

/** Whether the app fell back to mock after a live config with no apiBase (a misconfiguration). */
export function useFallback(): boolean {
  const ctx = useContext(Ctx);
  if (!ctx) throw new Error("useFallback must be used within ApiProvider");
  return ctx.fallback;
}
