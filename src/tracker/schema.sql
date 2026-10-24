-- Legislator Vote Tracker schema. Every statement is idempotent so init-db is safe to re-run.
-- raw_record holds every payload as received; all other tables derive from it and can be rebuilt.

-- Raw payloads exactly as received (API key stripped from URLs)
CREATE TABLE IF NOT EXISTS raw_record (
  source          TEXT NOT NULL,     -- 'congress_gov' | 'senate_xml' | 'legislators' (later: 'openstates')
  kind            TEXT NOT NULL,     -- 'house_vote', 'house_vote_members', 'senate_vote_menu', 'senate_vote',
                                     -- 'bill', 'bill_summaries', 'bill_text', 'member', 'legislators_current', ...
  external_id     TEXT NOT NULL,     -- e.g. 'house/119/1/245', 'senate/119/1/312', 'bill/119/hr/1234'
  url             TEXT,
  fetched_at      TEXT NOT NULL,     -- ISO 8601 UTC
  payload         TEXT NOT NULL,     -- JSON or XML as received
  payload_hash    TEXT NOT NULL,     -- skip re-normalizing unchanged payloads
  normalized_hash TEXT,              -- payload_hash at the last successful normalize; NULL = never normalized
  PRIMARY KEY (source, kind, external_id)
);

CREATE TABLE IF NOT EXISTS legislator (
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

CREATE TABLE IF NOT EXISTS party_membership (
  legislator_id INTEGER NOT NULL REFERENCES legislator(id),
  party         TEXT NOT NULL,       -- 'R', 'D', 'I', ...
  caucus        TEXT NOT NULL,       -- party they caucus with; equals party unless independent
  start_date    TEXT,
  end_date      TEXT
);

CREATE TABLE IF NOT EXISTS bill (
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
CREATE TABLE IF NOT EXISTS rule_link (
  rule_bill_id       INTEGER NOT NULL REFERENCES bill(id),
  underlying_bill_id INTEGER NOT NULL REFERENCES bill(id),
  PRIMARY KEY (rule_bill_id, underlying_bill_id)
);

CREATE TABLE IF NOT EXISTS bill_summary (
  bill_id      INTEGER NOT NULL REFERENCES bill(id),
  source       TEXT NOT NULL,        -- 'crs'
  version_code TEXT NOT NULL,
  action_date  TEXT,
  text         TEXT NOT NULL,        -- HTML stripped to plain text
  PRIMARY KEY (bill_id, source, version_code)
);

CREATE TABLE IF NOT EXISTS vote_event (
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
CREATE TABLE IF NOT EXISTS member_vote (
  vote_event_id  INTEGER NOT NULL REFERENCES vote_event(id),
  legislator_id  INTEGER NOT NULL REFERENCES legislator(id),
  position       TEXT NOT NULL,      -- 'yea' | 'nay' | 'present' | 'not_voting'
  position_raw   TEXT NOT NULL,      -- as published, e.g. 'Guilty'
  party_at_vote  TEXT NOT NULL,
  caucus_at_vote TEXT NOT NULL,
  PRIMARY KEY (vote_event_id, legislator_id)
);

CREATE TABLE IF NOT EXISTS sponsorship (
  bill_id       INTEGER NOT NULL REFERENCES bill(id),
  legislator_id INTEGER NOT NULL REFERENCES legislator(id),
  role          TEXT NOT NULL,       -- 'sponsor' | 'cosponsor'
  date          TEXT,
  withdrawn     TEXT,
  PRIMARY KEY (bill_id, legislator_id, role)
);

-- Derived per vote and caucus; recomputed by analyze
CREATE TABLE IF NOT EXISTS caucus_position (
  vote_event_id INTEGER NOT NULL REFERENCES vote_event(id),
  caucus        TEXT NOT NULL,
  yea           INTEGER NOT NULL,
  nay           INTEGER NOT NULL,
  majority      TEXT,                -- 'yea' | 'nay' | NULL on a tie
  cohesion      REAL,                -- max(yea, nay) / (yea + nay)
  PRIMARY KEY (vote_event_id, caucus)
);

CREATE TABLE IF NOT EXISTS finding (
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
CREATE TABLE IF NOT EXISTS llm_summary (
  bill_id        INTEGER NOT NULL REFERENCES bill(id),
  basis          TEXT NOT NULL,      -- 'crs' | 'bill_text' | 'title_only'
  basis_version  TEXT NOT NULL,      -- CRS version_code, text version date, or 'title'
  prompt_version TEXT NOT NULL,
  model          TEXT NOT NULL,
  output_json    TEXT NOT NULL,
  created_at     TEXT NOT NULL,
  PRIMARY KEY (bill_id, basis, basis_version, prompt_version)
);

CREATE TABLE IF NOT EXISTS sync_state (
  source   TEXT PRIMARY KEY,         -- e.g. 'house_votes:119-1', 'senate_votes:119-1', 'legislators'
  cursor   TEXT,
  last_run TEXT
);

CREATE TABLE IF NOT EXISTS run_log (
  id          INTEGER PRIMARY KEY,
  command     TEXT NOT NULL,
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  status      TEXT,                  -- 'ok' | 'partial' | 'failed'
  stats_json  TEXT                   -- counts fetched, skipped, errors, per source
);

CREATE INDEX IF NOT EXISTS idx_vote_event_date ON vote_event (chamber, vote_date);
CREATE INDEX IF NOT EXISTS idx_member_vote_legislator ON member_vote (legislator_id);
CREATE INDEX IF NOT EXISTS idx_finding_period ON finding (legislator_id, period_start);
CREATE INDEX IF NOT EXISTS idx_sponsorship_legislator ON sponsorship (legislator_id, date);
