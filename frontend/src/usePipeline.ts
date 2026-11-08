import { useCallback, useEffect, useRef, useState } from "react";
import { api, type Dashboard, type Health, type Status } from "./api";

export interface PipelineView {
  health: Health | "down" | null;
  status: Status | null;
  dash: Dashboard | null;
  /** Polling failed: the API is unreachable. */
  offline: boolean;
  refresh: () => void;
}

const POLL_MS = 1000;

export function usePipeline(): PipelineView {
  const [health, setHealth] = useState<PipelineView["health"]>(null);
  const [status, setStatus] = useState<Status | null>(null);
  const [dash, setDash] = useState<Dashboard | null>(null);
  const [offline, setOffline] = useState(false);
  const alive = useRef(true);

  const poll = useCallback(async () => {
    try {
      const [s, d] = await Promise.all([api.status(), api.dashboard()]);
      if (!alive.current) return;
      setStatus(s);
      setDash(d);
      setOffline(false);
    } catch {
      if (alive.current) setOffline(true);
    }
  }, []);

  useEffect(() => {
    alive.current = true;
    poll();
    const id = setInterval(poll, POLL_MS);
    return () => {
      alive.current = false;
      clearInterval(id);
    };
  }, [poll]);

  useEffect(() => {
    const check = () =>
      api
        .health()
        .then((h) => alive.current && setHealth(h))
        .catch(() => alive.current && setHealth("down"));
    check();
    const id = setInterval(check, 5000);
    return () => clearInterval(id);
  }, []);

  return { health, status, dash, offline, refresh: poll };
}
