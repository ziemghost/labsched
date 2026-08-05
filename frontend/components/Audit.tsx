"use client";

import { useCallback, useEffect, useState } from "react";
import { LabState, api } from "@/lib/api";

interface AuditRow {
  seq: number;
  at: string;
  actor: string;
  action: string;
  run_id: string | null;
  step_id: string | null;
  device_id: string | null;
  sample_id: string | null;
  token_id: string | null;
  intervention_id: string | null;
  detail: Record<string, unknown>;
}

const ACTION_COLOR = (a: string) =>
  a.startsWith("intervention")
    ? "text-amber"
    : a.startsWith("fault") || a.includes("failed") || a.includes("denied")
    ? "text-rose"
    : a.startsWith("reservation") || a.startsWith("run.admitted")
    ? "text-sky"
    : a.includes("done") || a.includes("resumed")
    ? "text-mint"
    : "text-slate-400";

export default function Audit({ state }: { state: LabState }) {
  const [rows, setRows] = useState<AuditRow[]>([]);
  const [runId, setRunId] = useState("");
  const [deviceId, setDeviceId] = useState("");
  const [tokenId, setTokenId] = useState("");

  const load = useCallback(async () => {
    const q = new URLSearchParams({ limit: "300" });
    if (runId) q.set("run_id", runId);
    if (deviceId) q.set("device_id", deviceId);
    if (tokenId) q.set("token_id", tokenId);
    setRows(await api<AuditRow[]>(`/api/audit?${q}`));
  }, [runId, deviceId, tokenId]);

  useEffect(() => {
    load();
    const t = setInterval(load, 1500);
    return () => clearInterval(t);
  }, [load]);

  return (
    <div className="panel flex h-full flex-col overflow-hidden">
      <div className="flex flex-wrap items-center gap-2 border-b border-line px-3 py-2">
        <span className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">
          Audit — append only
        </span>
        <div className="ml-auto flex gap-2">
          <Select value={runId} onChange={setRunId} label="all runs"
                  options={state.runs.map((r) => [r.id, r.name])} />
          <Select value={deviceId} onChange={setDeviceId} label="all instruments"
                  options={state.devices.map((d) => [d.id, d.id])} />
          <Select value={tokenId} onChange={setTokenId} label="all tokens"
                  options={state.tokens.map((t) => [t.id, t.label])} />
        </div>
      </div>

      <div className="flex-1 overflow-auto">
        <table className="w-full">
          <thead className="sticky top-0 bg-ink-900">
            <tr>
              <th className="th">#</th>
              <th className="th">Time</th>
              <th className="th">Actor</th>
              <th className="th">Action</th>
              <th className="th">Instrument</th>
              <th className="th">Step</th>
              <th className="th">Token</th>
              <th className="th">Detail</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.seq} className="border-t border-line/50 hover:bg-ink-850">
                <td className="td font-mono text-slate-600">{r.seq}</td>
                <td className="td font-mono text-slate-500">
                  {new Date(r.at).toLocaleTimeString()}
                </td>
                <td className="td font-mono text-slate-400">{r.actor}</td>
                <td className={`td font-mono ${ACTION_COLOR(r.action)}`}>{r.action}</td>
                <td className="td font-mono text-slate-500">{r.device_id ?? ""}</td>
                <td className="td font-mono text-slate-500">{r.step_id ?? ""}</td>
                <td className="td font-mono text-slate-500">{r.token_id ?? ""}</td>
                <td className="td max-w-lg truncate text-slate-500" title={JSON.stringify(r.detail)}>
                  {Object.entries(r.detail)
                    .filter(([, v]) => v !== null && v !== undefined)
                    .map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`)
                    .join("  ")}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {rows.length === 0 && (
          <p className="py-10 text-center text-xs text-slate-600">No entries match.</p>
        )}
      </div>
    </div>
  );
}

function Select({
  value,
  onChange,
  label,
  options,
}: {
  value: string;
  onChange: (v: string) => void;
  label: string;
  options: [string, string][];
}) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="rounded border border-ink-600 bg-ink-850 px-2 py-1 text-[11px] text-slate-300"
    >
      <option value="">{label}</option>
      {options.map(([v, l]) => (
        <option key={v} value={v}>
          {l}
        </option>
      ))}
    </select>
  );
}
