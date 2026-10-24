import type {
  ConfigResponse,
  ConfigValues,
  DigestResponse,
  FieldError,
  Job,
  JobsResponse,
  Legislator,
  MembersResponse,
  Status,
  WeeksResponse,
} from "./types";

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
    public fields: FieldError[] = [],
  ) {
    super(message);
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = {};
  if (method !== "GET") headers["X-Tracker-Client"] = "web"; // required by the server for changes
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(path, { method, headers, body: body === undefined ? undefined : JSON.stringify(body) });
  if (!res.ok) {
    let detail: unknown = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      /* not JSON */
    }
    if (Array.isArray(detail)) {
      const fields = detail as FieldError[];
      throw new ApiError(fields.map((f) => `${f.loc.join(".")}: ${f.msg}`).join("; "), res.status, fields);
    }
    throw new ApiError(String(detail), res.status);
  }
  return (await res.json()) as T;
}

export const api = {
  status: () => request<Status>("GET", "/api/status"),
  weeks: () => request<WeeksResponse>("GET", "/api/weeks"),
  digest: (week: string) => request<DigestResponse>("GET", `/api/digest/${week}`),
  emailUrl: (week: string) => `/api/digest/${week}/email`,
  summarize: (week: string) => request<Job>("POST", `/api/digest/${week}/summarize`),
  send: (week: string) => request<Job>("POST", `/api/digest/${week}/send`),
  legislators: () => request<{ legislators: Legislator[] }>("GET", "/api/legislators"),
  members: () => request<MembersResponse>("GET", "/api/members"),
  saveMembers: (tracked: string[]) => request<MembersResponse>("PUT", "/api/members", { tracked }),
  config: () => request<ConfigResponse>("GET", "/api/config"),
  saveConfig: (values: ConfigValues) => request<ConfigResponse>("PUT", "/api/config", { values }),
  validateConfig: (values: ConfigValues) =>
    request<{ errors: FieldError[] }>("POST", "/api/config/validate", { values }),
  jobs: () => request<JobsResponse>("GET", "/api/jobs"),
  startJob: (kind: string, extra: Record<string, unknown> = {}) => request<Job>("POST", "/api/jobs", { kind, ...extra }),
};

export const isActive = (job: Job | null | undefined) => !!job && (job.status === "queued" || job.status === "running");
