"use client";

import { useState } from "react";
import { LabState, Token, api } from "@/lib/api";

export default function Tokens({
  state,
  refresh,
}: {
  state: LabState;
  refresh: () => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const roots = state.tokens.filter((t) => !t.parent_id);
  const childrenOf = (id: string) => state.tokens.filter((t) => t.parent_id === id);

  const revoke = async (id: string) => {
    setBusy(id);
    setErr(null);
    try {
      const res = await api<{ revoked: string[] }>(`/api/tokens/${id}/revoke`, {
        method: "POST",
      });
      setErr(`revoked ${res.revoked.length} token(s): ${res.revoked.join(", ")}`);
      refresh();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  const render = (t: Token, depth: number): React.ReactNode => (
    <div key={t.id}>
      <TokenCard
        token={t}
        depth={depth}
        busy={busy === t.id}
        onRevoke={() => revoke(t.id)}
        runs={state.runs.filter((r) => r.token_id === t.id).length}
      />
      {childrenOf(t.id).map((c) => render(c, depth + 1))}
    </div>
  );

  return (
    <div className="panel h-full overflow-y-auto">
      <div className="border-b border-line px-3 py-2">
        <span className="text-[11px] font-semibold uppercase tracking-wider text-slate-400">
          Capability tokens — each child is strictly weaker than its parent
        </span>
        <p className="mt-0.5 text-[10.5px] text-slate-600">
          Attenuation is enforced by the token format, not by this UI. Revoking a token
          kills every token beneath it, and live runs under it drain rather than abort.
        </p>
      </div>
      {err && (
        <p className="border-b border-line bg-ink-850 px-3 py-2 font-mono text-[11px] text-slate-400">
          {err}
        </p>
      )}
      <div className="p-3">{roots.map((t) => render(t, 0))}</div>
    </div>
  );
}

function TokenCard({
  token,
  depth,
  busy,
  onRevoke,
  runs,
}: {
  token: Token;
  depth: number;
  busy: boolean;
  onRevoke: () => void;
  runs: number;
}) {
  const spentPct = token.budget_credits
    ? (token.credits_spent / token.budget_credits) * 100
    : 0;
  const expired = new Date(token.expires_at).getTime() < Date.now();

  return (
    <div style={{ marginLeft: depth * 26 }} className="relative mb-2">
      {depth > 0 && (
        <span className="absolute -left-[14px] top-6 h-px w-3 bg-ink-600" aria-hidden />
      )}
      <div
        className={`rounded-lg border px-3 py-2.5 ${
          token.revoked
            ? "border-rose/30 bg-rose/[0.05]"
            : "border-line bg-ink-850"
        }`}
      >
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <span className="chip border-violet/30 text-violet">{token.tier}</span>
              <span className="text-sm font-medium text-slate-100">{token.label}</span>
              {token.revoked && <span className="chip border-rose/40 text-rose">revoked</span>}
              {expired && !token.revoked && (
                <span className="chip border-amber/40 text-amber">expired</span>
              )}
            </div>
            <div className="mt-0.5 font-mono text-[10.5px] text-slate-600">{token.id}</div>
          </div>
          <button
            disabled={busy || token.revoked}
            onClick={onRevoke}
            className="btn-danger shrink-0"
          >
            {busy ? "…" : token.revoked ? "revoked" : "Revoke"}
          </button>
        </div>

        <div className="mt-2.5 grid grid-cols-2 gap-x-6 gap-y-1 text-[11px] sm:grid-cols-3">
          <Field label="device kinds" value={token.allowed_kinds.join(", ") || "—"} />
          <Field
            label="may act as"
            value={token.authorities.join(", ") || "nothing — schedules only"}
          />
          <Field label="max concurrent" value={String(token.max_concurrent)} />
          <Field label="max wall-clock" value={`${token.max_wallclock_s}s`} />
          <Field label="max credits / run" value={String(token.max_run_credits)} />
          <Field label="expires" value={new Date(token.expires_at).toLocaleDateString()} />
          <Field label="runs issued" value={String(runs)} />
        </div>

        <div className="mt-2">
          <div className="mb-1 flex justify-between text-[10px] text-slate-500">
            <span>budget</span>
            <span className="font-mono">
              {token.credits_spent} / {token.budget_credits} credits
            </span>
          </div>
          <div className="h-1.5 overflow-hidden rounded-full bg-ink-700">
            <div
              className={`h-full rounded-full ${
                spentPct > 85 ? "bg-rose" : spentPct > 60 ? "bg-amber" : "bg-sky"
              }`}
              style={{ width: `${Math.min(100, spentPct)}%` }}
            />
          </div>
        </div>

        {token.revoked && (
          <p className="mt-2 text-[10.5px] text-rose/80">
            revoked by {token.revoked_by} — every token beneath this one is dead too, and
            live runs under it drain rather than abort
          </p>
        )}
      </div>
    </div>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <span className="text-slate-600">{label}: </span>
      <span className="font-mono text-slate-300">{value}</span>
    </div>
  );
}
