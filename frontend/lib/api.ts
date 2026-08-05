"use client";

import { useCallback, useEffect, useRef, useState } from "react";

export type DeviceState = "idle" | "reserved" | "busy" | "faulted" | "offline";

export interface Device {
  id: string;
  kind: string;
  capabilities: string[];
  state: DeviceState;
  quarantined: boolean;
  suspect: boolean;
  note: string | null;
  layout_x: number;
  layout_y: number;
  heartbeat_age_s: string | null;
  calibration_epoch: number | null;
  step_id: string | null;
  step_name: string | null;
  step_state: string | null;
  step_started_at: string | null;
  step_duration_s: number | null;
  step_run_id: string | null;
  run_name: string | null;
  held_sample_id: string | null;
  intervention_id: string | null;
  intervention_kind: string | null;
}

export interface Sample {
  id: string;
  label: string;
  state: "ok" | "in_transit" | "parked" | "destroyed";
  location_kind: "storage" | "device" | "transit";
  location_device_id: string | null;
  transit_from: string | null;
  transit_to: string | null;
  transit_started_at: string | null;
  transit_eta: string | null;
  run_id: string | null;
  run_name: string | null;
  run_state: string | null;
}

export interface Run {
  id: string;
  name: string;
  priority: number;
  state: string;
  token_id: string;
  allowed_kinds: string[];
  drain_requested: boolean;
  drain_reason: string | null;
  note: string | null;
  created_at: string;
  updated_at: string;
  steps_done: number;
  steps_total: number;
}

export interface Step {
  id: string;
  run_id: string;
  run_name: string;
  priority: number;
  idx: number;
  name: string;
  capability: string;
  duration_s: number;
  credit_cost: number;
  sample_id: string;
  state: string;
  device_id: string | null;
  attempt: number;
  tried_devices: string[];
  scheduled_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  error: string | null;
  result: Record<string, unknown> | null;
}

export interface InterventionOption {
  key: string;
  label: string;
  consequence: string;
  authority: string;
  reversible: boolean;
  requires_reason?: boolean;
  agent_resolvable: boolean;
}

export interface Result {
  id: string;
  run_id: string;
  step_id: string;
  sample_id: string;
  device_id: string;
  calibration_epoch: number | null;
  payload: Record<string, unknown>;
  control_value: number | null;
  qc_verdict: "pass" | "warn" | "fail" | null;
  qc_note: string | null;
  state: "pending_qc" | "released" | "held" | "invalidated";
  invalidated_reason: string | null;
  created_at: string;
  released_at: string | null;
  step_name?: string;
  run_name?: string;
}

export interface Consequence {
  option: string;
  reversible: boolean;
  requires_reason: boolean;
  authority: string;
  agent_resolvable: boolean;
  runs_aborted: number;
  plates_destroyed: number;
  steps_requeued: number;
  credits_released: number;
  credits_spent_again: number;
  instruments_quarantined: number;
  other_runs_affected: number;
}

export interface Intervention {
  id: string;
  run_id: string;
  step_id: string | null;
  device_id: string | null;
  sample_id: string | null;
  kind: string;
  message: string;
  detail: Record<string, any>;
  options: InterventionOption[];
  required_authority: string;
  agent_resolvable: boolean;
  expires_at: string | null;
  escalation_policy: string | null;
  group_key: string | null;
  affected_sample_ids: string[];
  affected_run_ids: string[];
  acknowledged_by: string | null;
  version: number;
  resolved_by_token: string | null;
  resolution_reason: string | null;
  holds: { device: boolean; sample: boolean; rationale: string };
  state: "open" | "resolved";
  resolution: string | null;
  resolved_by: string | null;
  created_at: string;
  resolved_at: string | null;
  step_name?: string | null;
  run_name?: string | null;
  sample_label?: string | null;
  capability?: string | null;
  device_kind?: string | null;
}

export interface Token {
  id: string;
  parent_id: string | null;
  label: string;
  tier: "org" | "project" | "agent";
  allowed_kinds: string[];
  max_concurrent: number;
  max_wallclock_s: number;
  max_run_credits: number;
  budget_credits: number;
  credits_spent: number;
  expires_at: string;
  revoked: boolean;
  revoked_at: string | null;
  revoked_by: string | null;
  authorities: string[];
  created_at: string;
}

export interface LabState {
  now: string;
  scheduler: { ticks: number; last_error: string | null };
  chaos: { rate: number };
  storage_tile: { x: number; y: number };
  devices: Device[];
  samples: Sample[];
  runs: Run[];
  steps: Step[];
  interventions: Intervention[];
  results: Result[];
  tokens: Token[];
  open_intervention_count: number;
  held_result_count: number;
  // Counted over the table, not over the capped `results` page.
  result_state_counts: Record<string, number>;
  time_scale: number;
}

/**
 * Which token the UI is acting as. There is no login: picking a token from the
 * header is the point, because the same button succeeds or is refused
 * depending on who you are.
 */
// The operator by default: they carry engineer authority too, so breaking an
// instrument works on the first click. Switching to the client is what shows
// the boundary, since a customer may speak for their plate and nothing else.
let actingToken = "tok-operator";
export const getActingToken = () => actingToken;
export const setActingToken = (t: string) => {
  actingToken = t;
};

/**
 * Where the lab is. Empty when the UI is served by its own Next server, which
 * rewrites /api to the scheduler and keeps everything same-origin. The static
 * export has no server to rewrite anything, so it is built with an absolute
 * base and talks to the backend across origins.
 */
export const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "";

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      "content-type": "application/json",
      authorization: `Bearer ${actingToken}`,
      ...(init?.headers ?? {}),
    },
    cache: "no-store",
  });
  const text = await res.text();
  const body = text ? JSON.parse(text) : null;
  if (!res.ok) {
    // FastAPI wraps our typed rejections in `detail`; its own validation
    // errors put a list there instead. Fall back to the body so a caller
    // always gets something it can render rather than "422 Unprocessable
    // Entity", which tells an operator nothing.
    const detail = body?.detail ?? body;
    const problems = detail?.errors ?? (Array.isArray(detail) ? detail : null);
    const msg =
      typeof detail === "string"
        ? detail
        : detail?.reason ??
          detail?.error ??
          problems?.[0]?.message ??
          problems?.[0]?.msg ??
          `${res.status} ${res.statusText}`;
    const err = new Error(msg) as Error & {
      status?: number;
      detail?: any;
      problems?: any[];
    };
    err.status = res.status;
    err.detail = detail;
    err.problems = problems ?? [];
    throw err;
  }
  return body as T;
}

/**
 * Polls the snapshot endpoint and tracks the offset between the server's clock
 * and ours. Everything animated is drawn against `serverNow()`, so a browser
 * whose clock is minutes off still renders progress and plate movement
 * correctly, and so the UI cannot invent a state the server did not send.
 */
export function useLabState(intervalMs = 1000) {
  const [state, setState] = useState<LabState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const skew = useRef(0); // serverEpochMs - clientEpochMs

  const refresh = useCallback(async () => {
    try {
      const s = await api<LabState>("/api/state");
      skew.current = new Date(s.now).getTime() - Date.now();
      setState(s);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, intervalMs);
    return () => clearInterval(t);
  }, [refresh, intervalMs]);

  const serverNow = useCallback(() => Date.now() + skew.current, []);
  return { state, error, refresh, serverNow };
}

export const ts = (v: string | null | undefined) => (v ? new Date(v).getTime() : null);

export function fmtAgo(iso: string | null, now: number): string {
  if (!iso) return "-";
  const d = Math.max(0, (now - new Date(iso).getTime()) / 1000);
  if (d < 60) return `${d.toFixed(0)}s`;
  if (d < 3600) return `${Math.floor(d / 60)}m`;
  return `${Math.floor(d / 3600)}h`;
}

/**
 * One simulated second stands for `time_scale` seconds of lab time. The
 * scheduler ignores it; it exists so the screen shows durations at the scale a
 * lab runs at, instead of "this incubation took 10 seconds".
 */
export function fmtLab(seconds: number, scale: number): string {
  // `scale` is lab-seconds per sim-second, so the product is lab SECONDS.
  // This used to label them minutes, which put every duration on screen out by
  // 60x: a fault thirty seconds old read "held for 28 h of lab time".
  const t = seconds * scale;
  if (t < 90) return `${Math.round(t)} s`;
  if (t < 90 * 60) return `${(t / 60).toFixed(t < 600 ? 1 : 0)} min`;
  if (t < 36 * 3600) return `${(t / 3600).toFixed(1)} h`;
  return `${(t / 86400).toFixed(1)} d`;
}

/** Wall-clock age of a timestamp, expressed in lab time. */
export function fmtLabAgo(iso: string | null, now: number, scale: number): string {
  if (!iso) return "—";
  return fmtLab(Math.max(0, (now - new Date(iso).getTime()) / 1000), scale);
}

export const KIND_LABEL: Record<string, string> = {
  liquid_handler: "Liquid handler",
  bli_reader: "BLI reader",
  incubator: "Incubator",
  plate_reader: "Plate reader",
};

// Ids are the lab's own tags and stay the record everywhere they are recorded.
// On screen they are expanded, because "inc-1" is only obvious to someone who
// already knows the fleet.
const KIND_BY_PREFIX: Record<string, string> = {
  lh: "Liquid handler",
  inc: "Incubator",
  bli: "BLI reader",
  pr: "Plate reader",
};

/** "inc-1" -> "Incubator 1". Takes the kind when the caller has the device. */
export function deviceName(id: string | null | undefined, kind?: string): string {
  if (!id) return "—";
  const [prefix, n] = id.split("-");
  const base = (kind && KIND_LABEL[kind]) || KIND_BY_PREFIX[prefix];
  if (!base) return id;
  return n ? `${base} ${n}` : base;
}
