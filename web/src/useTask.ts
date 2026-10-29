import { useCallback, useEffect, useState } from "react";
import { ApiError, request } from "./api";
import type { Detail, Event, Review } from "./types";

export function useTask(taskId: string) {
  const [detail, setDetail] = useState<Detail | null>(null);
  const [events, setEvents] = useState<Event[]>([]);
  const [reviews, setReviews] = useState<Review[]>([]);
  const [error, setError] = useState<number | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const refresh = useCallback(() => setRefreshKey((key) => key + 1), []);
  useEffect(() => {
    setDetail(null);
    setEvents([]);
    setReviews([]);
    setError(null);
  }, [taskId]);
  useEffect(() => {
    if (!taskId) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    let after = 0;
    async function poll() {
      try {
        const current = await request<Detail>(
          `/investigations/${encodeURIComponent(taskId)}`,
          controller.signal,
        );
        if (controller.signal.aborted) return;
        setDetail(current);
        let moreEvents = false;
        // Drain bounded pages incrementally; later cycles continue if history is long.
        for (let page = 0; page < 5; page++) {
          const batch = await request<{
            events: Event[];
            next_after: number | null;
          }>(
            `/investigations/${encodeURIComponent(taskId)}/events?after=${after}&limit=100`,
            controller.signal,
          );
          if (controller.signal.aborted) return;
          if (batch.events.length) {
            after = batch.events.at(-1)!.event_id;
            setEvents((old) => [
              ...new Map(
                [...old, ...batch.events].map((event) => [
                  event.event_id,
                  event,
                ]),
              ).values(),
            ]);
          }
          moreEvents = batch.next_after !== null;
          if (batch.next_after === null) break;
        }
        if (current.status === "waiting_review") {
          const pending = await request<Review[]>(
            `/investigations/${encodeURIComponent(taskId)}/reviews`,
            controller.signal,
          );
          if (controller.signal.aborted) return;
          setReviews(pending);
        } else setReviews([]);
        setError(null);
        if (
          moreEvents ||
          current.status === "pending" ||
          current.status === "running" ||
          current.status === "waiting_review"
        ) {
          timer = setTimeout(poll, moreEvents ? 0 : 2000);
        }
      } catch (exc) {
        if (controller.signal.aborted) return;
        const status = exc instanceof ApiError ? exc.status : 0;
        setError(status);
        if (status !== 404) timer = setTimeout(poll, 4000);
      }
    }
    void poll();
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [taskId, refreshKey]);
  // Prevent a previous task's data flashing under a newly selected ID.
  return {
    detail: detail?.task_id === taskId ? detail : null,
    events,
    reviews,
    error,
    refresh,
  };
}
