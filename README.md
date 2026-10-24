# Legislator Vote Tracker

A Python tool that writes a weekly digest of what your congressional delegation (one U.S. representative and two U.S. senators) did in Congress. For each member it covers:

- **Breaks from party:** every vote cast against a united caucus.
- **Notable votes:** the highest-scoring votes of the week, with a plain-language summary of each bill.
- **Missed votes:** a count, with high-stakes misses listed individually.
- **Bills introduced and cosponsored** during the week.

The digest opens with **delegation splits**: Senate votes where your two senators voted opposite ways.

Ingest, classification and scoring are deterministic code. A language model is used for one job only: describing what a bill does. It never sees how anyone voted. Every sentence about a member's vote is built from the official record by a fixed template.

The digest is one self-contained HTML file per week, written to disk and optionally emailed. A Senate item looks like this (illustrative; the bullets come from the summarizer):

> ### Makes continuing appropriations for fiscal year 2026 (H.R. 5371)
> - *(2–4 bullets from the bill summary)*
>
> Summary based on the Congressional Research Service summary · H.R. 5371 on Congress.gov
>
> Vote to end debate on H.R. 5371 (60 votes needed). Passed 60–40.
> Sen. Michael F. Bennet voted Nay, with 39 of 47 Democratic caucus members.
> Nov 10, 2025 · Roll call 617

This is the MVP (phase 1) of the spec in [Legislator Vote Tracker — MVP Spec.md](Legislator%20Vote%20Tracker%20—%20MVP%20Spec.md). It covers the 119th Congress, both chambers.

---

## Read this first: known blockers

1. **senate.gov blocks scripted requests from this machine.** Every request to `www.senate.gov` from Python or curl returned `403 Forbidden` from Akamai's bot protection, even with a descriptive User-Agent. As the spec requires, the client does **not** try to get around this. When the block applies, Senate ingest fails cleanly: House data still updates, and the digest shows a "Senate data may be stale" banner. Test from the machine that will run the job. If it is blocked too, see [Open questions](#open-questions).
2. **You need a Congress.gov API key.** Get a free key at <https://api.congress.gov/sign-up/> and export it as `CONGRESS_GOV_API_KEY`. The shared `DEMO_KEY` allows about 10 requests an hour, which is enough to record a few fixtures and nothing more. A backfill makes a few thousand requests: at the default 1 request per second it takes roughly an hour, well under the key's limit of about 5,000 requests an hour.

---

## Quick start

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync                                  # install dependencies into .venv
cp config.example.toml config.toml       # then edit [members] and [http].user_agent
export CONGRESS_GOV_API_KEY=...          # required
export ANTHROPIC_API_KEY=...             # only if llm.provider = "anthropic"
export SMTP_PASSWORD=...                 # only if email.enabled = true

uv run tracker init-db
uv run tracker members                   # check that all three IDs resolve
uv run tracker ingest --since 2025-01-03 # backfill the 119th Congress
uv run tracker normalize
uv run tracker run-weekly                # analyze, summarize and render last week, then send
```

The digest is written to `digests/YYYY-MM-DD.html`, named for the week's Monday.

`config.example.toml` tracks Colorado's delegation: Sen. Michael Bennet (`B001267`), Sen. John Hickenlooper (`H000273`), and Rep. Diana DeGette (`D000197`, CO-1) as a stand-in representative. Replace her with your own representative's Bioguide ID, which you can find at <https://bioguide.congress.gov> or in the URL of their Congress.gov member page.

For summaries, either run [Ollama](https://ollama.com) locally (`ollama pull llama3.1:8b`, the default) or set `llm.provider = "anthropic"` and `llm.model = "claude-opus-5-5"`. Switching providers needs only a config change.

---

## Commands

| Command | Does |
| --- | --- |
| `tracker init-db` | Create the database from `schema.sql`; safe to re-run |
| `tracker ingest [--chamber house\|senate] [--since YYYY-MM-DD]` | Stage 1: fetch new and changed records. Both chambers by default; `--since` backfills from a date |
| `tracker normalize [--all]` | Stage 2: parse raw records into tables. `--all` re-parses every raw record, for example after a parser fix |
| `tracker analyze [--week YYYY-MM-DD]` | Stage 3 for the week containing the date. The default is the last full Monday–Sunday week |
| `tracker summarize [--week …]` | Stage 4: LLM summaries for the bills that made that week's digest |
| `tracker digest [--week …] [--send]` | Stages 5 and 6: render the HTML, then email it if `--send` is given and `[email].enabled` is true |
| `tracker run-weekly` | All stages for the last full week, then send |
| `tracker members` | Show tracked members as resolved: chamber, state, district, Bioguide and LIS IDs, party, caucus |
| `tracker explain-vote <chamber> <session> <roll>` | Print one vote's classification, threshold, caucus positions and per-member score, for tuning. Session is `119-1`, or just `1` |

Global options are `-c/--config PATH` (default `./config.toml`, or `$TRACKER_CONFIG`) and `-v/--verbose`.

Every command writes a `run_log` row with per-source counts. A command exits non-zero only when a whole stage fails, not when a single record or one chamber's source does.

**Scheduling.** Run ingest and normalize nightly, and the digest on Saturday morning, once both chambers' week is done:

```cron
15 2 * * *   cd /path/to/political-tracker && uv run tracker ingest && uv run tracker normalize
35 7 * * 6   cd /path/to/political-tracker && uv run tracker run-weekly
```

Logs go to stdout and to a rotating file at `logs/tracker.log`. The Congress.gov key is never logged: it is stripped from every stored URL and from error messages, and httpx's own request logging, which would include it, is silenced.

---

## How it works

```
 Congress.gov API ──┐                                    ┌─ LLM (Ollama | Anthropic)
 senate.gov XML ────┼─▶ 1 ingest ─▶ raw_record ─▶ 2 normalize ─▶ tables ─▶ 3 analyze ─▶ findings
 congress-legislators┘                                                       │
                                               digests/YYYY-MM-DD.html ◀─ 5 render ◀─ 4 summarize
                                                         │
                                                         └─▶ 6 deliver (SMTP + STARTTLS)
```

Each stage is a CLI command and safe to re-run. Stages 1–3 are deterministic and only stage 4 calls an LLM. House and Senate ingest are independent, so a failure in one never blocks the other.

### 1. Ingest (`ingest.py`, `sources/`)

- **Crosswalk.** Downloads [congress-legislators](https://github.com/unitedstates/congress-legislators) `legislators-current.json` when the stored copy is more than 7 days old, then resolves each tracked Bioguide ID to chamber, state, district and LIS ID. If any ID cannot be resolved, ingest stops with a clear error.
- **Members.** Fetches each tracked member, plus their sponsored and cosponsored legislation.
- **House votes.** Lists each session's votes from Congress.gov, then fetches detail and member positions for new votes, meaning those above the roll number stored in `sync_state`.
- **Senate votes.** Fetches the session's vote menu XML from senate.gov, then each new vote file.
- **Corrections.** In both chambers, re-fetches every vote from the last 14 days. A payload whose hash has not changed is not normalized again.
- **Bills.** Fetches each bill referenced by a vote, by an amendment vote's target, by a House rule's title, or by a sponsorship, along with its CRS summaries. A bill is re-fetched when it is referenced by something recent, or when the stored copy is more than a week old. Bill text is fetched lazily in stage 4, and only for bills that have no CRS summary.

A failure on one record is logged and skipped, and the run finishes as `partial`. The sync cursor never moves past a failed vote, so that vote is retried on the next run. Every response is stored as received in `raw_record`, so fixing a parser needs only `normalize --all`, not a re-fetch.

The HTTP client (`sources/http.py`) sends the configured User-Agent and limits each host to one request per `min_interval_seconds`. It retries 429 and 5xx responses with exponential backoff, honoring `Retry-After`, and does not retry other 4xx responses.

### 2. Normalize (`normalize/`)

- Every member who appears in a vote becomes a `legislator` row. Senators are mapped from LIS ID to Bioguide ID through the crosswalk. An LIS ID with no match is logged once and that member's row is skipped, never invented.
- `party_at_vote` comes from the vote record. `caucus_at_vote` comes from the crosswalk, as of the vote's date, so independents such as Sanders and King are grouped with Democrats, and a member who switches parties is compared with the right group. It falls back to party when the crosswalk has no entry.
- House "Aye"/"No" map to yea/nay. In Senate impeachment trials, "Guilty"/"Not Guilty" map to yea/nay. The original string is always kept in `position_raw`.
- In Speaker elections the House records a candidate's name instead of a position. A vote for the winner is stored as yea and a vote for anyone else as nay, so caucus positions still mean something.
- Bill references such as "H.R. 1234", "S.J.Res. 7" or "H. Con. Res. 14" are parsed. An amendment vote links to the bill it amends. A nomination vote sets `nomination_ref` (for example `PN11-7`) and keeps the Senate's published description, because nominations are not bills.
- A House rule is an H.Res. whose title starts with "Providing for consideration of". Each measure it brings to the floor is written to `rule_link`. Only parenthesized citations such as "the bill (H.R. 3838)" count, so a background reference such as "pursuant to H. Con. Res. 14" is not mistaken for an underlying bill.
- Senate vote times are published in Eastern time and are converted to UTC. All timestamps are stored as ISO 8601 UTC, and converted to the configured time zone only when rendering.
- CRS summaries are stripped of HTML.

### 3. Analyze (`analyze.py`, `classify.py`)

**Classification.** Each vote's question text is matched case-insensitively against a table, per chamber, and the first match wins. The order matters:

| Chamber | Question contains | `vote_class` | Base weight |
| --- | --- | --- | --- |
| Both | "Overriding the Veto", "Veto Message" | `veto_override` | 1.2 |
| Senate | "Guilty or Not Guilty" | `impeachment` | 1.2 |
| House | "Election of the Speaker" | `speaker` | 1.2 |
| Senate | "Cloture" | `cloture` | 0.9 |
| Both | "Table" | `table` | 0.4 |
| House | "Suspend the Rules" | `suspension` | 0.3 |
| Both | "Conference Report", "Concur" (whole word) | `final` | 1.1 |
| Both | "On Passage" | `passage` | 1.0 |
| Senate | "Resolution of Ratification" | `ratification` | 1.0 |
| Senate | "On the Joint/Concurrent Resolution", "On the Resolution" | `passage` | 1.0 |
| House | "Agreeing to the Resolution": a rule H.Res. → `rule`; any other → `passage` | | |
| Senate | "On the Nomination" | `nomination` | 0.8 |
| Both | "Amendment" | `amendment` | 0.6 |
| House | "Ordering the Previous Question" | `rule` | 0.5 |
| Senate | "Motion to Proceed" | `motion_to_proceed` | 0.5 |
| Senate | "Waive", "Point of Order", "Decision of the Chair", "Motion to Discharge/Reconsider/Instruct/Recommit" | `procedural` | 0.5 |
| House | "Recommit" | `recommit` | 0.4 |
| Both | "Adjourn", "Journal", "Quorum", "Call of the House" | `housekeeping` | 0.0 |
| Both | anything else, logged as a warning | `other` | 0.2 |

Cloture is checked first, so cloture on a nomination is `cloture`. "Table" comes before "Amendment", so a motion to table an amendment is `table`. "Suspend the Rules" comes before "Concur", so a suspension motion to concur keeps its two-thirds threshold. "Concur" is matched as a whole word, so "Concurrent Resolution" does not match it.

**Threshold.** This is the share of yea plus nay votes needed to win. For the Senate it comes from `majority_requirement`, which also gets cloture on nominations right: that needs only a simple majority since 2013. For the House it is two-thirds for suspensions and veto overrides, and a simple majority otherwise.

**Caucus position.** For each vote and caucus, only yea and nay are counted. `majority` is the larger side, or none on a tie, and `cohesion = max(yea, nay) / (yea + nay)`.

**Party break.** A tracked member voted yea or nay, against their caucus majority, when that caucus's cohesion was at least 0.80 and at least 5 of its members voted. Breaking from a split caucus is not news.

**Missed vote.** The member's position is `not_voting`. A `present` vote is a deliberate choice and is reported as cast, not as missed. A member with no record on a vote, for example a senator sworn in mid-week, is not counted as missing it.

**Score.** Closeness is measured against the vote's threshold, so a 59–41 cloture vote counts as close and a 59–41 simple-majority vote does not:

```python
yea_share = yea / (yea + nay)
closeness = 1 + (1 - abs(yea_share - threshold) / max(threshold, 1 - threshold))   # 1.0 far … 2.0 on the line
score = base_weight[vote_class] * closeness
score += 1.5 if party_break          # [scoring] party_break_bonus
score += 0.5 if member sponsored or cosponsored the bill (or a rule's underlying bill)
score += 0.5 if the Vice President broke a tie
score += topic_boost.get(bill.policy_area, 0)   # empty by default
```

**Selection** works per member:

- Every party break is included.
- Then the top `top_n` remaining items by score, where several votes on the same bill or nomination (for example cloture and then passage) collapse into one item, ranked by its highest-scoring vote.
- `housekeeping` votes are excluded, and so are lopsided votes (yea share above 0.95 or below 0.05) unless the member sponsored the bill.
- For missed votes, the digest shows a count and lists individually those scoring at least `missed_vote_min_score`. If the tracked representative is the Speaker, who by custom rarely votes, the missed-vote list is replaced with a note saying so.
- Every sponsorship and cosponsorship dated in the week is listed.

Re-running analyze for a week deletes and recreates that week's findings, with identical results.

### 4. Summarize (`summarize.py`, `llm/`)

For each bill attached to a selected finding, using the underlying bill(s) for a House rule vote, the summarizer picks a basis:

1. The latest **CRS summary**.
2. If there is none: the latest **bill text**, HTML stripped and cut to `max_input_chars` at a section boundary where possible.
3. If there is no text either: the **title only**. The LLM is not called, and the digest shows the title with the note "No summary or text published yet".

The model gets the bill number, titles, policy area, a basis label and the basis text, and nothing about votes or members. Its output must match `{"plain_title": str, "what_it_does": [2–4 str], "caveat": str|null}`, validated with pydantic, which enforces at most 12 words in the title and at most 30 in each bullet. Invalid output is retried once, with the validation error appended. If the retry also fails, nothing is stored and the digest shows the first two sentences of the CRS summary instead.

Results are cached in `llm_summary` by bill, basis, basis version and prompt version. A new CRS version, or bumping `llm.prompt_version`, regenerates summaries lazily as bills reappear. A bill voted on in both chambers is summarized once. Nominations never reach the model.

| Provider | Implementation |
| --- | --- |
| `ollama` | `POST /api/chat` with the JSON schema as `format`, temperature 0 |
| `anthropic` | The official `anthropic` SDK: Messages API with structured output (`output_config.format` as a JSON schema), `effort: "low"`, and the server-side refusal fallback (`fallbacks: "default"`) on models that support it |

The Anthropic Messages API now rejects sampling parameters such as `temperature`, so the spec's "temperature 0" applies only to Ollama. For Anthropic, repeatability comes from structured output plus the summary cache: a summary is generated once and reused.

### 5–6. Render and deliver (`render.py`, `templates/digest.html.j2`, `deliver.py`)

The digest uses inline style attributes only, with no `<style>` block, so it looks the same in a browser and in an email client. Its layout:

1. Delegation splits. This section is omitted when there are none.
2. One section per member, representative first and then senators by last name. Each section shows:
   - Header stats: votes cast, votes missed, and "agreed with caucus on X of Y contested votes", where a contested vote is one on which the member's caucus cohesion was below 0.95.
   - Breaks from party.
   - Notable votes.
   - Missed votes.
   - Bills introduced and cosponsored.
3. A footer with summary provenance, the note that voice votes and unanimous consent are not covered, and the as-of time of each source.

Each vote item has two lines of context, built from data only:

```
{class_phrase} on {subject}{threshold_note}. {result} {yea}–{nay}{tie_breaker_note}.
{Rep.|Sen.} {name} voted {position}, {with|against} {n} of {total} {Republican|Democratic caucus} members.
```

Each item also links to the bill on Congress.gov and to the roll call on clerk.house.gov or senate.gov. Every summary carries a provenance label:

- "Summary based on the Congressional Research Service summary"
- "AI summary of the bill text; no official summary yet"
- "No summary or text published yet"

If a bill shows up more than once, for example after votes in both chambers, its summary appears the first time and later items link back to it.

The subject line is `Your delegation — week of <Mon date>: <n> notable votes, <m> breaks from party`. Mail goes out over SMTP with STARTTLS. A delivery failure never removes the rendered file, so `tracker digest --send` can simply be re-run.

---

## Configuration

Everything lives in `config.toml`; see [config.example.toml](config.example.toml) for every key with comments. Secrets come only from the environment: `CONGRESS_GOV_API_KEY`, `ANTHROPIC_API_KEY` and `SMTP_PASSWORD`.

| Section | Keys |
| --- | --- |
| `[members]` | `tracked`: Bioguide IDs |
| `[congress]` | `number` (119) |
| `[http]` | `user_agent` (include a contact address), `min_interval_seconds`, `timeout_seconds`, `max_attempts` |
| `[llm]` | `provider`, `model`, `base_url`, `max_input_chars`, `prompt_version`, `effort`, `fallbacks` |
| `[digest]` | `top_n`, `missed_vote_min_score`, `output_dir`, `timezone` |
| `[email]` | `enabled`, `smtp_host`, `smtp_port`, `smtp_user`, `from_addr`, `to_addr` |
| `[storage]` | `db_path`, `log_dir`, `crosswalk_url`, `crosswalk_historical_url`, `crosswalk_max_age_days`, `corrections_window_days` |
| `[scoring]` | `party_break_bonus`, `sponsor_bonus`, `tie_breaker_bonus`, `break_min_cohesion`, `break_min_caucus_size`, `lopsided_share`, `contested_cohesion`, `[scoring.base_weights]`, `[scoring.topic_boost]` |

Every weight and threshold in the scoring section is a configurable default, not a constant in code.

---

## Testing

```bash
uv run pytest
```

The suite runs fully offline: an autouse fixture fails any test that opens a socket. HTTP is mocked with respx. Parsers are tested only against recorded responses in `tests/fixtures/`:

| Fixture | Source |
| --- | --- |
| `congress_gov/*.json` | Recorded from the live Congress.gov API, with the key removed: House vote list, vote detail and member positions (a rule's previous-question vote and an amendment vote), a rule H.Res., and CRS summaries |
| `senate/*.xml` | A trimmed vote menu, plus five vote files: bill passage with a VP tie-breaker (H.R. 1, roll 372), cloture on a bill (617), an amendment (616), cloture on a nomination (659), and a nomination with a VP tie-breaker (Hegseth, roll 15). See the note below |
| `legislators/*.json` | congress-legislators current and historical files, trimmed to the senators plus a few representatives, including the Speaker and a member who switched parties mid-term |
| `congress_gov/unverified/*` | **Not recorded.** Shape-only fixtures with invented values (bills H.R. 9001–9003) for sponsored and cosponsored legislation, a bill's cosponsor list, and bill text. The shared `DEMO_KEY` ran out before these endpoints could be recorded. See the README in that folder, and replace them with recorded responses when you have a key |

**How the Senate fixtures were recorded.** Because senate.gov blocks scripted clients, the five vote files were read in a real browser session. Their element names, element order and values (question, title, majority requirement, document, amendment, counts, tie-breaker, and every senator's LIS ID, party, state and vote) were transcribed as published, then re-serialized locally. Senator names in `member_full`, `first_name` and `last_name` were filled from the crosswalk, since the parser does not use them. The rebuilt files are within about 1% of the size of the originals. Some vote files (rolls 524, 614 and 646) kept returning 403 even in the browser, so the motion-to-table, motion-to-proceed and decision-of-the-chair cases are covered by question strings from the vote menu in `test_classify.py` instead of full vote files.

| Test file | Covers |
| --- | --- |
| `test_crosswalk.py` | Bioguide→LIS resolution, independents caucusing with Democrats, a dated party switch, Speaker detection, unmapped LIS IDs (logged and skipped), and former senators mapped through the historical file |
| `test_classify.py` | Real question strings from both chambers, including rule vs. non-rule H.Res., cloture on a nomination, and motion to table an amendment |
| `test_analyze.py` | Party break, a break from a split caucus (not flagged), a tie, present vs. not voting, an unseated member, a lopsided vote excluded unless sponsored, a 59–41 cloture vote scoring as close, collapsing, delegation splits, week boundaries, and re-run determinism |
| `test_rule_link.py` | Rule titles referencing one bill, several bills, an S. bill and an H.J.Res.; bill-reference parsing |
| `test_summarize.py` | A fake provider returning valid JSON, invalid then valid JSON, and invalid twice (fallback); caching; prompt-version bumps; the bill-text basis; a bill voted on in both chambers summarized once; nominations never sent; rules summarizing their underlying bill |
| `test_normalize.py` | Totals and caucus splits against the published party totals, UTC conversion, the amendment target, nominations, Guilty/Not Guilty mapping, and idempotency |
| `test_ingest.py` | A backfill followed by a second run that fetches only the corrections window; no duplicate rows; one chamber failing without blocking the other; fail-fast on an unknown member |
| `test_http.py` | Retry on 429 honoring Retry-After, backoff on 5xx, no retry on other 4xx, per-host throttling, paging, and key redaction |
| `test_providers.py` | Ollama and Anthropic request shapes, refusal handling, and switching providers through config alone |
| `test_sponsorships.py` | Sponsored and cosponsored lists, cosponsorship dates from the bill's cosponsor list (fetched once per bill), and fetching bill text only when no CRS summary exists, latest version first |
| `test_render.py` | Snapshots of the digest HTML for a fixed fixture database (one representative, two senators): `tests/snapshots/digest_2025-11-10.html` (Senate votes on H.R. 5371, sponsorships) and `digest_2025-09-08.html` (a House rule vote) |
| `test_deliver.py`, `test_cli.py` | STARTTLS delivery, delivery failure keeping the file, CLI stages and `run_log` rows |

To update the render snapshot after an intended change, run `UPDATE_SNAPSHOTS=1 uv run pytest tests/test_render.py`.

---

## Data model

The schema in `src/tracker/schema.sql` matches the spec, including the nullable columns phase 2 will need (`openstates_id`, `jurisdiction`). There is one addition: `raw_record.normalized_hash`, which stores the payload hash at the last successful normalize. It is how "skip re-normalizing unchanged payloads" works across runs.

`raw_record` kinds are `legislators_current`, `legislators_historical`, `member`, `sponsored`, `cosponsored`, `house_vote_list`, `house_vote`, `house_vote_members`, `amendment`, `bill`, `bill_summaries`, `bill_text`, `bill_text_version`, `senate_vote_menu` and `senate_vote`.

`sync_state` holds per-session vote cursors (`house_votes:119-1`, `senate_votes:119-2`, …) and per-chamber ingest health (`ingest_attempt:senate`, `ingest_ok:senate`), which the digest reads to warn about stale data.

---

## Decisions and deviations from the spec

- **Former members are mapped through `legislators-historical.json`.** The spec uses only `legislators-current`, but two senators who voted in 2025 (Graham, S293, and Mullin, S419) have since left the Senate. With the current file alone, their rows would be dropped from every vote they cast, and caucus totals would no longer match senate.gov. The historical file is used only for ID mapping, filtered to people who served in the configured Congress. Only current members can be tracked. Set `storage.crosswalk_historical_url = ""` to turn this off.
- **Speaker elections** are mapped as described under Normalize: a vote for the winner is yea and any other candidate is nay.
- **The Speaker** is detected from the crosswalk's `leadership_roles`.
- **No temperature for Anthropic**, because the Messages API now rejects it (see Summarize). The Anthropic provider uses the official SDK rather than raw HTTP.
- **Delegation splits** are computed when the digest is rendered, from stored votes, rather than stored as findings. The `finding.kind` values in the spec have no slot for them.
- **The project lives at the repository root** rather than in a `vote-tracker/` subdirectory. The package is still `src/tracker`, and the distribution name is `vote-tracker`.
- **Cosponsorship dates.** As far as the documentation shows, a member's cosponsored-legislation list gives each bill's introduced date, not the date the member signed on. So for each cosponsored bill, ingest fetches `/bill/{congress}/{type}/{number}/cosponsors` once, when the stored copy does not yet include the member, and takes `sponsorshipDate` and `sponsorshipWithdrawnDate` from it. If a list item does carry a `cosponsoredDate`, that date is used and the extra request is skipped. Withdrawn cosponsorships are not listed in the digest.
- **Output schema bounds.** The schema sent to Ollama includes `minItems: 2, maxItems: 4` for the bullets. Ollama's constrained decoding enforces those bounds, and without them `llama3.1` returned six bullets twice in a row. The Anthropic provider removes these keywords, because structured outputs do not support them. Word limits are checked only by pydantic, for both providers.

---

## Open questions

- **senate.gov access.** Scripted requests from this machine get `403` from Akamai. Before relying on the Senate half of the digest, check from the machine that will run the job: `curl -sI -A "vote-tracker/0.1 (you@example.com)" https://www.senate.gov/legislative/LIS/roll_call_lists/vote_menu_119_1.xml`. If it is blocked there too, the legitimate options are to ask the Senate (the LIS / webmaster) about access for a polite, rate-limited client, or to use a published mirror of the same XML. Spoofing a browser is not an option.
- **Has Congress.gov added Senate votes?** Not as of 2026-10-23: `GET /v3/senate-vote/119/1` returns `404 Unknown resource`. Check again before the next phase. If an endpoint appears, prefer it and keep the XML client as a fallback.
- **Does the House member-votes response include party?** Yes: each result has `voteParty` and `voteState`, so no lookup is needed. Settled with the first fixture.
- **Which local model gives acceptable summaries?** Only lightly tested. `llama3.1:latest` (8B) produced valid, neutral bullets for H.R. 3424 from its CRS summary, but reused the bill's own short title ("SPACE Act of 2025") as the plain title, which the prompt asks it to avoid. Compare two or three models (for example `qwen3.8`, `gemma4:26b`) on the same 10 bills before choosing.
- **Unrecorded Congress.gov endpoints.** Sponsored and cosponsored legislation, `/bill/.../cosponsors`, `/bill/.../text` and `/amendment/...` are built from documented shapes, not recorded responses (see `tests/fixtures/congress_gov/unverified/`). Record them with a real key, then confirm two things: whether cosponsored items carry a cosponsorship date, and the format label for HTML bill text.

---

## Project layout

```
pyproject.toml            uv project; entry point `tracker`
config.example.toml
src/tracker/
  cli.py                  typer commands, logging, run_log
  config.py               pydantic settings: config.toml + env vars
  db.py                   connection, schema migration, raw_record / sync_state / run_log helpers
  schema.sql
  sources/
    http.py               shared client: User-Agent, per-host throttle, retry, key redaction
    congress_gov.py       Congress.gov API v3: auth, paging, endpoints
    senate_xml.py         senate.gov vote menu and vote XML
    legislators.py        congress-legislators crosswalk (current + former members)
  ingest.py               stage 1
  normalize/
    __init__.py           stage 2 runner
    house.py, senate.py   vote parsers
    bills.py              bills, CRS summaries, sponsorships, rule links
    common.py             bill refs, rule detection, HTML stripping, upserts
    context.py            per-run normalize state
  classify.py             vote_class and threshold rules (pure functions)
  analyze.py              stage 3: caucus positions, findings, scoring, selection, splits, stats
  llm/
    base.py               SummaryProvider protocol
    ollama.py, anthropic.py
    prompts.py            system prompt, user message, output schema + pydantic validation
  summarize.py            stage 4: basis choice, cache, retry, fallback, summary view
  render.py               stage 5: digest view model and vote-context templates
  deliver.py              stage 6: SMTP with STARTTLS
  templates/digest.html.j2
tests/
  fixtures/{congress_gov,senate,legislators}/
  test_*.py
```

## Later phases

- **Phase 2: Colorado state legislators**, through the [Open States API v3](https://docs.openstates.org/api-v3/). Get session identifiers from `/jurisdictions` rather than guessing them. The schema already has `jurisdiction`, `level` and `openstates_id`, so it needs no migration.
- **Phase 3: polish.** Topic weighting (`[scoring.topic_boost]` already exists), nomination details from Congress.gov, and a browsable archive of past digests.
