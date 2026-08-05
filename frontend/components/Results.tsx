"use client";

/**
 * The result plane.
 *
 * Separate from execution on purpose: `run.state = done` means the robot
 * finished, and says nothing about whether the number is trustworthy. This
 * state can move backwards — a released result becomes `held` when the
 * instrument's calibration epoch is later called into question.
 */

import { useState } from "react";
import {
  LabState,
  Result,
  api,
  deviceName,
  getActingToken,
} from "@/lib/api";

const STATE_STYLE: Record<Result["state"], string> = {
  released: "border-mint/30 text-mint",
  pending_qc: "border-sky/30 text-sky",
  held: "border-amber/30 text-amber",
  invalidated: "border-rose/30 text-rose",
};

const VERDICT_STYLE: Record<string, string> = {
  pass: "text-mint",
  warn: "text-amber",
  fail: "text-rose",
};

export default function Results({
  state,
  refresh,
}: {
  state: LabState;
  refresh: () => void;
}) {
  const [filter, setFilter] = useState<string>("");
  const [msg, setMsg] = useState<string | null>(null);
  const rows = state.results.filter((r) => !filter || r.state === filter);

  // Counted over the table, not over the page this component was handed:
  // the payload is capped, so counting it here read "released 120" against
  // 155 and showed no chip for a state whose rows had all aged out.
  const counts = state.result_state_counts ?? {};

  const suspectEpoch = async (deviceId: string, epoch: number) => {
    setMsg(null);
    try {
      const res = await api<{
        intervention_id: string | null;
        results_held: number;
        held_elsewhere: number;
      }>(
        `/api/devices/${deviceId}/calibration/${epoch}/suspect`,
        {
          method: "POST",
          body: JSON.stringify({ note: "control chart reviewed, epoch not trustworthy" }),
        }
      );
      // Marking the same epoch twice returns the question that is already
      // open. Saying "raised" a second time claims something that did not
      // happen, and the count is the honest part of the sentence anyway.
      // The no-question case has two readings, and this button is built from
      // a result row, so "no results were produced" is the one thing it can
      // never truthfully say without checking which.
      setMsg(
        !res.intervention_id
          ? res.held_elsewhere
            ? `${deviceId} epoch ${epoch} closed suspect; its ${res.held_elsewhere} result(s) are already held by another open question`
            : `no results were produced by ${deviceId} in epoch ${epoch}`
          : res.results_held
          ? `${res.intervention_id}: ${res.results_held} result(s) pulled back from ${deviceId} epoch ${epoch}`
          : `${res.intervention_id} is already open over ${deviceId} epoch ${epoch}`
      );
      refresh();
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e));
    }
  };

  // Instrument x epoch pairs that actually produced results, for the
  // reach-backwards control.
  const epochs = Array.from(
    new Map(
      state.results
        .filter((r) => r.calibration_epoch != null)
        .map((r) => [`${r.device_id}:${r.calibration_epoch}`, r])
    ).values()
  );

  return (
    <div className="panel flex h-full flex-col overflow-hidden">
      <div className="flex flex-wrap items-center gap-2 border-b border-line px-3 py-2">
        <span className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">
          Results — <span className="text-slate-500">done ≠ trustworthy</span>
        </span>
        <div className="ml-2 flex gap-1">
          {["", "released", "pending_qc", "held", "invalidated"].map((s) => (
            <button
              key={s || "all"}
              onClick={() => setFilter(s)}
              className={`rounded px-2 py-0.5 text-[10.5px] transition ${
                filter === s
                  ? "bg-ink-700 text-slate-100"
                  : "text-slate-500 hover:bg-ink-850"
              }`}
            >
              {s ? s.replace(/_/g, " ") : "all"}
              {s && counts[s] ? ` ${counts[s]}` : ""}
            </button>
          ))}
        </div>

        <div className="ml-auto flex items-center gap-1.5">
          <span className="text-[10px] text-slate-600">
            mark an epoch suspect (reaches backwards):
          </span>
          {epochs.slice(0, 6).map((r) => (
            <button
              key={`${r.device_id}:${r.calibration_epoch}`}
              onClick={() => suspectEpoch(r.device_id, r.calibration_epoch!)}
              className="btn-danger"
              title={`Every result ${deviceName(r.device_id)} produced in epoch ${r.calibration_epoch} comes back into question`}
            >
              {r.device_id}@{r.calibration_epoch}
            </button>
          ))}
        </div>
      </div>

      {msg && (
        <p className="border-b border-line bg-ink-850 px-3 py-2 font-mono text-[11px] text-slate-400">
          {msg} <span className="text-slate-600">(as {getActingToken()})</span>
        </p>
      )}

      <div className="flex-1 overflow-auto">
        <table className="w-full">
          <thead className="sticky top-0 bg-ink-900">
            <tr>
              <th className="th">Run</th>
              <th className="th">Step</th>
              <th className="th">Instrument</th>
              <th className="th">Epoch</th>
              <th className="th">Control</th>
              <th className="th">QC</th>
              <th className="th">State</th>
              <th className="th">Note</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id} className="border-t border-line/50 hover:bg-ink-850">
                <td className="td text-slate-300">{r.run_name}</td>
                <td className="td text-slate-400">{r.step_name}</td>
                <td className="td text-slate-400" title={r.device_id}>
                  {deviceName(r.device_id)}
                </td>
                <td className="td font-mono text-slate-500">{r.calibration_epoch ?? "—"}</td>
                <td className="td font-mono text-slate-400">
                  {r.control_value != null ? r.control_value.toFixed(3) : "—"}
                </td>
                <td className={`td font-mono ${VERDICT_STYLE[r.qc_verdict ?? ""] ?? ""}`}>
                  {/* Null until QC has concluded something. A pending row used
                      to read "pass", which is a verdict for a check that has
                      not run. */}
                  {r.qc_verdict ?? <span className="text-slate-600">—</span>}
                </td>
                <td className="td">
                  <span className={`chip ${STATE_STYLE[r.state]}`}>
                    {r.state.replace(/_/g, " ")}
                  </span>
                </td>
                <td className="td max-w-md truncate text-slate-500" title={r.qc_note ?? ""}>
                  {r.invalidated_reason ?? r.qc_note ?? ""}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {rows.length === 0 && (
          <p className="py-10 text-center text-xs text-slate-600">
            No results yet — they appear as steps finish.
          </p>
        )}
      </div>
    </div>
  );
}
