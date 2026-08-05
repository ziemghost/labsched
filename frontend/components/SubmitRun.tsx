"use client";

/**
 * Submit a run.
 *
 * Without this the lab was a ninety-second demo: the seeded runs finished, the
 * floor went idle, and every retake needed a terminal.
 *
 * It makes you press "check" first. `POST /api/runs/plan` answers what a run
 * would cost and whether the token allows it with no side effects, and putting
 * that ahead of the irreversible button is the argument for building it that
 * way. A refusal is as interesting as an acceptance: it names the token that
 * binds and carries a `remedy` an agent can branch on.
 */

import { useEffect, useState } from "react";
import { api, getActingToken } from "@/lib/api";

interface ProtocolInfo {
  name: string;
  version: number;
  description: string;
  plate_bounds: [number, number];
  params: Record<string, ParamRule>;
}

interface ParamRule {
  type?: string;
  required?: boolean;
  default?: unknown;
  example?: unknown;
  min?: number;
  max?: number;
}

interface Projected {
  credits: number;
  instrument_seconds: number;
  critical_path_s: number;
  max_concurrent_steps: number;
  lab_seconds_per_second: number;
}

interface Problem {
  code: string;
  path: string | null;
  message: string;
  hint: string | null;
}

/**
 * Two shapes arrive here: our typed admission problems, and FastAPI's own
 * validation errors (`{loc, msg, type}`) when the body is malformed before it
 * ever reaches admission. Both are worth showing — the second is what you get
 * for leaving a required param blank, which is the first thing anyone does.
 */
function normalise(raw: any[] | undefined): Problem[] {
  return (raw ?? []).map((p) => ({
    code: p.code ?? p.type ?? "invalid",
    path: p.path ?? (Array.isArray(p.loc) ? p.loc.slice(1).join(".") : null),
    message: p.message ?? p.msg ?? String(p),
    hint: p.hint ?? null,
  }));
}

export default function SubmitRun({
  onClose,
  refresh,
}: {
  onClose: () => void;
  refresh: () => void;
}) {
  const [protocols, setProtocols] = useState<ProtocolInfo[]>([]);
  const [name, setName] = useState("");
  const [chosen, setChosen] = useState<string>("");
  const [plates, setPlates] = useState(2);
  const [priority, setPriority] = useState(0);
  const [params, setParams] = useState<Record<string, string>>({});

  const [plan, setPlan] = useState<{ projected: Projected; steps: unknown[] } | null>(null);
  const [problems, setProblems] = useState<Problem[]>([]);
  const [refusal, setRefusal] = useState<{ status: number; detail: any } | null>(null);
  const [busy, setBusy] = useState<"check" | "submit" | null>(null);
  const [done, setDone] = useState<string | null>(null);

  useEffect(() => {
    api<ProtocolInfo[]>("/api/protocols")
      .then((all) => {
        // The endpoint lists every version, because older ones stay
        // resolvable for runs pinned to them. New work always goes to the
        // current version, so the form offers one row per protocol.
        const latest = new Map<string, ProtocolInfo>();
        for (const p of all) {
          const seen = latest.get(p.name);
          if (!seen || p.version > seen.version) latest.set(p.name, p);
        }
        const ps = [...latest.values()];
        setProtocols(ps);
        if (ps.length && !chosen) selectProtocol(ps[0]);
      })
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const proto = protocols.find((p) => p.name === chosen);

  function selectProtocol(p: ProtocolInfo) {
    setChosen(p.name);
    // Start inside the protocol's own bounds: a protocol with a minimum of
    // three opened the form on a request it would refuse.
    setPlates(Math.min(Math.max(2, p.plate_bounds[0]), p.plate_bounds[1]));
    setParams(
      Object.fromEntries(
        Object.entries(p.params).map(([k, r]) => {
          const seed = r.default ?? r.example;
          return [k, seed !== undefined ? String(seed) : ""];
        }),
      ),
    );
    clearOutcome();
  }

  function clearOutcome() {
    setPlan(null);
    setProblems([]);
    setRefusal(null);
    setDone(null);
  }

  /** Text in, typed out: the protocol schema says which fields are numbers. */
  function coerce(): Record<string, unknown> {
    if (!proto) return {};
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(params)) {
      if (v === "") continue;
      const rule = proto.params[k];
      out[k] = rule?.type === "integer" || rule?.type === "number" ? Number(v) : v;
    }
    return out;
  }

  function body() {
    return JSON.stringify({
      name: name.trim() || `${chosen} ${new Date().toLocaleTimeString()}`,
      protocol: chosen,
      priority,
      plate_count: plates,
      params: coerce(),
    });
  }

  async function check() {
    setBusy("check");
    clearOutcome();
    try {
      const r = await api<{ ok: boolean; plan: any }>("/api/runs/plan", {
        method: "POST",
        body: body(),
      });
      setPlan(r.plan);
    } catch (e: any) {
      // A rejection is the interesting case, so it is rendered, not swallowed.
      setRefusal({ status: e.status ?? 0, detail: e.detail ?? { error: String(e) } });
      setProblems(normalise(e.problems));
    } finally {
      setBusy(null);
    }
  }

  async function submit() {
    setBusy("submit");
    try {
      const r = await api<{ id: string }>("/api/runs", {
        method: "POST",
        body: body(),
        headers: { "Idempotency-Key": `ui-${Date.now()}` },
      });
      setDone(r.id);
      setPlan(null);
      refresh();
    } catch (e: any) {
      // A rejection is the interesting case, so it is rendered, not swallowed.
      setRefusal({ status: e.status ?? 0, detail: e.detail ?? { error: String(e) } });
      setProblems(normalise(e.problems));
    } finally {
      setBusy(null);
    }
  }

  const scale = plan?.projected.lab_seconds_per_second ?? 60;
  const labMinutes = (s: number) => Math.round((s * scale) / 60);

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-ink-950/70 p-6 backdrop-blur-sm">
      <div className="panel max-h-full w-full max-w-2xl overflow-y-auto p-5">
        <div className="flex items-baseline gap-3">
          <h2 className="text-sm font-semibold text-slate-100">Submit a run</h2>
          <span className="text-[11px] text-slate-500">
            as <span className="font-mono text-violet">{getActingToken()}</span>
          </span>
          <button onClick={onClose} className="ml-auto text-xs text-slate-500 hover:text-slate-300">
            close
          </button>
        </div>

        <div className="mt-4 grid grid-cols-2 gap-3">
          <label className="flex flex-col gap-1 text-[10.5px] uppercase tracking-wider text-slate-500">
            protocol
            <select
              value={chosen}
              onChange={(e) => {
                const p = protocols.find((x) => x.name === e.target.value);
                if (p) selectProtocol(p);
              }}
              className="rounded border border-ink-600 bg-ink-850 px-2 py-1 text-xs normal-case tracking-normal text-slate-200"
            >
              {protocols.map((p) => (
                <option key={p.name} value={p.name}>
                  {p.name} · v{p.version}
                </option>
              ))}
            </select>
          </label>

          <label className="flex flex-col gap-1 text-[10.5px] uppercase tracking-wider text-slate-500">
            run name
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="optional"
              className="rounded border border-ink-600 bg-ink-850 px-2 py-1 text-xs normal-case tracking-normal text-slate-200"
            />
          </label>

          <label className="flex flex-col gap-1 text-[10.5px] uppercase tracking-wider text-slate-500">
            plates {proto ? `(${proto.plate_bounds[0]}–${proto.plate_bounds[1]})` : ""}
            <input
              type="number"
              min={proto?.plate_bounds[0] ?? 1}
              max={proto?.plate_bounds[1] ?? 8}
              value={plates}
              onChange={(e) => {
                setPlates(Number(e.target.value));
                clearOutcome();
              }}
              className="rounded border border-ink-600 bg-ink-850 px-2 py-1 text-xs text-slate-200"
            />
          </label>

          <label className="flex flex-col gap-1 text-[10.5px] uppercase tracking-wider text-slate-500">
            priority
            <input
              type="number"
              value={priority}
              onChange={(e) => {
                setPriority(Number(e.target.value));
                clearOutcome();
              }}
              className="rounded border border-ink-600 bg-ink-850 px-2 py-1 text-xs text-slate-200"
            />
          </label>

          {proto &&
            Object.entries(proto.params).map(([k, rule]) => (
              <label
                key={k}
                className="flex flex-col gap-1 text-[10.5px] uppercase tracking-wider text-slate-500"
              >
                {k}
                {rule.required ? <span className="text-amber"> ·required</span> : null}
                <input
                  value={params[k] ?? ""}
                  onChange={(e) => {
                    setParams({ ...params, [k]: e.target.value });
                    clearOutcome();
                  }}
                  placeholder={rule.type ?? ""}
                  className="rounded border border-ink-600 bg-ink-850 px-2 py-1 text-xs normal-case tracking-normal text-slate-200"
                />
              </label>
            ))}
        </div>

        {proto?.description && (
          <p className="mt-3 text-[11px] leading-relaxed text-slate-500">{proto.description}</p>
        )}

        <div className="mt-4 flex items-center gap-2">
          <button onClick={check} disabled={busy !== null} className="btn">
            {busy === "check" ? "checking…" : "check admission"}
          </button>
          <button
            onClick={submit}
            disabled={busy !== null || plan === null}
            className="btn-primary disabled:opacity-40"
            title={plan ? "" : "check admission first — it costs nothing and answers the same question"}
          >
            {busy === "submit" ? "submitting…" : "submit run"}
          </button>
          {done && (
            <span className="font-mono text-[11px] text-mint">
              admitted · {done}
            </span>
          )}
        </div>

        {plan && (
          <div className="mt-4 rounded border border-mint/30 bg-mint/5 p-3">
            <p className="text-[11px] font-semibold text-mint">
              Would be admitted. Nothing has been reserved.
            </p>
            <div className="mt-2 grid grid-cols-4 gap-3 font-mono text-[11px] text-slate-300">
              <Stat label="credits" value={plan.projected.credits} />
              <Stat label="steps" value={plan.steps.length} />
              <Stat
                label="critical path"
                value={`${labMinutes(plan.projected.critical_path_s)} lab-min`}
              />
              <Stat label="max parallel" value={plan.projected.max_concurrent_steps} />
            </div>
          </div>
        )}

        {refusal && (
          <div className="mt-4 rounded border border-rose/40 bg-rose/10 p-3">
            <p className="text-[11px] font-semibold text-rose">
              {/* `AdmissionError.body()` emits `errors` and `remedy`, not
                  `error`/`reason`, so every admission refusal used to
                  headline as the bare word "refused" above a perfectly good
                  list of typed problems. */}
              {refusal.status} ·{" "}
              {refusal.detail?.error ??
                refusal.detail?.reason ??
                problems[0]?.message ??
                refusal.detail?.remedy ??
                "refused"}
            </p>
            {refusal.detail?.remedy && (
              <p className="mt-1 font-mono text-[10.5px] text-slate-400">
                remedy: {refusal.detail.remedy}
              </p>
            )}
            {problems.length > 0 && (
              <ul className="mt-2 space-y-1.5">
                {problems.map((p, i) => (
                  <li key={i} className="text-[11px] leading-relaxed text-slate-300">
                    <span className="font-mono text-slate-500">{p.path ?? p.code}</span> —{" "}
                    {p.message}
                    {p.hint && <span className="text-slate-500"> ({p.hint})</span>}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <div>
      <div className="text-[9.5px] uppercase tracking-wider text-slate-600">{label}</div>
      <div className="text-slate-200">{value}</div>
    </div>
  );
}
