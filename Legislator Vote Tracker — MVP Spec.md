# Legislator Vote Tracker — MVP Spec

Oct 23, 2026 · @Jeff Fowler

## Overview

Build a Python tool that produces a weekly digest of what your congressional delegation did: one U.S. representative and two U.S. senators. For each member it covers notable roll-call votes with plain-language bill summaries, breaks from their party, missed votes, and bills they introduced or cosponsored.

**MVP scope**

- Tracked members are configured as a list of Bioguide IDs; the chamber of each is resolved automatically.
- The current Congress (119th), both chambers:
  - House roll-call votes, bills and members from the Congress.gov API.
  - Senate roll-call votes from the Senate's official XML feed on senate.gov, since the Congress.gov API has no Senate vote endpoint.
  - A member ID crosswalk, because Senate vote data identifies senators by LIS ID rather than Bioguide ID.
- Ingest, classification and scoring are deterministic code. An LLM is used only to summarize bills that make the digest.
- Output is one HTML digest per week with a section per member, written to disk and optionally emailed over SMTP.

**Non-goals for the MVP**

- State legislatures (a later phase).
- A web UI, multiple users, or real-time alerts.
- Voice votes and unanimous consent, which have no per-member record. In the Senate this covers many confirmations and much routine business.
- Any rating or characterization of whether a vote, bill or nominee is good or bad.

**Phases**

| Phase | Adds | Key work |
| --- | --- | --- |
| 1 (MVP) | One representative and two senators | Everything in this spec |
| 2 | Colorado state legislators | Open States API v3; seasonal, since the session runs roughly January to May |
| 3 | Polish | Topic weighting, nomination details from Congress.gov, a browsable archive of past digests |

The schema already includes the nullable columns phase 2 needs, so it should not require migrations.

## Tech stack and project layout

Python 3.12 with SQLite, packaged with uv. Keep dependencies few and boring.

| Concern | Choice |
| --- | --- |
| HTTP | httpx, with tenacity for retries |
| XML (Senate votes) | stdlib xml.etree.ElementTree |
| JSON (ID crosswalk) | the JSON edition of the crosswalk, parsed with the stdlib |
| Storage | SQLite through the stdlib sqlite3 module, plain SQL, no ORM |
| Config and validation | pydantic (settings, and validating LLM JSON output) |
| Templates | Jinja2 |
| CLI | typer |
| Tests | pytest, with respx to mock HTTP; no network access in tests |
| LLM | A small provider interface with Ollama and Anthropic implementations |

```
vote-tracker/
  pyproject.toml
  config.example.toml
  src/tracker/
    cli.py              # typer entry point
    config.py           # pydantic settings: config.toml + env vars
    db.py               # connection, schema migration, query helpers
    schema.sql
    sources/
      http.py           # shared client: user agent, throttling, retry
      congress_gov.py   # Congress.gov API: auth, paging
      senate_xml.py     # senate.gov vote menu and vote XML
      legislators.py    # congress-legislators ID crosswalk
    ingest.py
    normalize/
      house.py
      senate.py
      common.py         # bill refs, rule links, member upserts
    classify.py         # vote_class and threshold rules, pure functions
    analyze.py          # caucus positions, findings, scoring
    llm/
      base.py           # SummaryProvider protocol
      ollama.py
      anthropic.py
      prompts.py
    summarize.py
    render.py
    deliver.py
    templates/digest.html.j2
  tests/
    fixtures/
      congress_gov/     # recorded JSON, API key stripped
      senate/           # recorded XML
      legislators/      # a trimmed crosswalk file
```

**Configuration** lives in `config.toml`. Secrets come only from environment variables (`CONGRESS_GOV_API_KEY`, `ANTHROPIC_API_KEY`, `SMTP_PASSWORD`).

```toml
[members]
# Bioguide IDs; chamber and LIS ID are resolved from the crosswalk
tracked = ["X000000", "Y000000", "Z000000"]

[congress]
number = 119

[http]
user_agent = "vote-tracker/0.1 (personal use; you@example.com)"
min_interval_seconds = 1.0

[llm]
provider = "ollama"            # or "anthropic"
model = "<model name>"
base_url = "http://localhost:11434"
max_input_chars = 24000        # cap on bill text sent when no CRS summary exists
prompt_version = "v1"

[digest]
top_n = 10                     # scored votes per member, in addition to all party breaks
missed_vote_min_score = 1.0    # missed votes at or above this are listed individually
output_dir = "./digests"
timezone = "America/Denver"

[email]
enabled = false
smtp_host = "smtp.example.com"
smtp_port = 587
from_addr = "tracker@example.com"
to_addr = "me@example.com"

[scoring]                      # weights; defaults in the Scoring section
```

## Data sources

Three sources: the Congress.gov API for the House, bills and members; senate.gov XML for Senate votes; and the congress-legislators dataset to map IDs between them.

**Verify before parsing.** Field and element names in this spec are indicative. Before writing any parser, fetch one live response from each endpoint below, save it under `tests/fixtures/`, and build the parser against the real shape.

### Congress.gov API v3

[Library of Congress docs](https://github.com/LibraryOfCongress/api.congress.gov). Base URL `https://api.congress.gov/v3`; a free key from api.congress.gov, passed as the `api_key` query parameter; add `format=json` to every call. The House vote endpoints are labeled beta.

| Endpoint | Used for |
| --- | --- |
| `GET /member/{bioguideId}` | Each tracked member's name, chamber, state, district, party history |
| `GET /member/{bioguideId}/sponsored-legislation` | Bills the member introduced (both chambers) |
| `GET /member/{bioguideId}/cosponsored-legislation` | Bills the member cosponsored (both chambers) |
| `GET /house-vote/{congress}/{session}` | List of House roll-call votes |
| `GET /house-vote/{congress}/{session}/{voteNumber}` | Vote detail: question, result, totals, linked bill or amendment |
| `GET /house-vote/{congress}/{session}/{voteNumber}/members` | Every representative's position on that vote |
| `GET /bill/{congress}/{type}/{number}` | Bill title, introduced date, policy area |
| `GET /bill/{congress}/{type}/{number}/summaries` | CRS summaries, one per version |
| `GET /bill/{congress}/{type}/{number}/text` | Text versions, used only when no CRS summary exists |
| `GET /amendment/{congress}/{type}/{number}` | Amendment details and the bill it amends |

### Senate roll-call votes (senate.gov)

The Senate publishes one XML index per session and one XML file per roll call. As far as I can tell the Congress.gov API still exposes no Senate votes; check its docs once, and if an endpoint has appeared, prefer it and keep this client as a fallback.

| URL pattern | Used for |
| --- | --- |
| `https://www.senate.gov/legislative/LIS/roll_call_lists/vote_menu_{congress}_{session}.xml` | Index of the session's votes: number, date, question, result |
| `https://www.senate.gov/legislative/LIS/roll_call_votes/vote{congress}{session}/vote_{congress}_{session}_{NNNNN}.xml` | One vote; `NNNNN` is the roll number zero-padded to 5 digits |

Elements to expect in a vote file (confirm against fixtures): the question and vote title, the result, `majority_requirement` ("1/2", "3/5", "2/3"), a `document` block (type, number, title) or a nomination reference, an `amendment` block, counts, an optional `tie_breaker` (the Vice President's vote), and one `member` element per senator with `lis_member_id`, party, state and `vote_cast`.

`vote_cast` values include "Yea", "Nay", "Present" and "Not Voting", and "Guilty" or "Not Guilty" in impeachment trials. Map the last two to yea and nay and keep the original string.

### Member ID crosswalk

[unitedstates/congress-legislators](https://github.com/unitedstates/congress-legislators) publishes current members with their Bioguide, LIS, GovTrack and other IDs. Use the JSON edition of `legislators-current` and refresh it weekly. It is used to:

- resolve each tracked Bioguide ID to its chamber and, for senators, its LIS ID;
- map every senator in a vote file to a Bioguide ID, so Senate and House data share one `legislator` table;
- read the `caucus` field on a current term, so independents who caucus with a party are grouped with it (see Scoring).

### HTTP client requirements (all sources)

- Send a descriptive `User-Agent` with a contact address, from config.
- Throttle per host to `min_interval_seconds` (default 1 request per second). This also keeps the Congress.gov key well under its hourly limit, which as far as I know is about 5,000 requests.
- Retry 429 and 5xx with exponential backoff, honoring `Retry-After`. Do not retry other 4xx.
- senate.gov sits behind bot protection that has been known to block scripted clients. Test access from the machine that will run the job early; if blocked, record it as an open question rather than working around the protection.
- Never log the Congress.gov key; strip it from any URL stored in `raw_record` or written to fixtures.

### Later phase

State: [Open States API v3](https://docs.openstates.org/api-v3/) for Colorado bills, votes and legislators. Get session identifiers from `/jurisdictions` rather than guessing them.

## Database schema

One SQLite file. Every raw response is kept in `raw_record`, so a parser fix can be applied by re-running normalize without re-fetching. All other tables are derived from it and can be rebuilt. House and Senate data share the same tables, distinguished by `chamber`.

```sql
-- Raw payloads exactly as received (API key stripped from URLs)
CREATE TABLE raw_record (
  source       TEXT NOT NULL,        -- 'congress_gov' | 'senate_xml' | 'legislators' (later: 'openstates')
  kind         TEXT NOT NULL,        -- 'house_vote', 'house_vote_members', 'senate_vote_menu', 'senate_vote',
                                     -- 'bill', 'bill_summaries', 'member', 'legislators_current', ...
  external_id  TEXT NOT NULL,        -- e.g. 'house/119/1/245', 'senate/119/1/312', 'bill/119/hr/1234'
  url          TEXT,
  fetched_at   TEXT NOT NULL,        -- ISO 8601 UTC
  payload      TEXT NOT NULL,        -- JSON or XML as received
  payload_hash TEXT NOT NULL,        -- skip re-normalizing unchanged payloads
  PRIMARY KEY (source, kind, external_id)
);

CREATE TABLE legislator (
  id            INTEGER PRIMARY KEY,
  bioguide_id   TEXT UNIQUE,
  lis_id        TEXT UNIQUE,         -- senators; from the crosswalk
  openstates_id TEXT UNIQUE,         -- phase 2
  name          TEXT NOT NULL,
  level         TEXT NOT NULL,       -- 'federal' | 'state'
  chamber       TEXT NOT NULL,       -- 'house' | 'senate'
  state         TEXT,
  district      TEXT,                -- NULL for senators
  tracked       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE party_membership (
  legislator_id INTEGER NOT NULL REFERENCES legislator(id),
  party         TEXT NOT NULL,       -- 'R', 'D', 'I', ...
  caucus        TEXT NOT NULL,       -- party they caucus with; equals party unless independent
  start_date    TEXT,
  end_date      TEXT
);

CREATE TABLE bill (
  id           INTEGER PRIMARY KEY,
  jurisdiction TEXT NOT NULL,        -- 'us' (later: 'co')
  session      TEXT NOT NULL,        -- '119'
  bill_type    TEXT NOT NULL,        -- 'hr', 's', 'hres', 'sres', 'hjres', 'sjres', 'hconres', 'sconres'
  number       TEXT NOT NULL,
  title        TEXT,
  short_title  TEXT,
  introduced   TEXT,
  policy_area  TEXT,
  url          TEXT,
  updated_at   TEXT,
  UNIQUE (jurisdiction, session, bill_type, number)
);

-- House rule resolutions (H.Res.) linked to the bills they bring to the floor
CREATE TABLE rule_link (
  rule_bill_id       INTEGER NOT NULL REFERENCES bill(id),
  underlying_bill_id INTEGER NOT NULL REFERENCES bill(id),
  PRIMARY KEY (rule_bill_id, underlying_bill_id)
);

CREATE TABLE bill_summary (
  bill_id      INTEGER NOT NULL REFERENCES bill(id),
  source       TEXT NOT NULL,        -- 'crs'
  version_code TEXT NOT NULL,
  action_date  TEXT,
  text         TEXT NOT NULL,        -- HTML stripped to plain text
  PRIMARY KEY (bill_id, source, version_code)
);

CREATE TABLE vote_event (
  id             INTEGER PRIMARY KEY,
  jurisdiction   TEXT NOT NULL,
  chamber        TEXT NOT NULL,      -- 'house' | 'senate'
  session        TEXT NOT NULL,      -- '119-1', '119-2'
  roll_number    INTEGER NOT NULL,   -- numbering restarts each session, per chamber
  vote_date      TEXT NOT NULL,
  question       TEXT NOT NULL,      -- as published, e.g. 'On the Cloture Motion'
  vote_title     TEXT,               -- Senate vote title or document title, as published
  vote_class     TEXT NOT NULL,      -- see Scoring
  threshold      REAL NOT NULL,      -- share of yea+nay needed: 0.5, 0.6, 0.6667
  result         TEXT,
  bill_id        INTEGER REFERENCES bill(id),
  amendment_ref  TEXT,
  nomination_ref TEXT,               -- e.g. 'PN123-119'; nominations are not bills
  tie_breaker    TEXT,               -- Vice President's vote, if any
  yea            INTEGER, nay INTEGER, present INTEGER, not_voting INTEGER,
  source_url     TEXT,
  UNIQUE (jurisdiction, chamber, session, roll_number)
);

-- All members' positions, not just tracked members': needed for caucus positions
CREATE TABLE member_vote (
  vote_event_id  INTEGER NOT NULL REFERENCES vote_event(id),
  legislator_id  INTEGER NOT NULL REFERENCES legislator(id),
  position       TEXT NOT NULL,      -- 'yea' | 'nay' | 'present' | 'not_voting'
  position_raw   TEXT NOT NULL,      -- as published, e.g. 'Guilty'
  party_at_vote  TEXT NOT NULL,
  caucus_at_vote TEXT NOT NULL,
  PRIMARY KEY (vote_event_id, legislator_id)
);

CREATE TABLE sponsorship (
  bill_id       INTEGER NOT NULL REFERENCES bill(id),
  legislator_id INTEGER NOT NULL REFERENCES legislator(id),
  role          TEXT NOT NULL,       -- 'sponsor' | 'cosponsor'
  date          TEXT,
  withdrawn     TEXT,
  PRIMARY KEY (bill_id, legislator_id, role)
);

-- Derived per vote and caucus; recomputed by analyze
CREATE TABLE caucus_position (
  vote_event_id INTEGER NOT NULL REFERENCES vote_event(id),
  caucus        TEXT NOT NULL,
  yea           INTEGER NOT NULL,
  nay           INTEGER NOT NULL,
  majority      TEXT,                -- 'yea' | 'nay' | NULL on a tie
  cohesion      REAL,                -- max(yea, nay) / (yea + nay)
  PRIMARY KEY (vote_event_id, caucus)
);

CREATE TABLE finding (
  id            INTEGER PRIMARY KEY,
  legislator_id INTEGER NOT NULL REFERENCES legislator(id),
  period_start  TEXT NOT NULL,
  period_end    TEXT NOT NULL,
  kind          TEXT NOT NULL,       -- 'vote' | 'party_break' | 'missed_vote' | 'sponsored' | 'cosponsored'
  vote_event_id INTEGER REFERENCES vote_event(id),
  bill_id       INTEGER REFERENCES bill(id),
  score         REAL,
  selected      INTEGER NOT NULL DEFAULT 0,  -- 1 = appears in the digest
  detail_json   TEXT                 -- e.g. {"member":"yea","caucus_majority":"nay","caucus_split":"44-3"}
);

-- LLM output cache; a new CRS version or prompt version triggers a new summary
CREATE TABLE llm_summary (
  bill_id        INTEGER NOT NULL REFERENCES bill(id),
  basis          TEXT NOT NULL,      -- 'crs' | 'bill_text' | 'title_only'
  basis_version  TEXT NOT NULL,      -- CRS version_code, text version date, or 'title'
  prompt_version TEXT NOT NULL,
  model          TEXT NOT NULL,
  output_json    TEXT NOT NULL,
  created_at     TEXT NOT NULL,
  PRIMARY KEY (bill_id, basis, basis_version, prompt_version)
);

CREATE TABLE sync_state (
  source   TEXT PRIMARY KEY,         -- e.g. 'house_votes:119-1', 'senate_votes:119-1', 'legislators'
  cursor   TEXT,
  last_run TEXT
);

CREATE TABLE run_log (
  id          INTEGER PRIMARY KEY,
  command     TEXT NOT NULL,
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  status      TEXT,                  -- 'ok' | 'partial' | 'failed'
  stats_json  TEXT                   -- counts fetched, skipped, errors, per source
);
```

Store all timestamps as ISO 8601 strings in UTC. Senate vote dates are published in Eastern time; convert on normalize. Convert to the configured time zone only when rendering.

## Pipeline

Six stages, each a CLI command and each safe to re-run. Stages 1 to 3 are deterministic; only stage 4 calls an LLM. House and Senate ingest are independent: a failure in one never blocks the other.

1. **Ingest** — fetch new and changed records into `raw_record`.
   - *Crosswalk:* refresh the congress-legislators file if older than 7 days. Resolve each tracked Bioguide ID to chamber, state, district and LIS ID; fail fast with a clear error if one cannot be resolved.
   - *Members:* fetch each tracked member and their sponsored and cosponsored legislation from Congress.gov.
   - *House votes:* list votes for each session of the configured Congress. New votes are those above the highest roll number in `sync_state`. For each new vote fetch its detail and member positions.
   - *Senate votes:* fetch the session's vote menu XML, then each vote file above the highest roll number in `sync_state`.
   - *Corrections:* in both chambers, re-fetch every vote from the last 14 days; skip normalizing any whose `payload_hash` is unchanged.
   - *Bills:* for every bill referenced by a vote of either chamber, by an amendment vote's target, or by a sponsorship, fetch the bill and its summaries from Congress.gov. Fetch bill text lazily, in stage 4, only when needed.
   - A failure on one record is logged and skipped, never fatal. The run finishes as `partial`.
2. **Normalize** — parse raw records into the relational tables.
   - Upsert every member who appears in a vote as a `legislator` with `tracked = 0`. For senators, map `lis_member_id` to Bioguide through the crosswalk; log any unmapped LIS ID and skip that member's row rather than inventing one.
   - Record `party_at_vote` from the vote payload, and `caucus_at_vote` from the crosswalk's `caucus` field (falling back to party).
   - Map each position to yea, nay, present or not\_voting, keeping `position_raw`.
   - Set `vote_class` and `threshold` with `classify.py`. For the Senate, `threshold` comes from `majority_requirement`; for the House, from the vote class (see Scoring).
   - Link bills: parse the referenced document (for example "H.R. 1234", "S. 56", "S.J.Res. 7") into `bill_type` and `number`. For an amendment vote, link the bill being amended. For a nomination vote, set `nomination_ref` and keep `vote_title` as the description; nominations are not bills.
   - Detect House rule resolutions: an H.Res. whose title starts with "Providing for consideration of". Extract every referenced bill and write `rule_link` rows.
   - Strip HTML from CRS summaries before storing them.
3. **Analyze** — for a period (default: the previous Monday to Sunday in the configured time zone), per tracked member:
   - Compute `caucus_position` for every vote in the period, in both chambers.
   - Create `finding` rows for each tracked member from their own chamber's votes: one per vote, plus party breaks, missed votes, and sponsorships dated in the period.
   - Score and select per the Scoring section. Re-running analyze for a period deletes and recreates that period's findings.
   - Find *delegation splits*: Senate votes in the period where the two tracked senators voted opposite ways (yea against nay).
4. **Summarize** — for each bill attached to a selected finding, ensure an `llm_summary` exists for the current basis and prompt version. For a House rule vote, summarize the underlying bill(s), not the rule. A bill voted on in both chambers is summarized once. Nomination votes are never sent to the LLM.
5. **Render** — build one digest HTML from selected findings, summaries and deterministic vote context. Write it to `output_dir/YYYY-MM-DD.html` (the period's Monday).
6. **Deliver** — if email is enabled, send the HTML over SMTP with STARTTLS. Delivery failure must not delete the rendered file.

A `run-weekly` command runs all six in order for the most recent complete week.

## Scoring and classification

This is where the digest's value comes from. Every weight and threshold below is a default in `[scoring]` config, not a constant in code.

**Vote classification.** Match the question text case-insensitively, per chamber, first match wins. Any question that matches nothing becomes `other` and is logged as a warning, so the table can be extended. Real question strings vary; build the table from fixtures and the backfill, starting from these:

| Chamber | Question contains | vote\_class | Base weight |
| --- | --- | --- | --- |
| Both | "Overriding the Veto", "Veto Message" | `veto_override` | 1.2 |
| Senate | "Guilty or Not Guilty" | `impeachment` | 1.2 |
| House | "Election of the Speaker" | `speaker` | 1.2 |
| Both | "Conference Report", "Concur" | `final` | 1.1 |
| Both | "On Passage" | `passage` | 1.0 |
| Senate | "On the Joint Resolution", "On the Resolution", "On the Concurrent Resolution" | `passage` | 1.0 |
| Senate | "Resolution of Ratification" | `ratification` | 1.0 |
| House | "Agreeing to the Resolution" on an H.Res. that is not a rule | `passage` | 1.0 |
| Senate | "Cloture" | `cloture` | 0.9 |
| Senate | "On the Nomination" | `nomination` | 0.8 |
| Both | "Amendment" (agreeing to an amendment) | `amendment` | 0.6 |
| House | "Agreeing to the Resolution" on a rule; "Ordering the Previous Question" | `rule` | 0.5 |
| Senate | "Motion to Proceed" | `motion_to_proceed` | 0.5 |
| Senate | "Waive", "Point of Order", "Decision of the Chair" | `procedural` | 0.5 |
| House | "Recommit" | `recommit` | 0.4 |
| Both | "Table" | `table` | 0.4 |
| House | "Suspend the Rules" | `suspension` | 0.3 |
| Both | "Adjourn", "Journal", "Quorum", "Call of the House" | `housekeeping` | 0.0 |
| Both | anything else | `other` | 0.2 |

Order matters: "Cloture" must be checked before "Nomination" or "Passage", so that cloture on a nomination is classed as `cloture`, and "Motion to Table the Amendment" must land in `table`, not `amendment`.

**Threshold.** The share of yea plus nay votes needed to win. Senate: parse `majority_requirement` ("1/2" → 0.5, "3/5" → 0.6, "2/3" → 0.6667). House: 0.6667 for `suspension` and `veto_override`, otherwise 0.5. Cloture's three-fifths is of senators sworn, not of those voting; using it as a share of yea plus nay is a close enough approximation for scoring.

**Caucus position.** Group members by `caucus_at_vote`, so independents who caucus with Democrats are compared with Democrats. For each vote and caucus, count only yea and nay. `majority` is whichever is larger (NULL on a tie). `cohesion = max(yea, nay) / (yea + nay)`.

**Party break.** The tracked member voted yea or nay, opposite their caucus majority, and that caucus's cohesion is at least 0.80. Breaking from a split caucus is not newsworthy. Skip break detection for a caucus with fewer than 5 voting members.

**Missed vote.** The member's position is `not_voting`. `present` is a deliberate choice and is reported as such, not as missed.

**Score** for each vote finding. Closeness is measured against the vote's threshold, so a 59–41 cloture vote counts as close and a 59–41 simple-majority vote does not:

```python
total = yea + nay
yea_share = yea / total if total else 1.0
distance = abs(yea_share - threshold)
closeness = 1 + (1 - distance / max(threshold, 1 - threshold))   # 1.0 (far) to 2.0 (on the line)

score = base_weight[vote_class] * closeness
if party_break:                     score += 1.5
if member_sponsored_or_cosponsored: score += 0.5
if tie_breaker:                     score += 0.5
if bill.policy_area in topic_boost: score += topic_boost[bill.policy_area]  # default: empty
```

**Selection** for the digest, per tracked member:

- Always include every party break.
- Then the top `top_n` remaining votes by score.
- Exclude `housekeeping` votes, and exclude lopsided votes (yea share above 0.95 or below 0.05) unless they are a party break or the member sponsored the bill.
- Collapse multiple votes on the same bill or nomination into one digest item showing each vote (for example cloture, then confirmation), ranked by its highest score.
- Missed votes: report the count, and list individually those scoring at least `missed_vote_min_score`.
- All sponsorships and cosponsorships dated in the period are listed, unscored.
- Delegation splits are listed once, in their own section, regardless of score.

**Known edge cases**

- If the tracked representative is the Speaker, they usually do not vote; suppress their missed-vote section and say why.
- Caucus is taken at the time of the vote, so a member who switches parties is compared to the right group.
- A senator appointed or sworn in mid-period has no earlier votes; do not count those as missed.
- Vice President tie-breaking votes are recorded on the vote, not as a member position.

## LLM summarization

The LLM does one job: describe what a bill does in plain language. It never sees how the member voted, and it never writes the vote context; that sentence is built deterministically (see Digest format). This keeps the model's framing out of anything about the member.

The LLM summarizes bills only. Nomination votes show the Senate's own published description (for example the nominee and the position), with no model involved.

**Provider interface**

```python
class SummaryProvider(Protocol):
    name: str    # e.g. 'ollama:<model>'
    def complete_json(self, system: str, user: str, schema: dict) -> str: ...
```

Implement `OllamaProvider` (local HTTP API, JSON format mode) and `AnthropicProvider` (Messages API). Use temperature 0. Select the provider from config.

**Choosing the basis**, in order:

1. The latest CRS summary (`basis = 'crs'`, `basis_version` = its version code).
2. If none exists: the latest text version, HTML stripped, truncated to `max_input_chars` at a section boundary where possible (`basis = 'bill_text'`).
3. If no text is available either: the title only (`basis = 'title_only'`). Do not call the LLM; render the title and a "No summary or text published yet" note.

**System prompt**

```
You summarize U.S. legislation for a general reader.
Describe what the bill would do, using only the material provided.
Do not say whether the bill is good or bad, who supports or opposes it,
or what its effects will be beyond what the text states.
Do not use the bill's own promotional language; describe its mechanics.
If the material is truncated or unclear, say so in the caveat field.
Return only JSON matching the schema.
```

**User message** contains the bill number, official title, short title if any, policy area, the basis label, and the basis text.

**Output schema** (validate with pydantic):

```json
{
  "plain_title": "string, at most 12 words, neutral",
  "what_it_does": ["2 to 4 bullets, each at most 30 words"],
  "caveat": "string or null"
}
```

**Failure handling.** If the output fails to parse or validate, retry once with the validation error appended to the user message. If it fails again, store nothing, log the error, and render the CRS summary's first two sentences (or the title) instead.

**Caching.** Look up `llm_summary` by `(bill_id, basis, basis_version, prompt_version)` before calling. Bumping `prompt_version` in config regenerates summaries lazily as bills reappear.

## Digest format

One self-contained HTML file per week covering the whole delegation, with inline CSS only so it renders the same in a browser and an email client. Subject line: `Your delegation — week of <Mon date>: <n> notable votes, <m> breaks from party`.

**Layout, in order**

1. **Delegation splits**: Senate votes where your two senators voted opposite ways. If none, omit the section.
2. **One section per tracked member**, representative first, then senators alphabetically. Each contains:
   1. *Header stats*: votes cast, votes missed, and caucus agreement on contested votes (votes where the member's caucus cohesion was below 0.95), shown as "agreed with caucus on 41 of 43 contested votes".
   2. *Breaks from party*: every party break, even when low scoring. If none, say so in one line.
   3. *Notable votes*: selected votes, highest score first.
   4. *Missed votes*: the count, plus individually listed high-scoring misses.
   5. *Introduced and cosponsored*: bill number, plain title, date, link.
3. **Footer**: summary provenance, the note that voice votes and unanimous consent are not covered, and the data's as-of time per source.

If a bill was voted on in both chambers during the week, show its summary in the first place it appears and refer back to it later, rather than repeating the bullets.

**Each vote item** shows the plain title and bill number (or the nomination description), the LLM bullets for bills, the vote context, and links to the bill on Congress.gov and the roll call on clerk.house.gov or senate.gov. The vote context is a template, never LLM output:

```
{class_phrase} on {subject}{threshold_note}. {result} {yea}–{nay}{tie_breaker_note}.
{title} {name} voted {position}, {with_or_against} {caucus_count} of {caucus_total} {caucus_name} members.
```

- `threshold_note` appears only when the threshold is not a simple majority: " (60 votes needed)" for cloture, " (two-thirds needed)" for suspensions and veto overrides.
- `tie_breaker_note`: ", with the Vice President breaking the tie".
- `caucus_name` is "Republican" or "Democratic caucus" (the latter includes independents who caucus with Democrats).
- `class_phrase` comes from a fixed map, for example `rule` → "Procedural vote to begin debate", `suspension` → "Fast-track passage vote", `recommit` → "Motion to send the bill back to committee", `cloture` → "Vote to end debate", `motion_to_proceed` → "Vote to begin consideration", `nomination` → "Confirmation vote", `passage` → "Final passage vote".

**Provenance labels** under each bill summary: "Summary based on the Congressional Research Service summary", "AI summary of the bill text; no official summary yet", or "No summary or text published yet".

Illustrative example of one Senate item (invented bill and numbers):

```markdown
### Changes how federal grazing permits are renewed (S. 1234)
- Extends permit terms on federal grazing land from 10 to 20 years.
- Requires the agency to process renewals within 180 days.
- Exempts unchanged renewals from a new environmental review.

Vote to end debate on S. 1234 (60 votes needed). Failed 57–41.
Sen. Example voted Yea, with 45 of 47 Democratic caucus members.
Summary based on the Congressional Research Service summary · Bill · Roll call 312
```

## CLI, operations and testing

**Commands** (typer, entry point `tracker`):

| Command | Does |
| --- | --- |
| `tracker init-db` | Create the database from `schema.sql`; safe to re-run |
| `tracker ingest [--chamber house\|senate] [--since YYYY-MM-DD]` | Stage 1; both chambers by default; `--since` backfills from a date |
| `tracker normalize [--all]` | Stage 2; `--all` re-parses every raw record |
| `tracker analyze [--week YYYY-MM-DD]` | Stage 3 for the week containing the date; default last full week |
| `tracker summarize [--week …]` | Stage 4 for that week's selected findings |
| `tracker digest [--week …] [--send]` | Stages 5 and 6 |
| `tracker run-weekly` | All stages for the last full week, then send |
| `tracker members` | Show tracked members as resolved: chamber, state, Bioguide and LIS IDs |
| `tracker explain-vote <chamber> <session> <roll>` | Print classification, threshold, caucus positions and score for one vote; for tuning |

**Scheduling.** Run ingest and normalize nightly, and the digest weekly once both chambers' week is done:

```
15 2 * * *   cd /path/to/vote-tracker && uv run tracker ingest && uv run tracker normalize
35 7 * * 6   cd /path/to/vote-tracker && uv run tracker run-weekly
```

**Logging and errors.** Log to stdout and a rotating file. Every command writes a `run_log` row with counts per source. Exit non-zero only when a whole stage fails, not when individual records or one chamber's source do. If one chamber's ingest failed, the digest still renders and says which chamber's data is stale.

**Testing**

- Record real responses once per endpoint into `tests/fixtures/` (API key removed): Congress.gov JSON, a Senate vote menu, and Senate vote files covering a bill passage, a cloture vote, a nomination, an amendment, and a vote with a tie breaker if one exists. Parsers are tested only against these.
- `test_crosswalk.py`: Bioguide to LIS resolution, an independent who caucuses with Democrats, and an unmapped LIS ID (logged and skipped).
- `test_classify.py`: a table of real question strings from both chambers and their expected `vote_class` and `threshold`, including a rule H.Res., a non-rule H.Res., cloture on a nomination, and a motion to table an amendment.
- `test_analyze.py`: synthetic votes covering a party break, a break from a split caucus (not flagged), a tie, `present` versus `not_voting`, a lopsided vote that is excluded, a 59–41 cloture vote scoring as close, and a delegation split.
- `test_rule_link.py`: rule titles referencing one bill, several bills, and an S. or H.J.Res. bill.
- `test_summarize.py`: a fake provider returning valid JSON, invalid then valid JSON, and invalid twice (fallback path); confirm caching avoids a second call, a bill voted on in both chambers is summarized once, and nominations never reach the provider.
- `test_render.py`: a snapshot test of the digest HTML for a fixed fixture database with one representative and two senators.

**Acceptance criteria for the MVP**

- [ ] `tracker members` resolves all three configured Bioguide IDs to the right chamber, and both senators to LIS IDs.
- [ ] `tracker ingest --since 2025-01-03` backfills the 119th Congress for both chambers without errors or duplicate rows, and a second run fetches only recent changes.
- [ ] Every vote question seen so far in either chamber classifies to something other than `other`, or is logged.
- [ ] Totals and caucus splits for five hand-checked votes per chamber match clerk.house.gov and senate.gov.
- [ ] A weekly digest renders for a recent week with a section per member, lists every party break and any delegation split, and shows correct provenance labels.
- [ ] Re-running any stage for the same week produces identical output.
- [ ] Switching `llm.provider` between Ollama and Anthropic needs only a config change.
- [ ] The full test suite runs offline.

**Build order**

1. Project skeleton, config, `init-db`, the shared HTTP client, and recorded fixtures from all three sources. Confirm senate.gov is reachable from the machine that will run the job.
2. Crosswalk loading and `tracker members`.
3. House ingest and normalize; backfill the 119th Congress.
4. Senate ingest and normalize; backfill the 119th Congress.
5. Classification, thresholds, caucus positions, findings and scoring, with `explain-vote`. Check the plain-text output for each member against a few weeks you remember before going further.
6. LLM summarization with caching and fallback.
7. HTML digest and email delivery.
8. `run-weekly` and cron.

**Open questions**

- Does the House member-votes response include each member's party, or does it need a lookup? (Settle with the first fixture.)
- Has Congress.gov added Senate votes since this was written? If so, use it and keep the XML client as a fallback.
- Which local model gives acceptable summaries? Compare two or three on the same 10 bills before choosing.
