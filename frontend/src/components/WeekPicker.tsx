import type { Week } from "../types";

interface Props {
  weeks: Week[]; // newest first
  value: string;
  onChange: (start: string) => void;
}

export default function WeekPicker({ weeks, value, onChange }: Props) {
  const i = weeks.findIndex((w) => w.start === value);
  const older = i >= 0 && i < weeks.length - 1 ? weeks[i + 1] : null;
  const newer = i > 0 ? weeks[i - 1] : null;
  return (
    <div className="week-picker" role="group" aria-label="Week">
      <button className="btn icon" onClick={() => older && onChange(older.start)} disabled={!older} title="Previous week">
        ‹
      </button>
      <select className="select" value={value} onChange={(e) => onChange(e.target.value)} aria-label="Select week">
        {weeks.map((w, n) => (
          <option key={w.start} value={w.start}>
            {w.label}
            {n === 0 ? " (last week)" : ""}
          </option>
        ))}
      </select>
      <button className="btn icon" onClick={() => newer && onChange(newer.start)} disabled={!newer} title="Next week">
        ›
      </button>
    </div>
  );
}
