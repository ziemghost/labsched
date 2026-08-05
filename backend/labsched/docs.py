"""Print the failure taxonomy and operation catalog as markdown.

    python -m labsched.docs

Generated from the code so a table pasted anywhere else cannot quietly
disagree with what the scheduler will do on its own.
"""
from __future__ import annotations

from .catalog import DEFAULT_OPERATIONS
from .faults import AUTO_KINDS, AMBIGUOUS_KINDS, DERIVED_KINDS, HUMAN_FAULTS


def taxonomy_markdown() -> str:
    lines: list[str] = []

    lines.append("#### Handled without a human")
    lines.append("")
    lines.append("| Fault | Why it is safe to handle alone |")
    lines.append("|---|---|")
    why = {
        "device_offline": "the instrument dropped before reporting a result, so we know the step did not complete",
        "comms_error": "the transport failed, not the operation; the instrument's own state is unchanged",
        "heartbeat_lost": "liveness is a fact we own; draining its work loses nothing",
    }
    for k in AUTO_KINDS:
        lines.append(f"| `{k}` | {why.get(k, '')} |")

    lines.append("")
    lines.append("#### Outcome unknown, the operation decides")
    lines.append("")
    lines.append(
        "| Fault | Resolution |\n|---|---|\n"
        f"| `{AMBIGUOUS_KINDS[0]}` | The instrument accepted the job and stopped "
        "reporting. Whether that may be retried is a property of the operation "
        "(`operations.on_unknown`), not of the fault. |"
    )

    lines.append("")
    lines.append("#### Needs a human")
    lines.append("")
    lines.append("| Fault | What software cannot know | Holds | SLA |")
    lines.append("|---|---|---|---|")
    for k, s in HUMAN_FAULTS.items():
        holds = []
        if s.hold_device:
            holds.append("instrument")
        if s.hold_sample:
            holds.append("plate")
        derived = " *(derived)*" if k in DERIVED_KINDS else ""
        cut = s.could_not_observe.split(".")[0].strip()
        lines.append(
            f"| `{k}`{derived} | {cut}. | {', '.join(holds) or 'nothing'} | {s.sla_seconds}s |"
        )

    lines.append("")
    lines.append("#### Operation catalog (lab-owned)")
    lines.append("")
    lines.append("| Operation | Capability | Duration | Credits | On unknown outcome | Attempts |")
    lines.append("|---|---|---|---|---|---|")
    # Slice, not unpack: migration 0003 added a column and broke a nine-way
    # destructure here.
    for name, cap, dur, cost, unk, att in (op[:6] for op in DEFAULT_OPERATIONS):
        note = {"retry": "retry, physically repeatable",
                "ask": "ask a human, not repeatable",
                "fail": "fail"}[unk]
        lines.append(f"| `{name}` | `{cap}` | {dur}s | {cost} | {note} | {att} |")

    return "\n".join(lines)


if __name__ == "__main__":
    print(taxonomy_markdown())
