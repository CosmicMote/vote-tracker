import { useCallback, useEffect, useRef, useState } from "react";
import { navigate } from "../App";
import { api, isActive } from "../api";
import DigestView from "../components/DigestView";
import WeekPicker from "../components/WeekPicker";
import type { DigestResponse, WeeksResponse } from "../types";

const POLL_MS = 2500;

/** Monday of the week containing an ISO date (YYYY-MM-DD); weeks run Monday to Sunday. */
function mondayOf(iso: string): string {
  const d = new Date(`${iso}T00:00:00Z`);
  if (Number.isNaN(d.getTime())) return iso;
  d.setUTCDate(d.getUTCDate() - ((d.getUTCDay() + 6) % 7));
  return d.toISOString().slice(0, 10);
}

export default function DigestPage({ week }: { week?: string }) {
  const [weeks, setWeeks] = useState<WeeksResponse | null>(null);
  const [data, setData] = useState<DigestResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    api.weeks().then(setWeeks, (e) => setError(String(e.message ?? e)));
  }, []);

  const selected = week ? mondayOf(week) : weeks?.default;

  // Keep the URL on the week's Monday, so it matches the week picker (no new history entry).
  useEffect(() => {
    if (week && selected && week !== selected) window.location.replace(`#/digest/${selected}`);
  }, [week, selected]);

  // Responses can arrive out of order when switching weeks quickly; only the latest selection counts.
  const current = useRef(selected);
  current.current = selected;

  const load = useCallback(
    async (quiet = false) => {
      if (!selected) return;
      const wanted = selected;
      if (!quiet) setLoading(true);
      try {
        const resp = await api.digest(wanted);
        if (current.current !== wanted) return;
        setData(resp);
        setError(null);
      } catch (e) {
        if (current.current === wanted) setError((e as Error).message);
      } finally {
        if (current.current === wanted) setLoading(false);
      }
    },
    [selected],
  );

  useEffect(() => {
    load();
  }, [load]);

  // While summaries are being written in the background, refresh quietly.
  const job = data?.week.start === selected ? data?.summaries.job : null;
  useEffect(() => {
    if (!isActive(job)) return;
    const t = setTimeout(() => load(true), POLL_MS);
    return () => clearTimeout(t);
  }, [job, data, load]);

  const onSummarize = async () => {
    if (!selected) return;
    try {
      await api.summarize(selected);
      await load(true);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  if (!weeks) return error ? <div className="banner error">{error}</div> : <p className="muted">Loading…</p>;

  return (
    <div>
      <div className="toolbar">
        <div>
          <h1>Weekly digest</h1>
          <p className="muted">{data?.week.label ?? ""}</p>
        </div>
        <div className="toolbar-actions">
          <WeekPicker weeks={weeks.weeks} value={selected!} onChange={(w) => navigate({ page: "digest", week: w })} />
        </div>
      </div>

      {error && <div className="banner error">{error}</div>}
      {data && <SummaryStatus data={data} onRetry={onSummarize} />}
      {loading && !data && <p className="muted">Analyzing week…</p>}
      {data && data.digest.members.length === 0 && (
        <div className="banner info">
          <span>
            No members are selected yet. Choose who the digest covers on the <a href="#/members">Members</a> tab.
          </span>
        </div>
      )}
      {data && data.digest.members.length > 0 && (
        <div className={loading ? "dim" : ""}>
          <DigestView key={data.week.start} digest={data.digest} />
        </div>
      )}
    </div>
  );
}

function SummaryStatus({ data, onRetry }: { data: DigestResponse; onRetry: () => void }) {
  const { pending, job, waiting_for } = data.summaries;
  if (job?.status === "queued") {
    return (
      <div className="banner info">
        <span className="spinner" aria-hidden />
        <span>
          Summaries for {pending} bill{pending === 1 ? "" : "s"} are queued
          {waiting_for ? <> behind &ldquo;{waiting_for}&rdquo;</> : null}. They start when it finishes; excerpts are
          shown until then.
        </span>
      </div>
    );
  }
  if (isActive(job)) {
    return (
      <div className="banner info">
        <span className="spinner" aria-hidden />
        <span>
          Writing plain-language summaries for {pending} bill{pending === 1 ? "" : "s"}… They appear here as they
          finish.
        </span>
      </div>
    );
  }
  if (pending > 0) {
    return (
      <div className="banner warn">
        {pending} bill{pending === 1 ? " is" : "s are"} showing an excerpt instead of a summary
        {job?.status === "failed" && job.message ? ` (${job.message})` : ""}.{" "}
        <button className="btn small" onClick={onRetry}>
          Retry summaries
        </button>
      </div>
    );
  }
  return null;
}
