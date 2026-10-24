import { useEffect, useMemo, useState } from "react";
import { api } from "../api";
import type { Legislator, MembersResponse } from "../types";

const describe = (p: Legislator) =>
  `${p.chamber === "senate" ? "Sen." : "Rep."} ${p.name} (${p.party}-${p.state}${p.district ? `-${p.district}` : ""})`;

export default function MembersPage() {
  const [all, setAll] = useState<Legislator[] | null>(null);
  const [saved, setSaved] = useState<MembersResponse | null>(null);
  const [draft, setDraft] = useState<string[]>([]);
  const [state, setState] = useState("");
  const [query, setQuery] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    Promise.all([api.legislators(), api.members()]).then(
      ([l, m]) => {
        setAll(l.legislators);
        setSaved(m);
        setDraft(m.tracked);
        setState(m.members[0]?.state ?? "");
      },
      (e) => setError((e as Error).message),
    );
  }, []);

  const byId = useMemo(() => new Map((all ?? []).map((p) => [p.bioguide, p])), [all]);
  const states = useMemo(() => [...new Set((all ?? []).map((p) => p.state))].sort(), [all]);
  const dirty = saved !== null && draft.join() !== saved.tracked.join();

  const candidates = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (q) return (all ?? []).filter((p) => p.name.toLowerCase().includes(q) || p.bioguide.toLowerCase() === q).slice(0, 30);
    return (all ?? []).filter((p) => p.state === state);
  }, [all, state, query]);

  const toggle = (id: string) => setDraft((d) => (d.includes(id) ? d.filter((x) => x !== id) : [...d, id]));
  const move = (id: string, by: number) =>
    setDraft((d) => {
      const i = d.indexOf(id);
      const j = i + by;
      if (i < 0 || j < 0 || j >= d.length) return d;
      const out = [...d];
      [out[i], out[j]] = [out[j], out[i]];
      return out;
    });

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      const r = await api.saveMembers(draft);
      setSaved(r);
      setDraft(r.tracked);
      setNotice(
        r.job
          ? `Saved. Fetching sponsorships for the new members in the background (${r.job.label}).`
          : "Saved. Digests will use the new selection.",
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  if (!all || !saved) return error ? <div className="banner error">{error}</div> : <p className="muted">Loading…</p>;

  const senators = draft.filter((id) => byId.get(id)?.chamber === "senate").length;
  return (
    <div>
      <div className="toolbar">
        <div>
          <h1>Members</h1>
          <p className="muted">Who your digest covers. Saved to config.toml, so you only choose once.</p>
        </div>
        <div className="toolbar-actions">
          <button className="btn" onClick={() => setDraft(saved.tracked)} disabled={!dirty || saving}>
            Revert
          </button>
          <button className="btn primary" onClick={save} disabled={!dirty || saving || draft.length === 0}>
            {saving ? "Saving…" : "Save"}
          </button>
        </div>
      </div>
      {error && <div className="banner error">{error}</div>}
      {notice && <div className="banner info">{notice}</div>}
      {senators > 2 && (
        <div className="banner warn">
          Delegation splits compare the first two senators in the list. Use the arrows to choose which two.
        </div>
      )}

      <div className="grid-2">
        <section className="card">
          <h2>Selected ({draft.length})</h2>
          {draft.length === 0 && <p className="muted">Select at least one member.</p>}
          <ul className="member-list">
            {draft.map((id, i) => {
              const p = byId.get(id);
              return (
                <li key={id}>
                  <span>{p ? describe(p) : `${id} (not a current member)`}</span>
                  <span className="row-actions">
                    <button className="btn icon small" onClick={() => move(id, -1)} disabled={i === 0} title="Move up">
                      ↑
                    </button>
                    <button
                      className="btn icon small"
                      onClick={() => move(id, 1)}
                      disabled={i === draft.length - 1}
                      title="Move down"
                    >
                      ↓
                    </button>
                    <button className="btn small" onClick={() => toggle(id)}>
                      Remove
                    </button>
                  </span>
                </li>
              );
            })}
          </ul>
          <p className="muted small">
            Votes for any member are already stored. A newly added member&rsquo;s introduced and cosponsored bills are
            fetched after you save (needs CONGRESS_GOV_API_KEY).
          </p>
        </section>

        <section className="card">
          <h2>Add members</h2>
          <div className="filters">
            <label>
              State{" "}
              <select className="select" value={state} onChange={(e) => (setState(e.target.value), setQuery(""))}>
                {states.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            </label>
            <input
              className="input"
              type="search"
              placeholder="…or search by name or Bioguide ID"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
            />
          </div>
          <ul className="member-list">
            {candidates.map((p) => (
              <li key={p.bioguide}>
                <label className="check">
                  <input type="checkbox" checked={draft.includes(p.bioguide)} onChange={() => toggle(p.bioguide)} />
                  {describe(p)}
                </label>
                <span className="muted small mono">{p.bioguide}</span>
              </li>
            ))}
            {candidates.length === 0 && <li className="muted">No matches.</li>}
          </ul>
        </section>
      </div>
    </div>
  );
}
