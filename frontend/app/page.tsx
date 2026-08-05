"use client";

import { useState } from "react";
import Audit from "@/components/Audit";
import FactoryFloor from "@/components/FactoryFloor";
import InterventionInbox from "@/components/InterventionInbox";
import Results from "@/components/Results";
import SubmitRun from "@/components/SubmitRun";
import Timeline from "@/components/Timeline";
import Tokens from "@/components/Tokens";
import Workflows from "@/components/Workflows";
import { api, getActingToken, setActingToken, useLabState } from "@/lib/api";

const TABS = [
  "Factory floor",
  "Workflows",
  "Interventions",
  "Results",
  "Timeline",
  "Tokens",
  "Audit",
] as const;
type Tab = (typeof TABS)[number];

export default function Page() {
  const { state, error, refresh, serverNow } = useLabState(1000);
  const [tab, setTab] = useState<Tab>("Factory floor");
  const [acting, setActing] = useState(getActingToken());
  const [submitting, setSubmitting] = useState(false);
  const [reseeding, setReseeding] = useState(false);

  // The demo is a shared world: anyone can break an instrument, quarantine it,
  // or leave a question open. This is the way back to a known state without
  // shell access to the box.
  async function reseed() {
    if (!confirm("Reseed the lab? Every run, question and result is discarded.")) return;
    setReseeding(true);
    try {
      await api("/api/sim/reseed", { method: "POST" });
      refresh();
    } catch (e) {
      alert(e instanceof Error ? e.message : String(e));
    } finally {
      setReseeding(false);
    }
  }

  return (
    <main className="flex h-screen flex-col gap-3 p-3">
      <header className="flex items-center gap-4">
        <div className="flex items-baseline gap-2.5">
          <h1 className="font-mono text-sm font-semibold tracking-tight text-slate-100">
            labsched
          </h1>
          <span className="text-[11px] text-slate-600">
            instrument scheduler · every reservation is an authorization decision
          </span>
        </div>

        <nav className="ml-4 flex gap-1">
          {TABS.map((t) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className={`relative rounded px-2.5 py-1 text-xs transition ${
                tab === t
                  ? "bg-ink-800 text-slate-100"
                  : "text-slate-500 hover:bg-ink-850 hover:text-slate-300"
              }`}
            >
              {t}
              {t === "Interventions" && (state?.open_intervention_count ?? 0) > 0 && (
                <span className="ml-1.5 rounded-full bg-amber px-1.5 py-px text-[10px] font-bold text-ink-950">
                  {state!.open_intervention_count}
                </span>
              )}
              {t === "Results" && (state?.held_result_count ?? 0) > 0 && (
                <span className="ml-1.5 rounded-full bg-sky px-1.5 py-px text-[10px] font-bold text-ink-950">
                  {state!.held_result_count}
                </span>
              )}
            </button>
          ))}
        </nav>

        {/* The lab needs a way to be given work, or the demo is over as soon
            as the seeded runs finish. */}
        <button onClick={() => setSubmitting(true)} className="btn ml-auto">
          Submit run
        </button>

        <button
          onClick={reseed}
          disabled={reseeding}
          className="btn"
          title="Discard every run, question and result, and rebuild the seeded lab"
        >
          {reseeding ? "reseeding…" : "Reseed backend"}
        </button>

        {/* No login: choosing who you are IS the demo, because the same button
            succeeds or is refused depending on the token's authority. */}
        <label className="flex items-center gap-1.5 text-[10.5px] text-slate-500">
          acting as
          <select
            value={acting}
            onChange={(e) => {
              setActingToken(e.target.value);
              setActing(e.target.value);
            }}
            className="rounded border border-violet/30 bg-ink-850 px-2 py-1 font-mono text-[11px] text-violet"
          >
            {/* The root is the lab's own signing key, not somebody you act
                as, so it is not offered here. The Tokens tab still shows it. */}
            {(state?.tokens ?? [])
              .filter((t) => t.tier !== "org" && !t.revoked)
              .map((t) => (
                <option key={t.id} value={t.id}>
                  {t.label}
                  {t.authorities.length ? ` · ${t.authorities.join("+")}` : " · no authority"}
                </option>
              ))}
          </select>
        </label>

        <div className="flex items-center gap-3 font-mono text-[10.5px] text-slate-600">
          {error ? (
            <span className="text-rose">backend unreachable — {error}</span>
          ) : state ? (
            <>
              <span>{state.devices.length} instruments</span>
              <span>{state.runs.filter((r) => r.state === "running").length} running</span>
              <span className="flex items-center gap-1">
                <span className="h-1.5 w-1.5 rounded-full bg-mint" />
                live
              </span>
            </>
          ) : (
            <span>connecting…</span>
          )}
        </div>
      </header>

      {submitting && (
        <SubmitRun onClose={() => setSubmitting(false)} refresh={refresh} />
      )}

      <div className="min-h-0 flex-1">
        {!state ? (
          <div className="panel flex h-full items-center justify-center text-sm text-slate-600">
            {error ? `Cannot reach the API: ${error}` : "Loading lab state…"}
          </div>
        ) : tab === "Factory floor" ? (
          <FactoryFloor state={state} refresh={refresh} serverNow={serverNow} />
        ) : tab === "Workflows" ? (
          <Workflows refresh={refresh} />
        ) : tab === "Interventions" ? (
          <InterventionInbox state={state} refresh={refresh} serverNow={serverNow} />
        ) : tab === "Results" ? (
          <Results state={state} refresh={refresh} />
        ) : tab === "Timeline" ? (
          <Timeline state={state} serverNow={serverNow} />
        ) : tab === "Tokens" ? (
          <Tokens state={state} refresh={refresh} />
        ) : (
          <Audit state={state} />
        )}
      </div>
    </main>
  );
}
