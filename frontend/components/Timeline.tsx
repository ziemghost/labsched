"use client";

/** Gantt of steps against instruments. Hand-rolled SVG, no chart library. */

import { useMemo, useState } from "react";
import { LabState, fmtLab, ts } from "@/lib/api";

const ROW_H = 26;
const LABEL_W = 88;

/**
 * The window used to be a fixed 180s against 6-10s steps, which drew every bar
 * as a 4% sliver pinned to the right edge.
 *
 * "fit" now spans the most recent work rather than everything since boot: an
 * hour of uptime would otherwise put an hour on the axis and take the bars
 * back to slivers, which is the same bug with a longer fuse. The fixed spans
 * are labelled in **lab time**, because that is what the axis and the header
 * are labelled in.
 */
const WINDOWS: { key: string; labSeconds: number | null }[] = [
  { key: "fit", labSeconds: null },
  { key: "1 h", labSeconds: 3600 },
  { key: "6 h", labSeconds: 6 * 3600 },
  { key: "1 d", labSeconds: 86400 },
];
/** How many of the most recent steps "fit" tries to keep on screen. */
const FIT_STEPS = 24;
/** …and how many typical step-lengths wide the window may get, so a bar stays
 *  visible however long the lab has been idle between them. */
const MAX_STEPS_WIDE = 30;
const MIN_WINDOW_S = 30;

const STATE_FILL: Record<string, string> = {
  running: "#4ade80",
  done: "#3b6b52",
  scheduled: "#38bdf8",
  failed: "#fb7185",
  cancelled: "#4b5563",
  blocked_on_human: "#fbbf24",
};

export default function Timeline({
  state,
  serverNow,
}: {
  state: LabState;
  serverNow: () => number;
}) {
  const now = serverNow();
  const [pick, setPick] = useState("fit");

  const rows = useMemo(() => {
    const byDevice = new Map<string, typeof state.steps>();
    for (const d of state.devices) byDevice.set(d.id, []);
    for (const s of state.steps) {
      if (!s.device_id || !s.started_at) continue;
      if (!byDevice.has(s.device_id)) byDevice.set(s.device_id, []);
      byDevice.get(s.device_id)!.push(s);
    }
    return [...byDevice.entries()];
  }, [state.steps, state.devices]);

  /**
   * Where "fit" starts. Bounded two ways, because bounding only one of them
   * has now failed twice: by step COUNT (the newest FIT_STEPS steps) and by
   * SPAN (a multiple of how long a step actually takes). Twenty-four steps
   * scattered over twelve lab-days is a bounded count and a useless span --
   * every bar lands under the 3px floor.
   */
  const fitFrom = useMemo(() => {
    const starts: number[] = [];
    const durations: number[] = [];
    for (const [, steps] of rows) {
      for (const s of steps) {
        const t = ts(s.started_at);
        if (t === null) continue;
        starts.push(t);
        durations.push(((ts(s.finished_at) ?? now) - t) / 1000);
      }
    }
    if (starts.length === 0) return now - MIN_WINDOW_S * 1000;
    starts.sort((a, b) => b - a);
    const byCount = starts[Math.min(starts.length, FIT_STEPS) - 1];

    // A typical step should be a readable slice of the width, so the span is
    // capped at MAX_STEPS_WIDE times the median step duration.
    const sorted = [...durations].sort((a, b) => a - b);
    const median = sorted[Math.floor(sorted.length / 2)] || MIN_WINDOW_S;
    const bySpan = starts[0] - median * MAX_STEPS_WIDE * 1000;
    return Math.max(byCount, bySpan);
  }, [rows, now]);

  /** The end of the most recent work, which is what "fit" should end at. */
  const fitTo = useMemo(() => {
    let max = -Infinity;
    for (const [, steps] of rows) {
      for (const s of steps) {
        const end = ts(s.finished_at) ?? now;
        if (end > max) max = end;
      }
    }
    return Number.isFinite(max) ? max : now;
  }, [rows, now]);

  const labSeconds = WINDOWS.find((w) => w.key === pick)?.labSeconds ?? null;

  // "fit" ends at the last thing that happened, not at `now`. Pinning the
  // right edge to the clock meant an idle lab spent the whole width on the
  // idle: bounding the step COUNT does not bound the SPAN, so 24 steps
  // scattered over four lab-days drew 24 three-pixel slivers. Bounding both
  // is what actually keeps a bar readable.
  const fitSpanS = Math.max(MIN_WINDOW_S, ((fitTo - fitFrom) / 1000) * 1.12);
  const windowS = labSeconds ? labSeconds / (state.time_scale || 1) : fitSpanS;
  const t1 = labSeconds ? now : fitTo + fitSpanS * 1000 * 0.04;
  const t0 = t1 - windowS * 1000;

  const width = 1000;
  const height = rows.length * ROW_H + 26;
  const xOf = (t: number) =>
    LABEL_W + ((t - t0) / (windowS * 1000)) * (width - LABEL_W - 8);

  // Roughly eight gridlines whatever the span, on a round number of seconds.
  const step = [5, 10, 15, 30, 60, 120, 300, 600, 1800, 7200].find(
    (s) => windowS / s <= 9) ?? 86400;
  const ticks: number[] = [];
  for (let s = Math.ceil(windowS / step) * step; s >= 0; s -= step) {
    const t = t1 - s * 1000;
    if (t >= t0) ticks.push(t);
  }

  return (
    <div className="panel flex h-full flex-col overflow-hidden">
      <div className="flex flex-wrap items-center gap-3 border-b border-line px-3 py-2">
        <span className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">
          Instrument timeline · last {fmtLab(windowS, state.time_scale)} of lab time
        </span>
        <div className="flex overflow-hidden rounded border border-ink-600">
          {WINDOWS.map((w) => (
            <button
              key={w.key}
              onClick={() => setPick(w.key)}
              className={`px-2 py-0.5 text-[10px] transition ${
                pick === w.key
                  ? "bg-ink-800 text-slate-200"
                  : "bg-ink-850 text-slate-500 hover:text-slate-300"
              }`}
            >
              {w.key}
            </button>
          ))}
        </div>
        <div className="ml-auto flex gap-3 text-[10px]">
          {Object.entries(STATE_FILL).map(([k, v]) => (
            <span key={k} className="flex items-center gap-1 text-slate-500">
              <span className="h-2 w-2 rounded-sm" style={{ background: v }} />
              {k.replace(/_/g, " ")}
            </span>
          ))}
        </div>
      </div>

      <div className="flex-1 overflow-auto p-2">
        <svg viewBox={`0 0 ${width} ${height}`} className="w-full" style={{ minHeight: height }}>
          {ticks.map((t, i) => (
            <g key={i}>
              <line x1={xOf(t)} x2={xOf(t)} y1={16} y2={height - 6} stroke="#1b2130" strokeWidth="1" />
              <text x={xOf(t)} y={11} fontSize="9" textAnchor="middle" className="fill-slate-600 font-mono">
                -{fmtLab((t1 - t) / 1000, state.time_scale)}
              </text>
            </g>
          ))}

          {rows.map(([deviceId, steps], r) => {
            const y = 22 + r * ROW_H;
            const dev = state.devices.find((d) => d.id === deviceId);
            const down = dev?.state === "offline" || dev?.quarantined;
            return (
              <g key={deviceId}>
                <rect x={0} y={y} width={width} height={ROW_H - 4} fill={r % 2 ? "#0e1118" : "transparent"} />
                <text x={6} y={y + 14} fontSize="10.5" className="font-mono" fill={down ? "#fb7185" : "#94a3b8"}>
                  {deviceId}
                </text>
                {steps.map((s) => {
                  const start = ts(s.started_at)!;
                  const end = ts(s.finished_at) ?? now;
                  // Drop it before clamping. `x1` was clamped to LABEL_W
                  // first and `x2` to `x1 + 3`, so the "off the left edge"
                  // test could never fire and every step older than the
                  // window drew as a 3px stub stacked in the label gutter.
                  if (xOf(end) < LABEL_W) return null;
                  const x1 = Math.max(LABEL_W, xOf(start));
                  const x2 = Math.max(x1 + 3, xOf(end));
                  return (
                    <g key={s.id}>
                      <title>
                        {s.run_name} — {s.name} ({s.state}
                        {s.error ? `: ${s.error}` : ""})
                      </title>
                      <rect
                        x={x1}
                        y={y + 3}
                        width={x2 - x1}
                        height={ROW_H - 11}
                        rx="2.5"
                        fill={STATE_FILL[s.state] ?? "#475569"}
                        opacity={s.state === "done" ? 0.75 : 1}
                      />
                      {x2 - x1 > 46 && (
                        <text x={x1 + 5} y={y + 15} fontSize="9" className="font-mono" fill="#0b0e14">
                          {s.name.slice(0, Math.floor((x2 - x1) / 6))}
                        </text>
                      )}
                    </g>
                  );
                })}
              </g>
            );
          })}

          {/* The clock line, only when the clock is on screen: in
              "fit" on an idle lab, now is off to the right of the last work. */}
          {now <= t1 && (
            <line x1={xOf(now)} x2={xOf(now)} y1={16} y2={height - 6}
                  stroke="#a78bfa" strokeWidth="1.5" />
          )}
        </svg>
      </div>

      <RunStrip state={state} />
    </div>
  );
}

function RunStrip({ state }: { state: LabState }) {
  return (
    <div className="max-h-52 overflow-y-auto border-t border-line">
      <table className="w-full">
        <thead className="sticky top-0 bg-ink-900">
          <tr>
            <th className="th">Run</th>
            <th className="th">Pri</th>
            <th className="th">State</th>
            <th className="th">Progress</th>
            <th className="th">Token</th>
            <th className="th">Note</th>
          </tr>
        </thead>
        <tbody>
          {state.runs.map((r) => (
            <tr key={r.id} className="border-t border-line/60">
              <td className="td text-slate-200">{r.name}</td>
              <td className="td font-mono">{r.priority}</td>
              <td className="td">
                <span
                  className={`chip ${
                    r.state === "done"
                      ? "border-mint/30 text-mint"
                      : r.state === "failed"
                      ? "border-rose/30 text-rose"
                      : r.state === "awaiting_review"
                      ? "border-amber/30 text-amber"
                      : r.state === "cancelled"
                      ? "border-ink-600 text-slate-500"
                      : "border-sky/30 text-sky"
                  }`}
                >
                  {r.state}
                </span>
              </td>
              <td className="td font-mono text-slate-400">
                {r.steps_done}/{r.steps_total}
              </td>
              <td className="td font-mono text-slate-500">{r.token_id}</td>
              <td className="td max-w-xs truncate text-slate-500">
                {r.drain_requested ? `draining: ${r.drain_reason}` : r.note ?? ""}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
