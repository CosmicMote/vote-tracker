// Shapes returned by the FastAPI backend (src/tracker/web/app.py, src/tracker/render.py).

export interface Week {
  start: string;
  end: string;
  label: string;
}

export interface WeeksResponse {
  default: string;
  first: string;
  last: string;
  weeks: Week[];
}

export interface Bill {
  bill_id: number;
  number: string;
  official_title: string | null;
  url: string | null;
  plain_title: string;
  bullets: string[];
  caveat: string | null;
  excerpt: string | null;
  provenance: string;
  see_above: string | null;
  anchor: string | null;
}

export interface VoteLine {
  context: string[];
  date: string;
  roll_label: string;
  roll_url: string | null;
  chamber: "house" | "senate";
}

export interface Item {
  heading: string;
  number: string | null;
  bills: Bill[];
  votes: VoteLine[];
  positions?: string[];
}

export interface Sponsorship {
  role: string;
  number: string;
  plain_title: string;
  date: string;
  url: string | null;
}

export interface MemberStats {
  cast: number;
  missed: number;
  present: number;
  contested: number;
  agreed: number;
}

export interface MemberSection {
  anchor: string;
  title: string;
  name: string;
  label: string;
  chamber: "house" | "senate";
  stats: MemberStats;
  breaks: Item[];
  notable: Item[];
  missed_count: number;
  missed_listed: Item[];
  is_speaker: boolean;
  sponsorships: Sponsorship[];
}

export interface Digest {
  subject: string;
  week_label: string;
  splits: Item[];
  members: MemberSection[];
  as_of: { source: string; at: string }[];
  stale: string[];
  prompt_version: string;
  senate_source: "voteview" | "senate_gov";
}

export type JobStatus = "queued" | "running" | "ok" | "partial" | "failed" | "cancelled";

export interface Job {
  id: number;
  kind: string;
  label: string;
  key: string | null;
  status: JobStatus;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  message: string | null;
  stats: Record<string, unknown> | null;
}

export interface DigestResponse {
  week: Week;
  digest: Digest;
  reanalyzed: boolean;
  summaries: { pending: number; job: Job | null; waiting_for: string | null };
}

export interface Legislator {
  bioguide: string;
  lis: string | null;
  name: string;
  chamber: "house" | "senate";
  state: string;
  district: string | null;
  party: string;
  caucus: string;
}

export interface MembersResponse {
  tracked: string[];
  members: Legislator[];
  missing: string[];
  job?: Job | null;
}

export interface RunLogRow {
  id: number;
  command: string;
  started_at: string;
  finished_at: string | null;
  status: string | null;
  stats_json: string | null;
}

export interface JobsResponse {
  jobs: Job[];
  runs: RunLogRow[];
}

export interface Status {
  config_path: string;
  db_path: string;
  congress: number;
  provider: string;
  email_enabled: boolean;
  secrets: Record<string, boolean>;
  stale: string[];
  votes: Record<string, { votes: number; latest: string }>;
  has_crosswalk: boolean;
  busy: boolean;
  weeks: { first: string; last: string };
  schedule: Schedule;
}

export interface Schedule {
  running: boolean;
  update: { next: string; needs_key: boolean } | null;
  digest: {
    email_enabled: boolean;
    when: string;
    next: string;
    covers: string;
    needs_password: boolean;
    problems: string[];
    last_sent: { week: string; to: string; at: string } | null;
  };
}

// A small subset of JSON Schema, as produced by pydantic's model_json_schema().
export interface JsonSchema {
  type?: string;
  title?: string;
  description?: string;
  enum?: (string | number)[];
  const?: string | number;
  format?: string;
  default?: unknown;
  minimum?: number;
  maximum?: number;
  exclusiveMinimum?: number;
  items?: JsonSchema;
  properties?: Record<string, JsonSchema>;
  additionalProperties?: JsonSchema | boolean;
  anyOf?: JsonSchema[];
  allOf?: JsonSchema[];
  $ref?: string;
  $defs?: Record<string, JsonSchema>;
  required?: string[];
}

export type ConfigValues = Record<string, Record<string, unknown>>;

export interface ConfigResponse {
  path: string;
  values: ConfigValues;
  schema: JsonSchema;
  secrets: Record<string, boolean>;
  job?: Job | null;
}

export interface FieldError {
  loc: string[];
  msg: string;
}
