import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api";
import type { Legislator, Location, MembersResponse } from "../types";

const describe = (p: Legislator) =>
  `${p.chamber === "senate" ? "Sen." : "Rep."} ${p.name} (${p.party}-${p.state}${p.district ? `-${p.district}` : ""})`;

// Browsers offer geolocation only to secure pages: https, or http on localhost.
const canLocate = window.isSecureContext && "geolocation" in navigator;

const SUFFIX: Record<number, string> = { 1: "st", 2: "nd", 3: "rd" };
const ordinal = (n: number) => `${n}${n % 100 >= 11 && n % 100 <= 13 ? "th" : (SUFFIX[n % 10] ?? "th")}`;

const place = (loc: Location) =>
  loc.district === null
    ? loc.state_name
    : `${loc.state_name}'s ${loc.district === "0" ? "at-large" : ordinal(Number(loc.district))} district`;

function currentPosition(): Promise<GeolocationPosition> {
  return new Promise((resolve, reject) =>
    navigator.geolocation.getCurrentPosition(resolve, reject, { timeout: 20_000, maximumAge: 10 * 60_000 }),
  );
}

export default function MembersPage() {
  const [all, setAll] = useState<Legislator[] | null>(null);
  const [saved, setSaved] = useState<MembersResponse | null>(null);
  const [draft, setDraft] = useState<string[]>([]);
  const [state, setState] = useState("");
  const [query, setQuery] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [locating, setLocating] = useState(false);
  const autoLocated = useRef(false);

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

  // Find the state and district, then select its senators and representative if nothing is selected yet.
  const locate = async () => {
    setLocating(true);
    setError(null);
    setNotice(null);
    try {
      let pos: GeolocationPosition;
      try {
        pos = await currentPosition();
      } catch (e) {
        const err = e as GeolocationPositionError;
        throw new Error(
          err.code === err.PERMISSION_DENIED
            ? "Location access is blocked for this page. Pick your state below, or allow location in the site settings."
            : `Couldn't get your location (${err.message}). Pick your state below.`,
        );
      }
      const loc = await api.locate(pos.coords.latitude, pos.coords.longitude);
      setState(loc.state);
      setQuery("");
      const here = (all ?? []).filter((p) => p.state === loc.state);
      // Senators first: delegation splits compare the first two senators in the list.
      const senators = here.filter((p) => p.chamber === "senate");
      const reps = here.filter((p) => p.chamber === "house" && loc.district !== null && p.district === loc.district);
      if (draft.length > 0) {
        setNotice(`You're in ${place(loc)}. Your selection is unchanged; tick members below to add them.`);
      } else if (senators.length + reps.length > 0) {
        setDraft([...senators, ...reps].map((p) => p.bioguide));
        const who = [
          senators.length && (senators.length === 1 ? "senator" : "two senators"),
          reps.length && "representative",
        ];
        const rough = pos.coords.accuracy > 1000 && reps.length > 0;
        setNotice(
          `You're in ${place(loc)}. Selected your ${who.filter(Boolean).join(" and ")}; check them and Save.` +
            (rough ? " Your location is approximate, so near a district line the representative may be wrong." : ""),
        );
      } else {
        setNotice(`You're in ${place(loc)}, but no current members were found for it. Pick from the list below.`);
      }
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLocating(false);
    }
  };

  // On a first visit, locate straight away if the browser already allows it; otherwise wait for the button,
  // so the permission prompt only appears when asked for.
  useEffect(() => {
    if (!all || !saved || saved.tracked.length > 0 || !canLocate || autoLocated.current) return;
    autoLocated.current = true;
    navigator.permissions?.query({ name: "geolocation" }).then(
      (p) => {
        if (p.state === "granted") locate();
      },
      () => {},
    );
  }, [all, saved]);

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
      {saved.tracked.length === 0 && !notice && (
        <div className="banner info">
          <span>
            Choose who your digest covers: usually your two senators and your representative.{" "}
            {canLocate
              ? "Use my location finds them from your congressional district."
              : "Pick your state below, or search by name."}
          </span>
        </div>
      )}
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
          {canLocate && (
            <div className="locate">
              <button className="btn small" onClick={locate} disabled={locating}>
                {locating ? "Locating…" : "Use my location"}
              </button>
              <span className="muted small">
                Your location goes to the US Census Bureau&rsquo;s geocoder to find your district. It isn&rsquo;t
                stored.
              </span>
            </div>
          )}
          <div className="filters">
            <label>
              State{" "}
              <select className="select" value={state} onChange={(e) => (setState(e.target.value), setQuery(""))}>
                {state === "" && (
                  <option value="" disabled>
                    Choose…
                  </option>
                )}
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
            {candidates.length === 0 && (
              <li className="muted">{state || query.trim() ? "No matches." : "Pick a state or search by name."}</li>
            )}
          </ul>
        </section>
      </div>
    </div>
  );
}
