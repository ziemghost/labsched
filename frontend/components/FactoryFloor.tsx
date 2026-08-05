"use client";

/**
 * Top-down view of the lab floor.
 *
 * The only thing this computes for itself is position: where a plate is along
 * a path it is already known to be travelling, and how far through a step's
 * declared duration we are. Both come from server timestamps measured against
 * the server's clock.
 *
 * Everything else is rendered exactly as the last snapshot reported it. The
 * floor may be smoother than the poll interval; it may not be ahead of it.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import {
  Device,
  Intervention,
  KIND_LABEL,
  deviceName,
  LabState,
  Sample,
  api,
  ts,
} from "@/lib/api";

const CELL_W = 210;
const CELL_H = 180;
const PAD = 24;
const TILE_W = 152;
const TILE_H = 108;
const PLATE = 17;

type XY = { x: number; y: number };

const STATE_STYLE: Record<
  string,
  { fill: string; stroke: string; text: string; glow?: string }
> = {
  idle: { fill: "#141821", stroke: "#2b3345", text: "#64748b" },
  reserved: { fill: "#131c28", stroke: "#38bdf8", text: "#7dd3fc", glow: "#38bdf8" },
  busy: { fill: "#122019", stroke: "#4ade80", text: "#86efac", glow: "#4ade80" },
  faulted: { fill: "#221317", stroke: "#fb7185", text: "#fda4af", glow: "#fb7185" },
  offline: { fill: "#0d0f14", stroke: "#242a36", text: "#3f4759" },
};

export default function FactoryFloor({
  state,
  refresh,
  serverNow,
}: {
  state: LabState;
  refresh: () => void;
  serverNow: () => number;
}) {
  const [frame, setFrame] = useState(0);
  const [openBubble, setOpenBubble] = useState<string | null>(null);
  const [focused, setFocused] = useState<string | null>(null);
  const raf = useRef<number>(0);

  // Redraw at display rate so movement is smooth between 1s polls.
  useEffect(() => {
    const loop = () => {
      setFrame((f) => (f + 1) % 1_000_000);
      raf.current = requestAnimationFrame(loop);
    };
    raf.current = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf.current);
  }, []);

  const now = serverNow();
  const openIvs = state.interventions.filter((i) => i.state === "open");

  // Close a popover the moment its intervention stops being open, so a
  // resolved decision cannot linger on screen.
  useEffect(() => {
    if (openBubble && !openIvs.some((i) => i.id === openBubble)) setOpenBubble(null);
  }, [openBubble, openIvs]);

  const geom = useMemo(() => {
    const xs = [state.storage_tile.x, ...state.devices.map((d) => d.layout_x)];
    const ys = [state.storage_tile.y, ...state.devices.map((d) => d.layout_y)];
    const minX = Math.min(...xs);
    const minY = Math.min(...ys);
    const maxX = Math.max(...xs);
    const maxY = Math.max(...ys);
    const center = (x: number, y: number): XY => ({
      x: (x - minX) * CELL_W + CELL_W / 2 + PAD,
      y: (y - minY) * CELL_H + CELL_H / 2 + PAD,
    });
    return {
      center,
      width: (maxX - minX + 1) * CELL_W + PAD * 2,
      height: (maxY - minY + 1) * CELL_H + PAD * 2,
      corridorY: center(minX, (minY + maxY) / 2).y,
      storage: center(state.storage_tile.x, state.storage_tile.y),
    };
  }, [state.devices, state.storage_tile]);

  const deviceById = useMemo(
    () => new Map(state.devices.map((d) => [d.id, d])),
    [state.devices]
  );
  const anchor = (deviceId: string | null): XY =>
    deviceId && deviceById.has(deviceId)
      ? geom.center(deviceById.get(deviceId)!.layout_x, deviceById.get(deviceId)!.layout_y)
      : geom.storage;

  const ivByDevice = new Map(openIvs.filter((i) => i.device_id).map((i) => [i.device_id!, i]));

  return (
    <div className="flex h-full gap-3">
      <div className="panel relative min-w-0 flex-1 overflow-hidden bg-ink-950">
        <FloorGrid />
        <svg
          viewBox={`0 0 ${geom.width} ${geom.height}`}
          className="h-full w-full"
          preserveAspectRatio="xMidYMid meet"
        >
          <defs>
            <pattern id="hatch" width="7" height="7" patternTransform="rotate(45)" patternUnits="userSpaceOnUse">
              <line x1="0" y1="0" x2="0" y2="7" stroke="#fb7185" strokeWidth="2" opacity="0.22" />
            </pattern>
            <filter id="glow" x="-60%" y="-60%" width="220%" height="220%">
              <feGaussianBlur stdDeviation="5" result="b" />
              <feMerge>
                <feMergeNode in="b" />
                <feMergeNode in="SourceGraphic" />
              </feMerge>
            </filter>
          </defs>

          <Belts devices={state.devices} geom={geom} />

          <StorageTile at={geom.storage} count={state.samples.filter(
            (s) => s.location_kind === "storage" && s.state !== "destroyed"
          ).length} />

          {state.devices.map((d) => (
            <Machine
              key={d.id}
              device={d}
              at={geom.center(d.layout_x, d.layout_y)}
              now={now}
              focused={focused === d.id}
              intervention={ivByDevice.get(d.id) ?? null}
              onBubble={() => setOpenBubble(ivByDevice.get(d.id)?.id ?? null)}
            />
          ))}

          {state.samples.map((s, i) => (
            <Plate
              key={s.id}
              sample={s}
              index={i}
              now={now}
              anchor={anchor}
              storage={geom.storage}
              corridorY={geom.corridorY}
            />
          ))}
        </svg>

        {openBubble && (
          <InterventionPopover
            intervention={openIvs.find((i) => i.id === openBubble)!}
            onClose={() => setOpenBubble(null)}
            onResolved={refresh}
          />
        )}

        <ChaosBar state={state} refresh={refresh} />
      </div>

      <AlertRail
        interventions={openIvs}
        devices={state.devices}
        onFocus={(deviceId, ivId) => {
          setFocused(deviceId);
          setOpenBubble(ivId);
          setTimeout(() => setFocused(null), 2600);
        }}
      />
    </div>
  );
}

/* ------------------------------------------------------------------ floor -- */

function FloorGrid() {
  return (
    <div
      className="pointer-events-none absolute inset-0 opacity-[0.5]"
      style={{
        backgroundImage:
          "linear-gradient(#1a1f2b 1px, transparent 1px), linear-gradient(90deg, #1a1f2b 1px, transparent 1px)",
        backgroundSize: "28px 28px",
      }}
    />
  );
}

function Belts({ devices, geom }: { devices: Device[]; geom: any }) {
  const segs: string[] = [];
  const seen = new Set<number>();
  for (const d of devices) {
    const c = geom.center(d.layout_x, d.layout_y);
    segs.push(`M ${c.x} ${c.y} L ${c.x} ${geom.corridorY}`);
    seen.add(Math.round(c.x));
  }
  const xs = [...seen, Math.round(geom.storage.x)].sort((a, b) => a - b);
  segs.push(`M ${xs[0]} ${geom.corridorY} L ${xs[xs.length - 1]} ${geom.corridorY}`);
  segs.push(`M ${geom.storage.x} ${geom.storage.y} L ${geom.storage.x} ${geom.corridorY}`);
  return (
    <g>
      {segs.map((d, i) => (
        <path key={i} d={d} stroke="#1c2230" strokeWidth="12" fill="none" strokeLinecap="round" />
      ))}
      {segs.map((d, i) => (
        <path
          key={`d${i}`}
          d={d}
          stroke="#39445c"
          strokeWidth="2"
          fill="none"
          className="belt"
          strokeLinecap="round"
        />
      ))}
    </g>
  );
}

function StorageTile({ at, count }: { at: XY; count: number }) {
  const w = 118;
  const h = 132;
  return (
    <g transform={`translate(${at.x - w / 2}, ${at.y - h / 2})`}>
      <rect width={w} height={h} rx="8" fill="#101420" stroke="#2b3345" strokeWidth="1.5" />
      {[0, 1, 2, 3].map((i) => (
        <line
          key={i}
          x1="12"
          x2={w - 12}
          y1={26 + i * 26}
          y2={26 + i * 26}
          stroke="#232a39"
          strokeWidth="3"
          strokeLinecap="round"
        />
      ))}
      <text x={w / 2} y="16" textAnchor="middle" className="fill-slate-500 font-mono" fontSize="11">
        PLATE STORE
      </text>
      <text x={w / 2} y={h - 8} textAnchor="middle" className="fill-slate-600 font-mono" fontSize="10">
        {count} plate{count === 1 ? "" : "s"}
      </text>
    </g>
  );
}

/* ---------------------------------------------------------------- machine -- */

function Machine({
  device,
  at,
  now,
  focused,
  intervention,
  onBubble,
}: {
  device: Device;
  at: XY;
  now: number;
  focused: boolean;
  intervention: Intervention | null;
  onBubble: () => void;
}) {
  const down = device.state === "offline" || device.quarantined;
  const style = STATE_STYLE[down ? "offline" : device.state] ?? STATE_STYLE.idle;
  const x = at.x - TILE_W / 2;
  const y = at.y - TILE_H / 2;

  // Progress is elapsed/declared, both from the server. It is capped at 1 and
  // annotated when it runs over, rather than being allowed to imply completion.
  const started = ts(device.step_started_at);
  const dur = (device.step_duration_s ?? 0) * 1000;
  const running = device.step_state === "running" && started !== null && dur > 0;
  const raw = running ? (now - started!) / dur : 0;
  const progress = Math.max(0, Math.min(1, raw));
  const overdue = running && raw > 1.02;

  return (
    <g>
      {focused && (
        <rect
          x={x - 6}
          y={y - 6}
          width={TILE_W + 12}
          height={TILE_H + 12}
          rx="14"
          fill="none"
          stroke="#fbbf24"
          className="focus-ring"
        />
      )}

      <rect
        x={x}
        y={y}
        width={TILE_W}
        height={TILE_H}
        rx="10"
        fill={style.fill}
        stroke={style.stroke}
        strokeWidth={device.state === "idle" || down ? 1.25 : 2}
        filter={style.glow && !down ? "url(#glow)" : undefined}
      />

      {device.suspect && !down && (
        <rect x={x} y={y} width={TILE_W} height={TILE_H} rx="10" fill="url(#hatch)" />
      )}

      <MachineIcon kind={device.kind} cx={at.x} cy={at.y - 14} color={style.text} dim={down} />

      <text
        x={at.x}
        y={y + TILE_H - 26}
        textAnchor="middle"
        fontSize="11.5"
        fontWeight="500"
        fill={down ? "#4b5563" : "#cbd5e1"}
      >
        {deviceName(device.id, device.kind)}
      </text>
      <text
        x={at.x}
        y={y + TILE_H - 12}
        textAnchor="middle"
        className="font-mono"
        fontSize="9.5"
        fill={style.text}
      >
        {down
          ? device.quarantined
            ? "QUARANTINED"
            : "OFFLINE"
          : device.state.toUpperCase()}
      </text>

      {running && (
        <>
          <rect x={x + 14} y={y + TILE_H - 40} width={TILE_W - 28} height="5" rx="2.5" fill="#1e2735" />
          <rect
            x={x + 14}
            y={y + TILE_H - 40}
            width={(TILE_W - 28) * progress}
            height="5"
            rx="2.5"
            fill={overdue ? "#fbbf24" : "#4ade80"}
          />
          <text x={at.x} y={y + 14} textAnchor="middle" fontSize="9" className="font-mono" fill="#64748b">
            {overdue ? "OVERDUE" : device.step_name?.slice(0, 22)}
          </text>
        </>
      )}

      {down && (
        <g stroke={device.quarantined ? "#fb7185" : "#3f4759"} strokeWidth="3" strokeLinecap="round">
          <line x1={x + 26} y1={y + 22} x2={x + TILE_W - 26} y2={y + TILE_H - 22} />
          <line x1={x + TILE_W - 26} y1={y + 22} x2={x + 26} y2={y + TILE_H - 22} />
        </g>
      )}

      {intervention && (
        <g
          className="alert-bubble cursor-pointer"
          onClick={onBubble}
          style={{ transformBox: "fill-box" }}
        >
          <circle cx={at.x} cy={y - 24} r="16" fill="#fbbf24" stroke="#78350f" strokeWidth="2" />
          <text
            x={at.x}
            y={y - 17}
            textAnchor="middle"
            fontSize="21"
            fontWeight="800"
            fill="#1c1408"
          >
            !
          </text>
        </g>
      )}
    </g>
  );
}

function MachineIcon({
  kind,
  cx,
  cy,
  color,
  dim,
}: {
  kind: string;
  cx: number;
  cy: number;
  color: string;
  dim: boolean;
}) {
  const c = dim ? "#39404f" : color;
  const s = 1.15;
  const g = (children: React.ReactNode) => (
    <g transform={`translate(${cx}, ${cy}) scale(${s})`} stroke={c} fill="none" strokeWidth="2">
      {children}
    </g>
  );
  switch (kind) {
    case "liquid_handler":
      return g(
        <>
          <rect x="-22" y="-14" width="44" height="10" rx="2" />
          <line x1="-14" y1="-4" x2="-14" y2="8" />
          <line x1="0" y1="-4" x2="0" y2="8" />
          <line x1="14" y1="-4" x2="14" y2="8" />
          <rect x="-24" y="10" width="48" height="7" rx="2" strokeOpacity="0.55" />
        </>
      );
    case "incubator":
      return g(
        <>
          <rect x="-20" y="-15" width="40" height="32" rx="4" />
          <path d="M -9 8 q 5 -7 0 -13 q -5 -6 0 -12" strokeWidth="1.7" />
          <path d="M 4 8 q 5 -7 0 -13 q -5 -6 0 -12" strokeWidth="1.7" />
        </>
      );
    case "bli_reader":
      return g(
        <>
          <rect x="-21" y="-14" width="42" height="30" rx="4" />
          <circle cx="0" cy="1" r="8" />
          <line x1="0" y1="-14" x2="0" y2="-7" strokeWidth="3" />
          <line x1="-13" y1="12" x2="13" y2="12" strokeOpacity="0.5" />
        </>
      );
    default:
      return g(
        <>
          <rect x="-21" y="-14" width="42" height="30" rx="4" />
          {[-10, 0, 10].map((dx) =>
            [-5, 5].map((dy) => (
              <circle key={`${dx},${dy}`} cx={dx} cy={dy + 1} r="2.6" fill={c} stroke="none" />
            ))
          )}
        </>
      );
  }
}

/* ------------------------------------------------------------------ plate -- */

function Plate({
  sample,
  index,
  now,
  anchor,
  storage,
  corridorY,
}: {
  sample: Sample;
  index: number;
  now: number;
  anchor: (id: string | null) => XY;
  storage: XY;
  corridorY: number;
}) {
  // The store tile holds four rows of three. Past that, plates wrap back into
  // the same slots with a small offset so they read as stacked. The demo has
  // 21 plates, and the old unbounded layout ran them out of the box and over
  // the "21 plates" caption underneath it.
  const ROWS = 4;
  const COLS = 3;
  const cell = index % (ROWS * COLS);
  const layer = Math.floor(index / (ROWS * COLS));
  const slot: XY = {
    x: storage.x - 32 + (cell % COLS) * 32 + layer * 3,
    y: storage.y - 34 + Math.floor(cell / COLS) * 26 - layer * 3,
  };

  let pos: XY;
  let moving = false;

  if (sample.location_kind === "transit") {
    const from = sample.transit_from ? anchor(sample.transit_from) : slot;
    const to = sample.transit_to ? anchor(sample.transit_to) : slot;
    const t0 = ts(sample.transit_started_at);
    const t1 = ts(sample.transit_eta);
    const t = t0 && t1 && t1 > t0 ? Math.max(0, Math.min(1, (now - t0) / (t1 - t0))) : 1;
    pos = alongPath(from, to, corridorY, t);
    moving = true;
  } else if (sample.location_kind === "device" && sample.location_device_id) {
    const c = anchor(sample.location_device_id);
    pos = { x: c.x, y: c.y + 22 };
  } else {
    pos = slot;
  }

  const destroyed = sample.state === "destroyed";
  const fill = destroyed ? "#2a1418" : moving ? "#1e293b" : "#16202e";
  const stroke = destroyed ? "#fb7185" : moving ? "#a78bfa" : "#475569";

  return (
    <g transform={`translate(${pos.x}, ${pos.y})`} className={destroyed ? "" : ""}>
      <title>
        {sample.label} — {sample.state}
        {sample.run_name ? ` — ${sample.run_name}` : ""}
      </title>
      <rect
        x={-PLATE / 2}
        y={-PLATE / 2}
        width={PLATE}
        height={PLATE}
        rx="3"
        fill={fill}
        stroke={stroke}
        strokeWidth="1.6"
        opacity={destroyed ? 0.9 : 1}
      />
      {!destroyed &&
        [-4.5, 0.5].map((dy) =>
          [-4.5, 0.5, 5.5].map((dx) => (
            <circle key={`${dx},${dy}`} cx={dx - 0.5} cy={dy} r="1.35" fill={stroke} opacity="0.85" />
          ))
        )}
      {destroyed && (
        <g stroke="#fb7185" strokeWidth="2" strokeLinecap="round">
          <line x1={-5} y1={-5} x2={5} y2={5} />
          <line x1={5} y1={-5} x2={-5} y2={5} />
        </g>
      )}
      {moving && (
        <circle r={PLATE} fill="none" stroke="#a78bfa" strokeOpacity="0.25" strokeWidth="1.5" />
      )}
    </g>
  );
}

/** Route via the central corridor, so plates travel on the belts rather than
 *  cutting diagonally across machines. Three segments, walked by arc length. */
function alongPath(from: XY, to: XY, corridorY: number, t: number): XY {
  const pts: XY[] = [
    from,
    { x: from.x, y: corridorY },
    { x: to.x, y: corridorY },
    to,
  ];
  const legs = pts.slice(1).map((p, i) => Math.hypot(p.x - pts[i].x, p.y - pts[i].y));
  const total = legs.reduce((a, b) => a + b, 0);
  if (total === 0) return to;
  let d = t * total;
  for (let i = 0; i < legs.length; i++) {
    if (d <= legs[i] || i === legs.length - 1) {
      const f = legs[i] === 0 ? 1 : Math.min(1, d / legs[i]);
      return {
        x: pts[i].x + (pts[i + 1].x - pts[i].x) * f,
        y: pts[i].y + (pts[i + 1].y - pts[i].y) * f,
      };
    }
    d -= legs[i];
  }
  return to;
}

/* ------------------------------------------------------------- alert rail -- */

function AlertRail({
  interventions,
  devices,
  onFocus,
}: {
  interventions: Intervention[];
  devices: Device[];
  onFocus: (deviceId: string, ivId: string) => void;
}) {
  const down = devices.filter((d) => d.state === "offline" || d.quarantined);
  return (
    <div className="panel flex w-72 shrink-0 flex-col overflow-hidden">
      <div className="flex items-center justify-between border-b border-line px-3 py-2">
        <span className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">
          Alerts
        </span>
        {interventions.length > 0 && (
          <span className="rounded-full bg-amber px-1.5 py-0.5 text-[10px] font-bold text-ink-950">
            {interventions.length}
          </span>
        )}
      </div>
      <div className="flex-1 space-y-1.5 overflow-y-auto p-2">
        {interventions.length === 0 && down.length === 0 && (
          <p className="px-1 py-6 text-center text-xs text-slate-600">Nothing needs you.</p>
        )}
        {interventions.map((iv) => (
          <button
            key={iv.id}
            onClick={() => iv.device_id && onFocus(iv.device_id, iv.id)}
            className="w-full rounded border border-amber/35 bg-amber/[0.07] px-2.5 py-2 text-left transition hover:bg-amber/[0.14]"
          >
            <div className="flex items-center gap-1.5">
              <span className="text-sm leading-none text-amber">!</span>
              <span className="text-[11px] font-medium text-amber">
                {deviceName(iv.device_id)}
              </span>
            </div>
            <div className="mt-1 text-[11px] font-medium text-slate-200">
              {iv.kind.replace(/_/g, " ")}
            </div>
            <div className="text-[10px] text-slate-500">{iv.run_name ?? iv.run_id}</div>
          </button>
        ))}
        {down.map((d) => (
          <div key={d.id} className="rounded border border-ink-700 bg-ink-850 px-2.5 py-2">
            <div className="text-[11px] font-medium text-slate-400">
              {deviceName(d.id, d.kind)}
            </div>
            <div className="text-[10px] text-slate-500">
              {d.quarantined ? "quarantined" : "offline"} — {d.note ?? "no heartbeat"}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

/* ---------------------------------------------------------------- popover -- */

function InterventionPopover({
  intervention,
  onClose,
  onResolved,
}: {
  intervention: Intervention;
  onClose: () => void;
  onResolved: () => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const pick = async (option: string) => {
    setBusy(option);
    setErr(null);
    try {
      await api(`/api/interventions/${intervention.id}/resolve`, {
        method: "POST",
        body: JSON.stringify({ option, resolved_by: "operator" }),
      });
      onResolved();
      onClose();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      setBusy(null);
    }
  };

  return (
    <div className="pop-in absolute left-1/2 top-1/2 z-30 w-[440px] -translate-x-1/2 -translate-y-1/2 rounded-lg border border-amber/45 bg-ink-900 shadow-2xl shadow-black/70">
      <div className="flex items-start justify-between border-b border-line px-4 py-3">
        <div>
          <div className="flex items-center gap-2">
            <span className="text-amber">!</span>
            <h3 className="text-sm font-semibold text-slate-100">
              {intervention.kind.replace(/_/g, " ")}
            </h3>
          </div>
          <p className="mt-0.5 font-mono text-[11px] text-slate-500">
            {deviceName(intervention.device_id)} ·{" "}
            {intervention.run_name ?? intervention.run_id} ·{" "}
            {intervention.step_name ?? intervention.step_id}
          </p>
        </div>
        <button onClick={onClose} className="px-1 text-lg leading-none text-slate-500 hover:text-slate-300">
          ×
        </button>
      </div>

      <div className="space-y-3 px-4 py-3">
        <p className="text-xs leading-relaxed text-slate-300">{intervention.message}</p>

        <div className="rounded border border-line bg-ink-850 px-3 py-2">
          <div className="grid grid-cols-2 gap-y-1 text-[11px]">
            <span className="text-slate-500">Plate</span>
            <span className="font-mono text-slate-300">
              {intervention.sample_label ?? intervention.sample_id ?? "—"}
            </span>
            <span className="text-slate-500">Instrument held</span>
            <span className={intervention.holds.device ? "text-amber" : "text-slate-400"}>
              {intervention.holds.device ? "yes — locked" : "no — released"}
            </span>
            <span className="text-slate-500">Plate held</span>
            <span className={intervention.holds.sample ? "text-amber" : "text-slate-400"}>
              {intervention.holds.sample ? "yes — locked" : "no — released"}
            </span>
          </div>
          <p className="mt-2 border-t border-line pt-2 text-[10.5px] leading-relaxed text-slate-500">
            {intervention.holds.rationale}
          </p>
        </div>

        {err && (
          <p className="rounded border border-rose/40 bg-rose/10 px-2 py-1.5 text-[11px] text-rose">
            {err}
          </p>
        )}

        <div className="space-y-1.5">
          {intervention.options.map((o) => (
            <button
              key={o.key}
              disabled={busy !== null}
              onClick={() => pick(o.key)}
              className="w-full rounded border border-ink-600 bg-ink-850 px-3 py-2 text-left transition hover:border-sky/50 hover:bg-ink-800 disabled:opacity-50"
            >
              <div className="text-xs font-medium text-slate-100">
                {busy === o.key ? "applying…" : o.label}
              </div>
              <div className="mt-0.5 text-[10.5px] leading-relaxed text-slate-500">
                {o.consequence}
              </div>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}

/* --------------------------------------------------------------- chaos bar -- */

function ChaosBar({ state, refresh }: { state: LabState; refresh: () => void }) {
  const [kind, setKind] = useState("plate_stuck");
  const [device, setDevice] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [caps, setCaps] = useState<Record<string, string[] | null>>({});

  const KINDS = [
    "sample_integrity_unknown",
    "batch_destroyed",
    "unexpected_reading",
    "plate_stuck",
    "calibration_drift",
    "device_offline",
    "device_timeout",
    "comms_error",
  ];

  // Which faults each instrument can physically produce, from the same
  // taxonomy the backend enforces, so the picker cannot offer "liquid
  // handler aborted mid-transfer" against an incubator.
  useEffect(() => {
    api<{ human: Record<string, { capabilities: string[] | null }> }>("/api/fault-kinds")
      .then((f) =>
        setCaps(
          Object.fromEntries(
            Object.entries(f.human).map(([k, v]) => [k, v.capabilities]),
          ),
        ),
      )
      .catch(() => {});
  }, []);

  const capable = (d: { capabilities: string[] }) => {
    const need = caps[kind];
    return !need || d.capabilities.some((c) => need.includes(c));
  };
  const targets = state.devices.filter(capable);

  // Switching to a fault the selected instrument cannot raise clears it back
  // to "any", rather than leaving an impossible pairing armed.
  useEffect(() => {
    if (device && !targets.some((d) => d.id === device)) setDevice("");
  }, [kind, device, targets]);

  const inject = async () => {
    try {
      await api("/api/sim/fault", {
        method: "POST",
        body: JSON.stringify({ kind, device_id: device || null }),
      });
      setMsg(`queued ${kind}${device ? ` on ${device}` : ""}`);
      setTimeout(() => setMsg(null), 2500);
      refresh();
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e));
    }
  };

  const drift = async () => {
    const target = device || state.devices.find((d) => d.kind === "bli_reader")?.id;
    if (!target) return;
    try {
      await api(`/api/sim/drift/${target}`, { method: "POST" });
      setMsg(`${target} is now drifting — nothing will be reported as an error`);
      setTimeout(() => setMsg(null), 4000);
      refresh();
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e));
    }
  };

  const setChaos = async (rate: number) => {
    await api("/api/sim/chaos", { method: "POST", body: JSON.stringify({ rate }) });
    refresh();
  };

  const rate = state.chaos.rate;

  return (
    <div className="absolute bottom-3 left-3 right-3 flex flex-wrap items-center gap-2 rounded-lg border border-line bg-ink-900/95 px-3 py-2 backdrop-blur">
      <span className="text-[10px] font-semibold uppercase tracking-wider text-slate-500">
        Chaos
      </span>
      <div className="flex overflow-hidden rounded border border-ink-600">
        {[0, 0.15, 0.4, 0.8].map((r) => (
          <button
            key={r}
            onClick={() => setChaos(r)}
            className={`px-2 py-1 text-[11px] transition ${
              Math.abs(rate - r) < 0.001
                ? "bg-rose/20 text-rose"
                : "bg-ink-850 text-slate-400 hover:bg-ink-800"
            }`}
          >
            {r === 0 ? "off" : `${Math.round(r * 100)}%`}
          </button>
        ))}
      </div>

      <div className="ml-2 h-4 w-px bg-line" />

      <select
        value={kind}
        onChange={(e) => setKind(e.target.value)}
        className="rounded border border-ink-600 bg-ink-850 px-2 py-1 text-[11px] text-slate-300"
      >
        {KINDS.map((k) => (
          <option key={k} value={k}>
            {k.replace(/_/g, " ")}
          </option>
        ))}
      </select>
      <select
        value={device}
        onChange={(e) => setDevice(e.target.value)}
        className="rounded border border-ink-600 bg-ink-850 px-2 py-1 text-[11px] text-slate-300"
      >
        <option value="">{caps[kind] ? "any capable instrument" : "any instrument"}</option>
        {targets.map((d) => (
          <option key={d.id} value={d.id}>
            {d.id}
          </option>
        ))}
      </select>
      <button onClick={inject} className="btn-danger">
        Break it
      </button>
      <button
        onClick={drift}
        className="btn"
        title="No fault is raised. The instrument keeps reporting success and quietly returns off-baseline controls; QC has to catch it."
      >
        Drift silently
      </button>

      {msg && <span className="font-mono text-[10.5px] text-slate-500">{msg}</span>}

      <div className="ml-auto flex items-center gap-3 font-mono text-[10.5px] text-slate-600">
        <span>tick {state.scheduler.ticks}</span>
        {state.scheduler.last_error && (
          <span className="text-rose">scheduler: {state.scheduler.last_error}</span>
        )}
      </div>
    </div>
  );
}
