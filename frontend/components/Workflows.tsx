"use client";

/**
 * The protocol shelf: what the lab knows how to run, and a button that runs it.
 *
 * Submitting from here fills the parameters from the protocol's own schema
 * rather than asking, so the demo can put work on the floor in one click. The
 * form in "Submit run" is still the place to argue with admission.
 */

import { useCallback, useEffect, useState } from "react";
import { api, getActingToken } from "@/lib/api";

interface Protocol {
  name: string;
  version: number;
  digest: string;
  description: string;
  plate_bounds: [number, number];
  params: Record<string, { type: string; default?: unknown; example?: unknown; required?: boolean }>;
  source: string;
}

/** Steps come out of the YAML source; the API does not expand them. */
function stepsOf(source: string): { id: string; op: string; after: string | null }[] {
  const out: { id: string; op: string; after: string | null }[] = [];
  let cur: { id: string; op: string; after: string | null } | null = null;
  for (const line of source.split("\n")) {
    const id = line.match(/^\s*-\s*id:\s*(\S+)/);
    if (id) {
      if (cur) out.push(cur);
      cur = { id: id[1], op: "", after: null };
      continue;
    }
    if (!cur) continue;
    const op = line.match(/^\s*op:\s*(\S+)/);
    if (op) cur.op = op[1];
    const after = line.match(/^\s*after:\s*\[(.*)\]/);
    if (after) cur.after = after[1].trim();
  }
  if (cur) out.push(cur);
  return out;
}

/** Defaults first, then the schema's example: enough to be admitted. */
function paramsFor(p: Protocol): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, rule] of Object.entries(p.params)) {
    const v = rule.default ?? rule.example;
    if (v !== undefined) out[k] = v;
  }
  return out;
}

function platesFor(p: Protocol): number {
  const [lo, hi] = p.plate_bounds;
  return Math.min(Math.max(lo, 3), hi);
}

export default function Workflows({ refresh }: { refresh: () => void }) {
  const [protocols, setProtocols] = useState<Protocol[]>([]);
  const [busy, setBusy] = useState<string | null>(null);
  const [done, setDone] = useState<{ name: string; runId: string } | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      setProtocols(await api<Protocol[]>("/api/protocols"));
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function submit(p: Protocol) {
    setBusy(p.name);
    setErr(null);
    setDone(null);
    try {
      const plates = platesFor(p);
      const r = await api<{ id: string }>("/api/runs", {
        method: "POST",
        body: JSON.stringify({
          name: `${p.name} ${new Date().toLocaleTimeString()}`,
          protocol: p.name,
          plate_count: plates,
          params: paramsFor(p),
        }),
        headers: { "Idempotency-Key": `wf-${p.name}-${Date.now()}` },
      });
      setDone({ name: p.name, runId: r.id });
      refresh();
    } catch (e: any) {
      // A refusal is worth reading: it names the token that binds.
      setErr(e?.detail?.error ?? (e instanceof Error ? e.message : String(e)));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="panel h-full overflow-y-auto p-4">
      <div className="mb-3 flex items-baseline gap-3">
        <h2 className="text-sm font-semibold text-slate-100">Workflows</h2>
        <span className="text-[11px] text-slate-500">
          registered protocols, pinned by digest · submitting as{" "}
          <span className="font-mono text-violet">{getActingToken()}</span>
        </span>
      </div>

      {err && (
        <div className="mb-3 rounded border border-rose/40 bg-rose/[0.07] px-3 py-2 text-[11.5px] text-rose">
          {err}
        </div>
      )}
      {done && (
        <div className="mb-3 rounded border border-mint/40 bg-mint/[0.07] px-3 py-2 text-[11.5px] text-mint">
          submitted {done.name} · run <span className="font-mono">{done.runId}</span> — watch it on
          the factory floor
        </div>
      )}

      <div className="grid gap-3 lg:grid-cols-2">
        {protocols.map((p) => {
          const steps = stepsOf(p.source);
          const plates = platesFor(p);
          return (
            <div key={`${p.name}-${p.version}`} className="rounded border border-ink-700 bg-ink-850 p-3.5">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-[12.5px] font-medium text-slate-100">
                      {p.name}
                    </span>
                    <span className="chip border-ink-600 text-slate-500">v{p.version}</span>
                  </div>
                  <div className="mt-0.5 font-mono text-[10px] text-slate-600">{p.digest}</div>
                </div>
                <button
                  disabled={busy !== null}
                  onClick={() => submit(p)}
                  className="btn shrink-0"
                >
                  {busy === p.name ? "submitting…" : `submit ${p.name}`}
                </button>
              </div>

              <p className="mt-2 text-[11.5px] leading-relaxed text-slate-500">{p.description}</p>

              <div className="mt-3 text-[10px] font-semibold uppercase tracking-wider text-slate-600">
                Steps
              </div>
              <div className="mt-1 flex flex-wrap items-center gap-1.5">
                {steps.map((s, i) => (
                  <span key={s.id} className="flex items-center gap-1.5">
                    {i > 0 && <span className="text-slate-700">→</span>}
                    <span className="chip border-ink-600 text-slate-400">
                      {s.id} · <span className="font-mono">{s.op}</span>
                    </span>
                  </span>
                ))}
              </div>

              <div className="mt-3 text-[10px] font-semibold uppercase tracking-wider text-slate-600">
                Submits as
              </div>
              <dl className="mt-1 grid grid-cols-[110px_1fr] gap-y-1 text-[11px]">
                <dt className="text-slate-600">plates</dt>
                <dd className="font-mono text-slate-300">
                  {plates}{" "}
                  <span className="text-slate-600">
                    (accepts {p.plate_bounds[0]}–{p.plate_bounds[1]})
                  </span>
                </dd>
                {Object.entries(paramsFor(p)).map(([k, v]) => (
                  <div key={k} className="contents">
                    <dt className="text-slate-600">{k}</dt>
                    <dd className="font-mono text-slate-300">{String(v)}</dd>
                  </div>
                ))}
              </dl>
            </div>
          );
        })}
      </div>

      {protocols.length === 0 && !err && (
        <div className="text-[11.5px] text-slate-600">loading protocols…</div>
      )}
    </div>
  );
}
