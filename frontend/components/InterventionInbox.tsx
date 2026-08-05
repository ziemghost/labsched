"use client";

/**
 * The human review queue.
 *
 * The layout follows the order a decision actually needs: what the machine
 * observed, then *what it could not observe*, then the physical situation,
 * then corroboration, then the options with their computed blast radius. The
 * second and fourth blocks are the ones everyone skips and the ones that stop
 * a wrong call being made.
 */

import { useCallback, useEffect, useState } from "react";
import {
  Consequence,
  Intervention,
  LabState,
  api,
  deviceName,
  fmtLabAgo,
  getActingToken,
} from "@/lib/api";

export default function InterventionInbox({
  state,
  refresh,
  serverNow,
}: {
  state: LabState;
  refresh: () => void;
  serverNow: () => number;
}) {
  const open = state.interventions.filter((i) => i.state === "open");
  const resolved = state.interventions.filter((i) => i.state === "resolved");
  const [selected, setSelected] = useState<string | null>(null);
  const active = open.find((i) => i.id === selected) ?? open[0] ?? null;
  const now = serverNow();

  return (
    <div className="grid h-full grid-cols-[360px_1fr] gap-3">
      <div className="panel flex flex-col overflow-hidden">
        <div className="flex items-center justify-between border-b border-line px-3 py-2">
          <span className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">
            Awaiting a human
          </span>
          <span
            className={`rounded-full px-2 py-0.5 text-[10px] font-bold ${
              open.length ? "bg-amber text-ink-950" : "bg-ink-700 text-slate-500"
            }`}
          >
            {open.length}
          </span>
        </div>
        <div className="flex-1 overflow-y-auto">
          {open.length === 0 && (
            <p className="px-3 py-10 text-center text-xs text-slate-600">
              No decisions pending.
            </p>
          )}
          {open.map((iv) => {
            const overdue = iv.expires_at && new Date(iv.expires_at).getTime() < now;
            return (
              <button
                key={iv.id}
                onClick={() => setSelected(iv.id)}
                className={`block w-full border-b border-line px-3 py-2.5 text-left transition ${
                  active?.id === iv.id ? "bg-amber/[0.08]" : "hover:bg-ink-850"
                }`}
              >
                <div className="flex items-baseline justify-between gap-2">
                  <span className="text-xs font-medium text-slate-100">
                    {iv.kind.replace(/_/g, " ")}
                  </span>
                  <span
                    className={`shrink-0 font-mono text-[10px] ${
                      overdue ? "text-rose" : "text-slate-600"
                    }`}
                  >
                    {fmtLabAgo(iv.created_at, now, state.time_scale)}
                    {overdue ? " · overdue" : ""}
                  </span>
                </div>
                <div className="mt-0.5 text-[10.5px] text-slate-500">
                  {deviceName(iv.device_id)} ·{" "}
                  <span className="font-mono">{iv.run_name ?? iv.run_id}</span>
                </div>
                <div className="mt-1 flex flex-wrap items-center gap-1">
                  <span className="chip border-violet/30 text-violet">
                    needs {iv.required_authority.replace(/_/g, " ")}
                  </span>
                  {iv.affected_run_ids.length > 1 && (
                    <span className="chip border-rose/30 text-rose">
                      {iv.affected_run_ids.length} runs
                    </span>
                  )}
                  {iv.acknowledged_by && (
                    <span className="chip border-sky/30 text-sky">
                      {iv.acknowledged_by} is on it
                    </span>
                  )}
                </div>
              </button>
            );
          })}

          {resolved.length > 0 && (
            <div className="px-3 pb-2 pt-4 text-[10px] font-semibold uppercase tracking-wider text-slate-600">
              Recently decided
            </div>
          )}
          {resolved.slice(0, 12).map((iv) => (
            <div key={iv.id} className="border-b border-line/60 px-3 py-2 opacity-70">
              <div className="text-[11px] text-slate-400">{iv.kind.replace(/_/g, " ")}</div>
              <div className="font-mono text-[10px] text-slate-600">
                {iv.resolution} — {iv.resolved_by_token ?? iv.resolved_by}
              </div>
            </div>
          ))}
        </div>
      </div>

      <div className="panel overflow-y-auto">
        {active ? (
          <Detail
            key={active.id}
            intervention={active}
            refresh={refresh}
            now={now}
            scale={state.time_scale}
          />
        ) : (
          <div className="flex h-full items-center justify-center text-sm text-slate-600">
            Nothing is waiting on you.
          </div>
        )}
      </div>
    </div>
  );
}

function Detail({
  intervention,
  refresh,
  now,
  scale,
}: {
  intervention: Intervention;
  refresh: () => void;
  now: number;
  scale: number;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [reason, setReason] = useState("");
  const [confirming, setConfirming] = useState<string | null>(null);
  const [costs, setCosts] = useState<Record<string, Consequence>>({});

  const loadCosts = useCallback(async () => {
    try {
      const res = await api<{ options: Consequence[] }>(
        `/api/interventions/${intervention.id}/consequences`
      );
      setCosts(Object.fromEntries(res.options.map((o) => [o.option, o])));
    } catch {
      /* the panel still works without the numbers */
    }
  }, [intervention.id]);

  useEffect(() => {
    loadCosts();
  }, [loadCosts]);

  /** The consequences call is the authority, but it can fail; the option
   *  carries the same flag so the box is never quietly unenforced. */
  const reasonRequired = (o: { key: string; requires_reason?: boolean; reversible: boolean }) =>
    costs[o.key]?.requires_reason ?? o.requires_reason ?? !o.reversible;

  const pick = async (option: string) => {
    const c = costs[option];
    const opt = intervention.options.find((o) => o.key === option);
    if (opt && reasonRequired(opt) && !reason.trim()) {
      setErr("This cannot be undone. Write down why before applying it.");
      return;
    }
    setBusy(option);
    setErr(null);
    try {
      await api(`/api/interventions/${intervention.id}/resolve`, {
        method: "POST",
        body: JSON.stringify({
          option,
          reason: reason.trim() || null,
          expected_version: intervention.version,
        }),
      });
      setReason("");
      setConfirming(null);
      refresh();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  const needsReason = intervention.options.some(reasonRequired);

  const detail = (intervention.detail ?? {}) as Record<string, any>;
  const result = detail.result as Record<string, unknown> | undefined;
  const corro = detail.corroboration as Record<string, any> | undefined;
  const expires = intervention.expires_at ? new Date(intervention.expires_at).getTime() : null;
  const overdue = expires !== null && expires < now;
  // What an escalation will NOT touch, named the way the operator sees it.
  const escalations = Number(detail.escalations ?? 0);
  const held = [
    intervention.holds.device ? "instrument" : null,
    intervention.holds.sample ? "plate" : null,
  ].filter(Boolean) as string[];

  return (
    <div className="p-5">
      <div className="flex items-start gap-3">
        <span className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-amber text-base font-bold text-ink-950">
          !
        </span>
        <div className="min-w-0 flex-1">
          <div className="flex items-center justify-between gap-3">
            <h2 className="text-base font-semibold text-slate-100">
              {intervention.kind.replace(/_/g, " ")}
            </h2>
            <span className="chip shrink-0 border-violet/30 text-violet">
              needs {intervention.required_authority.replace(/_/g, " ")}
            </span>
          </div>
          <p className="mt-1 max-w-2xl text-xs leading-relaxed text-slate-400">
            {intervention.message}
          </p>
        </div>
      </div>

      {/* 1. what the machine observed */}
      {result && (
        <Block title="What the instrument reported">
          <pre className="overflow-x-auto font-mono text-[11px] text-slate-300">
            {JSON.stringify(result, null, 2)}
          </pre>
        </Block>
      )}
      {detail.detected_by === "control chart" && (
        <Block title="What the instrument reported">
          {/* Two checks, and they are not interchangeable. The absolute one
              compares against the operation's declared control target and is
              the only one that can catch an instrument that was already
              drifting the first time it was used; the rolling-median one
              catches a slow walk that has not yet left the absolute band.
              Naming the wrong one — or printing "a rolling median of NaN"
              because the absolute check sets none — throws away the whole
              distinction. */}
          <p className="text-[11.5px] text-slate-300">
            Control well{" "}
            <span className="font-mono text-slate-100">{String(detail.control_value)}</span>{" "}
            {detail.check === "rolling_median" ? (
              <>
                against a rolling median of{" "}
                <span className="font-mono text-slate-100">
                  {Number(detail.rolling_median).toFixed(3)}
                </span>{" "}
              </>
            ) : (
              <>
                against an expected{" "}
                <span className="font-mono text-slate-100">
                  {Number(detail.control_target).toFixed(3)}
                </span>{" "}
              </>
            )}
            — a deviation of{" "}
            <span className="font-mono text-amber">
              {(Number(detail.deviation) * 100).toFixed(0)}%
            </span>{" "}
            against a tolerance of {(Number(detail.tolerance) * 100).toFixed(0)}%.
          </p>
          <p className="mt-1.5 text-[10.5px] text-slate-500">
            The instrument raised no fault.{" "}
            {detail.check === "rolling_median"
              ? "This was derived by comparing it against its own recent history."
              : "This was derived by comparing it against the value this operation's control well is supposed to read — the check that still fires when an instrument was already drifting the first time we used it."}
          </p>
        </Block>
      )}

      {/* 2. what it could NOT observe, the block that prevents wrong calls */}
      {detail.could_not_observe && (
        <Block title="What the instrument could not observe" tone="warn">
          <p className="text-[11.5px] leading-relaxed text-amber/90">
            {String(detail.could_not_observe)}
          </p>
        </Block>
      )}

      {/* 3. physical state right now */}
      <Block title="Physical state">
        <dl className="grid grid-cols-[130px_1fr] gap-y-1.5 text-[11.5px]">
          {[
            [
              "Instrument",
              intervention.device_id
                ? `${deviceName(intervention.device_id, intervention.device_kind ?? undefined)} · ${intervention.device_id}`
                : "—",
            ],
            [
              "Step",
              `${intervention.step_name ?? "—"} · ${
                intervention.capability ?? ""
              }`,
            ],
            ["Run", intervention.run_name ?? intervention.run_id],
            ["Plate", intervention.sample_label ?? intervention.sample_id ?? "—"],
            [held.length ? "Held for" : "Open for",
             `${fmtLabAgo(intervention.created_at, now, scale)} of lab time`],
          ].map(([k, v]) => (
            <div key={k} className="contents">
              <dt className="text-slate-500">{k}</dt>
              <dd className="font-mono text-slate-300">{v}</dd>
            </div>
          ))}
        </dl>
        <div className="mt-2.5 flex gap-6 border-t border-line pt-2 text-[11.5px]">
          <span>
            <span className="text-slate-500">Instrument: </span>
            <span className={intervention.holds.device ? "text-amber" : "text-mint"}>
              {intervention.holds.device ? "locked" : "released"}
            </span>
          </span>
          <span>
            <span className="text-slate-500">Plate: </span>
            <span className={intervention.holds.sample ? "text-amber" : "text-mint"}>
              {intervention.holds.sample ? "locked" : "released"}
            </span>
          </span>
        </div>
        <p className="mt-2 text-[11px] leading-relaxed text-slate-500">
          {intervention.holds.rationale}
        </p>
      </Block>

      {/* 4. corroboration, which turns a guess into an inference */}
      {corro && (
        <Block title="Corroboration">
          <ul className="space-y-1 text-[11.5px] text-slate-300">
            <li>
              <span className="text-slate-500">This instrument raised this fault </span>
              <span className="font-mono text-slate-100">
                {corro.same_fault_on_this_device_24h}×
              </span>
              <span className="text-slate-500"> in the last 24h.</span>
            </li>
            {corro.control_median != null && (
              <li>
                <span className="text-slate-500">Its recent control median is </span>
                <span className="font-mono text-slate-100">
                  {Number(corro.control_median).toFixed(3)}
                </span>
                <span className="text-slate-500">
                  {" "}
                  over {(corro.recent_control_values ?? []).length} reads.
                </span>
              </li>
            )}
            {corro.previous_resolutions &&
              Object.keys(corro.previous_resolutions).length > 0 && (
                <li className="text-amber/80">
                  Previously resolved here as{" "}
                  {Object.entries(corro.previous_resolutions)
                    .map(([k, v]) => `${k} ×${v}`)
                    .join(", ")}
                  . Consider whether that is still the right call.
                </li>
              )}
          </ul>
        </Block>
      )}

      {(intervention.affected_run_ids.length > 1 ||
        intervention.affected_sample_ids.length > 1) && (
        <Block title="Blast radius" tone="warn">
          <p className="text-[11.5px] text-slate-300">
            This affects{" "}
            <span className="font-mono text-rose">{intervention.affected_run_ids.length}</span>{" "}
            runs and{" "}
            <span className="font-mono text-rose">
              {intervention.affected_sample_ids.length}
            </span>{" "}
            plates. One decision applies to all of them.
          </p>
        </Block>
      )}

      {/* 6. what happens if you do nothing */}
      <Block title="If you do nothing" tone={overdue ? "warn" : undefined}>
        <p className="text-[11.5px] leading-relaxed text-slate-400">
          {overdue ? (
            <>
              This passed its SLA and has been escalated
              {escalations > 1 ? ` ${escalations} times` : ""}.{" "}
              {held.length > 0 ? (
                <>
                  The {held.join(" and ")} {held.length > 1 ? "are" : "is"} still held —
                  nothing moved on a timer.{" "}
                </>
              ) : (
                // Not "everything": a plate a live step still holds is left
                // exactly where it is, whatever this question's kind declares
                // about holding plates. The escalation audit lists those as
                // `samples_in_use`.
                <>
                  Idle plates have been parked; any a running step still holds
                  were left in place.{" "}
                </>
              )}
              <span className="text-slate-200">The question was not answered</span> and will
              not be answered automatically.
            </>
          ) : (
            <>
              At{" "}
              <span className="font-mono text-slate-200">
                {expires ? new Date(expires).toLocaleTimeString() : "—"}
              </span>{" "}
              this escalates under{" "}
              <span className="font-mono text-slate-200">
                {intervention.escalation_policy}
              </span>
              : park anything this question does not physically hold,
              {held.length > 0 ? (
                <> leave the {held.join(" and ")} exactly where {held.length > 1 ? "they are" : "it is"},</>
              ) : null}{" "}
              leave the question open. Never accept, never discard.
            </>
          )}
        </p>
      </Block>

      {err && (
        <p className="mt-4 max-w-2xl rounded border border-rose/40 bg-rose/10 px-3 py-2 text-[11.5px] text-rose">
          {err}
        </p>
      )}

      {/* 5. options with computed consequences */}
      <div className="mt-5 max-w-2xl">
        <div className="mb-2 text-[10px] font-semibold uppercase tracking-wider text-slate-500">
          Decide — acting as{" "}
          <span className="font-mono text-violet">{getActingToken()}</span>
        </div>

        <input
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          placeholder={
            needsReason
              ? "reason (required by one of the options below)"
              : "reason (optional, recorded with the decision)"
          }
          className="mb-2 w-full rounded border border-ink-600 bg-ink-850 px-2.5 py-1.5 text-[11.5px] text-slate-200 placeholder:text-slate-600"
        />

        <div className="space-y-2">
          {intervention.options.map((o) => {
            const c = costs[o.key];
            const irreversible = c ? !c.reversible : !o.reversible;
            const isConfirming = confirming === o.key;
            return (
              <div
                key={o.key}
                className={`rounded border px-3.5 py-2.5 transition ${
                  irreversible ? "border-rose/30 bg-rose/[0.04]" : "border-ink-600 bg-ink-850"
                }`}
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="text-xs font-medium text-slate-100">{o.label}</span>
                      {irreversible && (
                        <span className="chip border-rose/40 text-rose">no undo</span>
                      )}
                      {reasonRequired(o) && (
                        <span className="chip border-amber/40 text-amber">reason required</span>
                      )}
                      {o.agent_resolvable && (
                        <span className="chip border-sky/30 text-sky">agent may</span>
                      )}
                      <span className="chip border-ink-600 text-slate-500">
                        {o.authority.replace(/_/g, " ")}
                      </span>
                    </div>
                    <div className="mt-0.5 text-[11px] leading-relaxed text-slate-500">
                      {o.consequence}
                    </div>
                    {c && <Counts c={c} />}
                  </div>
                  <button
                    disabled={busy !== null}
                    onClick={() =>
                      irreversible && !isConfirming ? setConfirming(o.key) : pick(o.key)
                    }
                    className={`shrink-0 ${irreversible ? "btn-danger" : "btn"}`}
                  >
                    {busy === o.key ? "applying…" : isConfirming ? "confirm" : "apply"}
                  </button>
                </div>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function Counts({ c }: { c: Consequence }) {
  const items: [string, boolean][] = [];
  if (c.runs_aborted) items.push([`${c.runs_aborted} run(s) aborted`, true]);
  if (c.plates_destroyed) items.push([`${c.plates_destroyed} plate(s) destroyed`, true]);
  if (c.instruments_quarantined) items.push(["instrument quarantined", false]);
  if (c.steps_requeued)
    items.push([
      `${c.steps_requeued} step${c.steps_requeued === 1 ? "" : "s"} re-run`,
      false,
    ]);
  if (c.credits_spent_again) items.push([`${c.credits_spent_again} credits again`, false]);
  if (c.credits_released) items.push([`${c.credits_released} credits released`, false]);
  if (c.other_runs_affected)
    items.push([
      `${c.other_runs_affected} other run${c.other_runs_affected === 1 ? "" : "s"} queued here`,
      true,
    ]);
  if (items.length === 0) return null;

  return (
    <div className="mt-1.5 flex flex-wrap gap-1.5">
      {items.map(([label, severe]) => (
        <span
          key={label}
          className={`chip ${severe ? "border-rose/30 text-rose" : "border-ink-600 text-slate-400"}`}
        >
          {label}
        </span>
      ))}
    </div>
  );
}

function Block({
  title,
  children,
  tone,
}: {
  title: string;
  children: React.ReactNode;
  tone?: "warn";
}) {
  return (
    <div
      className={`mt-4 max-w-2xl rounded border p-3 ${
        tone === "warn" ? "border-amber/30 bg-amber/[0.05]" : "border-line bg-ink-850"
      }`}
    >
      <div className="mb-1.5 text-[10px] font-semibold uppercase tracking-wider text-slate-500">
        {title}
      </div>
      {children}
    </div>
  );
}
