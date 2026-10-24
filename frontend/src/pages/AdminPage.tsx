import { useCallback, useEffect, useState } from "react";
import { ApiError, api, isActive } from "../api";
import SettingsForm from "../components/SettingsForm";
import type { ConfigResponse, ConfigValues, FieldError, JobsResponse, Status } from "../types";

const SECRET_HELP: Record<string, string> = {
  CONGRESS_GOV_API_KEY: "Required to fetch House votes, bills and sponsorships.",
  ANTHROPIC_API_KEY: "Only when llm.provider is anthropic.",
  SMTP_PASSWORD: "Only when email is enabled.",
};

const when = (iso: string | null) => (iso ? new Date(iso).toLocaleString() : "");

export default function AdminPage() {
  const [config, setConfig] = useState<ConfigResponse | null>(null);
  const [draft, setDraft] = useState<ConfigValues | null>(null);
  const [errors, setErrors] = useState<FieldError[]>([]);
  const [message, setMessage] = useState<{ kind: "info" | "error"; text: string } | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    api.config().then(
      (c) => {
        setConfig(c);
        setDraft(c.values);
      },
      (e) => setMessage({ kind: "error", text: (e as Error).message }),
    );
  }, []);

  const dirty = !!config && !!draft && JSON.stringify(config.values) !== JSON.stringify(draft);

  const save = async () => {
    if (!draft) return;
    setSaving(true);
    setErrors([]);
    try {
      const c = await api.saveConfig(draft);
      setConfig(c);
      setDraft(c.values);
      setMessage({ kind: "info", text: `Saved to ${c.path}.${c.job ? ` ${c.job.label} in the background.` : ""}` });
    } catch (e) {
      const err = e as ApiError;
      setErrors(err.fields ?? []);
      setMessage({ kind: "error", text: `Not saved: ${err.message}` });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div>
      <div className="toolbar">
        <div>
          <h1>Admin</h1>
          <p className="muted">Settings from {config?.path ?? "config.toml"}, plus data jobs.</p>
        </div>
      </div>
      {message && <div className={`banner ${message.kind}`}>{message.text}</div>}

      <StatusAndJobs />

      {config && (
        <section className="card">
          <h2>Secrets</h2>
          <p className="muted">
            Read from the server&rsquo;s environment when <code>tracker serve</code> starts; never stored in
            config.toml or shown here. Set them in the shell that runs the server, then restart it.
          </p>
          <ul className="secrets">
            {Object.entries(config.secrets).map(([k, set]) => (
              <li key={k}>
                <span className={set ? "pill ok" : "pill"}>{set ? "set" : "not set"}</span>
                <span className="mono">{k}</span> <span className="muted small">{SECRET_HELP[k]}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {config && draft && (
        <>
          <div className="sticky-actions">
            <span className="muted small">{dirty ? "Unsaved changes" : "All changes saved"}</span>
            <button className="btn" onClick={() => (setDraft(config.values), setErrors([]))} disabled={!dirty || saving}>
              Revert
            </button>
            <button className="btn primary" onClick={save} disabled={!dirty || saving}>
              {saving ? "Saving…" : "Save settings"}
            </button>
          </div>
          <SettingsForm schema={config.schema} values={draft} errors={errors} onChange={setDraft} />
        </>
      )}
    </div>
  );
}

function StatusAndJobs() {
  const [status, setStatus] = useState<Status | null>(null);
  const [jobs, setJobs] = useState<JobsResponse | null>(null);
  const [since, setSince] = useState("");
  const [allRecords, setAllRecords] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [s, j] = await Promise.all([api.status(), api.jobs()]);
      setStatus(s);
      setJobs(j);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  const busy = jobs?.jobs.some(isActive) ?? false;
  useEffect(() => {
    if (!busy) return;
    const t = setInterval(refresh, 2000);
    return () => clearInterval(t);
  }, [busy, refresh]);

  const start = async (kind: string, extra: Record<string, unknown> = {}) => {
    setError(null);
    try {
      await api.startJob(kind, extra);
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <section className="card">
      <h2>Data</h2>
      {error && <div className="banner error">{error}</div>}
      {status && (
        <>
          {status.stale.map((w) => (
            <div className="banner warn" key={w}>
              {w}
            </div>
          ))}
          <dl className="facts">
            <dt>House votes</dt>
            <dd>
              {status.votes.house?.votes ?? 0}
              {status.votes.house && <span className="muted"> · latest {when(status.votes.house.latest)}</span>}
            </dd>
            <dt>Senate votes</dt>
            <dd>
              {status.votes.senate?.votes ?? 0}
              {status.votes.senate && <span className="muted"> · latest {when(status.votes.senate.latest)}</span>}
            </dd>
            <dt>Summaries</dt>
            <dd>{status.provider}</dd>
            <dt>Database</dt>
            <dd className="mono">{status.db_path}</dd>
          </dl>
        </>
      )}
      <div className="job-actions">
        <button className="btn primary" onClick={() => start("update")} disabled={busy}>
          Update data
        </button>
        <span className="inline-group">
          <input className="input narrow" type="date" min="2025-01-03" value={since} onChange={(e) => setSince(e.target.value)} />
          <button className="btn" onClick={() => start("update", { since })} disabled={busy || !since}>
            Backfill from date
          </button>
        </span>
        <span className="inline-group">
          <label className="check small">
            <input type="checkbox" checked={allRecords} onChange={(e) => setAllRecords(e.target.checked)} /> all records
          </label>
          <button className="btn" onClick={() => start("normalize", { all_records: allRecords })} disabled={busy}>
            Re-normalize
          </button>
        </span>
        <button className="btn" onClick={() => start("crosswalk")} disabled={busy}>
          Fetch member list
        </button>
      </div>
      <p className="muted small">
        Update fetches new votes, bills and sponsorships, then normalizes them. Jobs run one at a time in the background.
      </p>

      {jobs && jobs.jobs.length > 0 && (
        <>
          <h3>This session</h3>
          <table className="table">
            <tbody>
              {jobs.jobs.map((j) => (
                <tr key={j.id}>
                  <td>
                    <span className={`pill ${j.status}`}>
                      {isActive(j) && <span className="spinner" aria-hidden />} {j.status}
                    </span>
                  </td>
                  <td>
                    {j.label}
                    {j.message && <div className="small error-text">{j.message}</div>}
                  </td>
                  <td className="muted small nowrap">{when(j.finished_at ?? j.started_at ?? j.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      {jobs && (
        <details className="history">
          <summary>Run history ({jobs.runs.length} most recent)</summary>
          <table className="table">
            <tbody>
              {jobs.runs.map((r) => (
                <tr key={r.id}>
                  <td>
                    <span className={`pill ${r.status ?? "running"}`}>{r.status ?? "running"}</span>
                  </td>
                  <td className="mono">{r.command}</td>
                  <td className="muted small nowrap">{when(r.started_at)}</td>
                  <td className="muted small stats-cell" title={r.stats_json ?? ""}>
                    {r.stats_json}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}
    </section>
  );
}
