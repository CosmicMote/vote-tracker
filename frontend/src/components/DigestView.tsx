import { useState } from "react";
import type { Bill, Digest, Item, MemberSection } from "../types";

function BillSummary({ bill, labeled }: { bill: Bill; labeled: boolean }) {
  const label = labeled && (
    <h5 className="bill-label">
      {bill.number}
      {bill.plain_title !== bill.number && <span className="muted">: {bill.plain_title}</span>}
    </h5>
  );
  if (bill.see_above) {
    return (
      <p className="muted small">
        {bill.number}: see the <a href={`#${bill.see_above}`} onClick={scrollTo(bill.see_above)}>summary above</a>.
      </p>
    );
  }
  return (
    <div className="bill" id={bill.anchor ?? undefined}>
      {label}
      {bill.bullets.length > 0 ? (
        <ul className="bullets">
          {bill.bullets.map((b, i) => (
            <li key={i}>{b}</li>
          ))}
        </ul>
      ) : bill.excerpt ? (
        <p>{bill.excerpt}</p>
      ) : bill.official_title && bill.official_title !== bill.plain_title ? (
        <p>{bill.official_title}</p>
      ) : null}
      {bill.caveat && <p className="caveat">Note: {bill.caveat}</p>}
      <p className="provenance">
        {bill.provenance}
        {bill.url && (
          <>
            {" · "}
            <a href={bill.url} target="_blank" rel="noreferrer">
              {bill.number} on Congress.gov
            </a>
          </>
        )}
      </p>
    </div>
  );
}

// In-page anchors must not change the hash, which holds the route.
function scrollTo(id: string) {
  return (e: React.MouseEvent) => {
    e.preventDefault();
    document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
  };
}

function ItemCard({ item }: { item: Item }) {
  return (
    <article className="item">
      <h4>
        {item.heading}
        {item.number && <span className="number"> ({item.number})</span>}
      </h4>
      {item.bills.map((b) => (
        <BillSummary key={b.bill_id} bill={b} labeled={item.bills.length > 1} />
      ))}
      {item.votes.map((v, i) => (
        <div className="vote" key={i}>
          {v.context.map((line, j) => (
            <p key={j} className={j === 0 ? "vote-outcome" : "vote-member"}>
              {line}
            </p>
          ))}
          <p className="small muted">
            {v.date} ·{" "}
            {v.roll_url ? (
              <a href={v.roll_url} target="_blank" rel="noreferrer">
                {v.roll_label}
              </a>
            ) : (
              v.roll_label
            )}
          </p>
        </div>
      ))}
      {item.positions && <p className="positions">{item.positions.join(" · ")}</p>}
    </article>
  );
}

function Section({ title, items, empty }: { title: string; items: Item[]; empty: string }) {
  return (
    <section className="subsection">
      <h3>{title}</h3>
      {items.length ? items.map((it, i) => <ItemCard key={i} item={it} />) : <p className="muted">{empty}</p>}
    </section>
  );
}

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

interface MemberCardProps {
  m: MemberSection;
  expanded: boolean;
  onToggle: () => void;
}

function MemberCard({ m, expanded, onToggle }: MemberCardProps) {
  const s = m.stats;
  const bodyId = `${m.anchor}-body`;
  return (
    <section className={expanded ? "member card" : "member card collapsed"} id={m.anchor}>
      <header className="member-head">
        <h2>
          <button className="member-toggle" onClick={onToggle} aria-expanded={expanded} aria-controls={bodyId}>
            <span className="chevron" aria-hidden>
              ›
            </span>
            {m.title} {m.name} <span className="muted">({m.label})</span>
          </button>
        </h2>
        <div className="stats">
          <span className="stat">
            <strong>{s.cast}</strong> cast{s.present ? ` (${s.present} Present)` : ""}
          </span>
          <span className="stat">
            <strong>{s.missed}</strong> missed
          </span>
          <span className="stat">
            {s.contested ? (
              <>
                agreed with caucus on <strong>{s.agreed}</strong> of {plural(s.contested, "contested vote")}
              </>
            ) : (
              "no contested votes"
            )}
          </span>
        </div>
      </header>
      <div id={bodyId} hidden={!expanded}>
        <Section title="Breaks from party" items={m.breaks} empty="No breaks from party this week." />
        <Section title="Notable votes" items={m.notable} empty="No notable votes this week." />
        <section className="subsection">
          <h3>Missed votes</h3>
          {m.is_speaker ? (
            <p className="muted">Not shown: by custom the Speaker of the House rarely votes.</p>
          ) : m.missed_count ? (
            <>
              <p>
                Missed {plural(m.missed_count, "vote")} this week{m.missed_listed.length ? ", including:" : "."}
              </p>
              {m.missed_listed.map((it, i) => (
                <ItemCard key={i} item={it} />
              ))}
            </>
          ) : (
            <p className="muted">No missed votes this week.</p>
          )}
        </section>
        <section className="subsection">
          <h3>Introduced and cosponsored</h3>
          {m.sponsorships.length ? (
            <table className="sponsorships">
              <tbody>
                {m.sponsorships.map((sp, i) => (
                  <tr key={i}>
                    <td className="muted nowrap">{sp.date}</td>
                    <td className="muted nowrap">{sp.role}</td>
                    <td>
                      {sp.url ? (
                        <a href={sp.url} target="_blank" rel="noreferrer">
                          {sp.number}
                        </a>
                      ) : (
                        sp.number
                      )}{" "}
                      {sp.plain_title}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <p className="muted">No bills introduced or cosponsored this week.</p>
          )}
        </section>
      </div>
    </section>
  );
}

// Remount (key) per week, so every member starts expanded.
export default function DigestView({ digest }: { digest: Digest }) {
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());

  const toggle = (anchor: string) =>
    setCollapsed((c) => {
      const next = new Set(c);
      if (next.has(anchor)) next.delete(anchor);
      else next.add(anchor);
      return next;
    });

  // A pill shows only that member: expand it, collapse the rest, then scroll once the layout settles.
  const focus = (anchor: string) => (e: React.MouseEvent) => {
    e.preventDefault();
    setCollapsed(new Set(digest.members.map((m) => m.anchor).filter((a) => a !== anchor)));
    requestAnimationFrame(() =>
      document.getElementById(anchor)?.scrollIntoView({ behavior: "smooth", block: "start" }),
    );
  };

  const focused = collapsed.size === digest.members.length - 1 ? digest.members.find((m) => !collapsed.has(m.anchor)) : null;

  return (
    <div className="digest">
      {digest.stale.map((w) => (
        <div className="banner warn" key={w}>
          {w}
        </div>
      ))}
      <nav className="member-chips" aria-label="Members">
        {digest.members.map((m) => (
          <a
            key={m.anchor}
            href={`#${m.anchor}`}
            onClick={focus(m.anchor)}
            className={focused?.anchor === m.anchor ? "chip active" : "chip"}
            aria-current={focused?.anchor === m.anchor ? "true" : undefined}
          >
            {m.title} {m.name} <span className="muted">({m.label})</span>
          </a>
        ))}
        {collapsed.size > 0 && (
          <button className="link-button" onClick={() => setCollapsed(new Set())}>
            Expand all
          </button>
        )}
      </nav>
      {digest.splits.length > 0 && (
        <section className="card">
          <h2>Delegation splits</h2>
          <p className="muted">Senate votes where your two senators voted opposite ways.</p>
          {digest.splits.map((it, i) => (
            <ItemCard key={i} item={it} />
          ))}
        </section>
      )}
      {digest.members.map((m) => (
        <MemberCard key={m.anchor} m={m} expanded={!collapsed.has(m.anchor)} onToggle={() => toggle(m.anchor)} />
      ))}
      <footer className="digest-footer">
        <p>
          Bill summaries are written by a language model from the Congressional Research Service summary or, when none
          exists yet, the bill text (prompt {digest.prompt_version}); each is labeled with its source. Vote descriptions,
          counts and caucus comparisons come directly from the official records.
        </p>
        <p>
          Only recorded roll-call votes are covered. Voice votes and unanimous consent leave no record of individual
          positions.
        </p>
        <p>Data as of: {digest.as_of.map((a) => `${a.source}, ${a.at}`).join("; ") || "no data yet"}.</p>
      </footer>
    </div>
  );
}
