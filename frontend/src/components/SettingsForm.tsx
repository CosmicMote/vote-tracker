import { useEffect, useState } from "react";
import type { ConfigValues, FieldError, JsonSchema } from "../types";

// Renders every config.toml setting from the pydantic JSON Schema the server sends.

function resolve(schema: JsonSchema, root: JsonSchema): JsonSchema {
  const ref = schema.$ref ?? schema.allOf?.find((s) => s.$ref)?.$ref;
  if (!ref) return schema;
  const name = ref.split("/").pop()!;
  return { ...root.$defs?.[name], ...schema, $ref: undefined, allOf: undefined };
}

const nullable = (s: JsonSchema) => s.anyOf?.some((x) => x.type === "null") ?? false;
const baseType = (s: JsonSchema) => s.type ?? s.anyOf?.find((x) => x.type !== "null")?.type;
const humanize = (key: string) => key.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());

const SECTION_TITLES: Record<string, string> = {
  members: "Members",
  congress: "Congress",
  http: "HTTP",
  senate: "Senate votes",
  llm: "Summaries (LLM)",
  digest: "Digest",
  email: "Email",
  schedule: "Schedule",
  backup: "Backup",
  scoring: "Scoring",
  storage: "Storage",
};

interface FieldProps {
  name: string;
  schema: JsonSchema;
  value: unknown;
  error?: string;
  onChange: (v: unknown) => void;
}

function NumberInput({ value, integer, onChange }: { value: unknown; integer: boolean; onChange: (v: unknown) => void }) {
  const [text, setText] = useState(String(value ?? ""));
  useEffect(() => setText(String(value ?? "")), [value]);
  return (
    <input
      className="input narrow"
      inputMode={integer ? "numeric" : "decimal"}
      value={text}
      onChange={(e) => {
        const raw = e.target.value;
        setText(raw);
        // Send what was typed when it isn't a number, so the server's validation reports it.
        const n = Number(raw);
        onChange(raw.trim() !== "" && Number.isFinite(n) ? n : raw);
      }}
    />
  );
}

function MappingEditor({ value, onChange }: { value: Record<string, number>; onChange: (v: unknown) => void }) {
  const [newKey, setNewKey] = useState("");
  const entries = Object.entries(value ?? {});
  const set = (k: string, v: number | undefined) => {
    const next = { ...value };
    if (v === undefined) delete next[k];
    else next[k] = v;
    onChange(next);
  };
  return (
    <div className="mapping">
      {entries.length === 0 && <p className="muted small">None.</p>}
      {entries.map(([k, v]) => (
        <div className="mapping-row" key={k}>
          <span className="mono">{k}</span>
          <NumberInput value={v} integer={false} onChange={(n) => set(k, n as number)} />
          <button type="button" className="btn small" onClick={() => set(k, undefined)}>
            Remove
          </button>
        </div>
      ))}
      <div className="mapping-row">
        <input className="input" placeholder="Add key…" value={newKey} onChange={(e) => setNewKey(e.target.value)} />
        <button
          type="button"
          className="btn small"
          disabled={!newKey.trim() || newKey.trim() in (value ?? {})}
          onClick={() => {
            set(newKey.trim(), 0);
            setNewKey("");
          }}
        >
          Add
        </button>
      </div>
    </div>
  );
}

function Field({ name, schema, value, error, onChange }: FieldProps) {
  const type = baseType(schema);
  let control: React.ReactNode;
  if (schema.enum) {
    control = (
      <select className="select" value={String(value)} onChange={(e) => onChange(e.target.value)}>
        {schema.enum.map((o) => (
          <option key={String(o)} value={String(o)}>
            {String(o)}
          </option>
        ))}
      </select>
    );
  } else if (type === "boolean") {
    control = (
      <label className="check">
        <input type="checkbox" checked={!!value} onChange={(e) => onChange(e.target.checked)} /> Enabled
      </label>
    );
  } else if (type === "integer" || type === "number") {
    control = <NumberInput value={value} integer={type === "integer"} onChange={onChange} />;
  } else if (type === "array") {
    control = (
      <input
        className="input"
        value={((value as string[]) ?? []).join(", ")}
        onChange={(e) => onChange(e.target.value.split(",").map((x) => x.trim()).filter(Boolean))}
      />
    );
  } else if (type === "object") {
    control = <MappingEditor value={value as Record<string, number>} onChange={onChange} />;
  } else {
    control = (
      <input
        className="input"
        value={(value as string | null) ?? ""}
        placeholder={nullable(schema) ? "(not set)" : undefined}
        onChange={(e) => onChange(nullable(schema) && e.target.value === "" ? null : e.target.value)}
      />
    );
  }
  return (
    <div className={error ? "field has-error" : "field"} data-field={name}>
      <label className="field-label">
        {humanize(name)} <span className="mono muted small">{name}</span>
      </label>
      {control}
      {schema.description && <p className="help">{schema.description}</p>}
      {error && <p className="error-text">{error}</p>}
    </div>
  );
}

interface Props {
  schema: JsonSchema;
  values: ConfigValues;
  errors: FieldError[];
  onChange: (values: ConfigValues) => void;
}

export default function SettingsForm({ schema, values, errors, onChange }: Props) {
  const sections = Object.entries(schema.properties ?? {}).filter(([k]) => k !== "secrets");
  const errorFor = (section: string, key: string) =>
    errors
      .filter((e) => e.loc[0] === section && e.loc[1] === key)
      .map((e) => e.msg)
      .join("; ") || undefined;
  return (
    <div className="settings">
      {sections.map(([section, raw]) => {
        const s = resolve(raw, schema);
        return (
          <fieldset className="card settings-section" key={section} data-section={section}>
            <legend>
              <h2>
                {SECTION_TITLES[section] ?? humanize(section)} <span className="mono muted small">[{section}]</span>
              </h2>
            </legend>
            {s.description && <p className="muted">{s.description}</p>}
            <div className="fields">
              {Object.entries(s.properties ?? {}).map(([key, fs]) => (
                <Field
                  key={key}
                  name={key}
                  schema={resolve(fs, schema)}
                  value={values[section]?.[key]}
                  error={errorFor(section, key)}
                  onChange={(v) => onChange({ ...values, [section]: { ...values[section], [key]: v } })}
                />
              ))}
            </div>
          </fieldset>
        );
      })}
    </div>
  );
}
