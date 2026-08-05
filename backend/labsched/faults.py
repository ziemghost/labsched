"""Fault taxonomy.

The organising claim: **software may decide anything it can later verify; it
must ask about anything it can only assume.**

Whether an ambiguous outcome may be retried is not a property of the fault: a
timeout on a read is harmless to repeat, a timeout on a dispense risks a double
dispense. That discriminator belongs to the operation, in
`operations.on_unknown`. So `AUTO_KINDS` lists only faults where we still know
the physical truth afterwards, and `device_timeout` is not among them.

Beyond its label an option carries which authority may choose it, whether it
can be undone, and whether an agent may take it unattended.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Authorities, mirrored from auth.tokens to avoid a circular import.
OPERATOR = "operator"          # on site; can open a door, free a gripper
ENGINEER = "engineer"          # remote; can quarantine, drain, requeue
SAMPLE_OWNER = "sample_owner"  # the customer; owns the plate and the number


class FaultKind:
    # ---- auto-recoverable: we still know the physical truth afterwards ------
    DEVICE_OFFLINE = "device_offline"      # dropped before the operation ran
    COMMS_ERROR = "comms_error"            # transient transport failure
    HEARTBEAT_LOST = "heartbeat_lost"      # no liveness signal within timeout

    # ---- ambiguous: outcome unknown, policy comes from the operation --------
    DEVICE_TIMEOUT = "device_timeout"      # accepted the job, never reported

    # ---- needs a human: the system must not guess ---------------------------
    SAMPLE_INTEGRITY_UNKNOWN = "sample_integrity_unknown"
    BATCH_DESTROYED = "batch_destroyed"
    UNEXPECTED_READING = "unexpected_reading"
    PLATE_STUCK = "plate_stuck"
    CALIBRATION_DRIFT = "calibration_drift"

    # ---- derived, never injected: raised by looking backwards ---------------
    RESULTS_SUSPECT = "results_suspect"


AUTO_KINDS = (
    FaultKind.DEVICE_OFFLINE,
    FaultKind.COMMS_ERROR,
    FaultKind.HEARTBEAT_LOST,
)

#: Outcome unknown. What happens next is decided by `operations.on_unknown`,
#: not by this list.
AMBIGUOUS_KINDS = (FaultKind.DEVICE_TIMEOUT,)

HUMAN_KINDS = (
    FaultKind.SAMPLE_INTEGRITY_UNKNOWN,
    FaultKind.BATCH_DESTROYED,
    FaultKind.UNEXPECTED_READING,
    FaultKind.PLATE_STUCK,
    FaultKind.CALIBRATION_DRIFT,
)

#: Raised by the system reasoning over its own history rather than by any
#: instrument reporting a fault. Not injectable, because nothing injects it --
#: it is what "we noticed later" looks like.
DERIVED_KINDS = (FaultKind.RESULTS_SUSPECT,)

#: What `POST /api/sim/fault` will accept.
ALL_KINDS = AUTO_KINDS + AMBIGUOUS_KINDS + HUMAN_KINDS


@dataclass(frozen=True)
class Option:
    key: str
    label: str
    consequence: str
    #: Who may choose this. Destroying a customer's plate is not an operator's
    #: call to make, even though the operator is the one standing next to it.
    authority: str = OPERATOR
    #: False means there is no undo. The UI must make these harder to click,
    #: and the compensating action is offered instead of a fake undo.
    reversible: bool = True
    #: Whether resolving demands a written justification. Irreversible by
    #: default, because that is the case where the record is worth more than
    #: the friction. Set explicitly to opt out: a question whose whole content
    #: is "you decide" gains nothing from a box the operator fills with "ok".
    demands_reason: bool | None = None
    #: True when the question is about budget or policy rather than physical
    #: judgement, so a sufficiently authorised agent may answer it itself.
    agent_resolvable: bool = False

    @property
    def requires_reason(self) -> bool:
        return (not self.reversible) if self.demands_reason is None else self.demands_reason


@dataclass(frozen=True)
class HumanFaultSpec:
    kind: str
    title: str
    message: str
    #: Stated plainly, because the commonest wrong assumption an operator makes
    #: is that the machine would have told them if something were wrong.
    could_not_observe: str
    options: tuple[Option, ...]
    hold_device: bool
    hold_sample: bool
    hold_rationale: str
    #: How long before the question is escalated. Expiry never answers it --
    #: see `escalation_policy`.
    sla_seconds: int = 120
    #: What an expiry does, which is deliberately not one of `options`: free
    #: what is not physically held, leave the held thing held, raise the
    #: question louder. In a lab the safe default is not "keep the machine
    #: busy": idling an instrument costs a bounded, recoverable amount of
    #: money, and shipping a wrong number or destroying a plate does not.
    escalation_policy: str = "park_and_hold"
    #: True when a fault on a shared instrument implicates every plate that was
    #: on it, not just the one step that noticed.
    cohort_scope: bool = False
    capabilities: tuple[str, ...] | None = None


HUMAN_FAULTS: dict[str, HumanFaultSpec] = {
    FaultKind.SAMPLE_INTEGRITY_UNKNOWN: HumanFaultSpec(
        kind=FaultKind.SAMPLE_INTEGRITY_UNKNOWN,
        title="Sample integrity unknown",
        message=(
            "Liquid handler aborted mid-transfer. Re-running risks a double "
            "dispense; continuing risks an empty well."
        ),
        could_not_observe=(
            "This handler has no tip-level liquid sensing. There is no evidence "
            "either way about whether the volume landed. The instrument is not "
            "staying silent about a problem, it is incapable of knowing."
        ),
        options=(
            Option("redo_step", "Re-run this step",
                   "Step is requeued from scratch. Safe only if you know the transfer did not land.",
                   authority=OPERATOR, reversible=True),
            Option("accept_continue", "Accept and continue",
                   "Step marked done as-is; downstream steps proceed with the plate as it stands.",
                   authority=SAMPLE_OWNER, reversible=False, demands_reason=False),
            Option("discard_abort", "Discard sample, abort run",
                   "Plate marked destroyed, run aborted, all reservations released.",
                   authority=SAMPLE_OWNER, reversible=False, demands_reason=False),
        ),
        hold_device=True,
        hold_sample=True,
        hold_rationale=(
            "The plate is physically inside the handler with an unknown liquid state. "
            "It cannot be moved by the mover until a human looks, so both the deck and "
            "the plate stay locked."
        ),
        capabilities=("liquid_transfer",),
    ),
    FaultKind.BATCH_DESTROYED: HumanFaultSpec(
        kind=FaultKind.BATCH_DESTROYED,
        title="Batch destroyed",
        message=(
            "Incubator temperature excursion. Plates held on this instrument during "
            "the excursion are cooked. This is a known-bad outcome, not an ambiguous one."
        ),
        could_not_observe=(
            "The sensor reports the chamber, not each plate. Which plates were "
            "actually past their thermal limit is inferred from what was loaded "
            "during the window, not measured per plate."
        ),
        options=(
            Option("abort_run", "Abort affected runs",
                   "Runs marked failed, plates marked destroyed, everything released.",
                   authority=SAMPLE_OWNER, reversible=False),
            Option("reprep_restart", "Re-prep samples and restart",
                   "Fresh plates are issued and every step is reset to pending.",
                   authority=SAMPLE_OWNER, reversible=False),
        ),
        hold_device=False,
        hold_sample=False,
        hold_rationale=(
            "Nothing ambiguous is pending: the plates are known destroyed and the "
            "incubator can be cleaned and reused immediately, so we release both "
            "rather than idle a working instrument."
        ),
        cohort_scope=True,
        capabilities=("incubate",),
    ),
    FaultKind.UNEXPECTED_READING: HumanFaultSpec(
        kind=FaultKind.UNEXPECTED_READING,
        title="Reading outside expected range",
        message=(
            "The instrument returned a value far outside its sane range. This is "
            "either a real result worth keeping or a broken detector."
        ),
        could_not_observe=(
            "Both hypotheses explain the observation equally well. The corroboration "
            "below narrows it, with this instrument's control history and whether "
            "sibling plates read elsewhere agree, but nothing closes it."
        ),
        options=(
            Option("accept_reading", "Accept reading as real",
                   "Result recorded and queued for release; the run continues.",
                   authority=SAMPLE_OWNER, reversible=True),
            Option("rerun_step", "Re-read on another instrument",
                   "Step requeued with this device excluded. The step's credits are "
                   "refunded and charged again, so the run costs no more than it did.",
                   authority=ENGINEER, reversible=True, agent_resolvable=True),
            Option("quarantine_device", "Quarantine the instrument",
                   "Device goes offline and drains; the step is requeued elsewhere.",
                   authority=ENGINEER, reversible=True),
        ),
        hold_device=False,
        hold_sample=True,
        hold_rationale=(
            "The read finished, so the instrument is free and we release it. The plate "
            "stays locked and parked because whether it still needs this step is exactly "
            "the open question."
        ),
        capabilities=("bli_read", "absorbance_read"),
    ),
    FaultKind.PLATE_STUCK: HumanFaultSpec(
        kind=FaultKind.PLATE_STUCK,
        title="Plate stuck in gripper",
        message=(
            "The mover reports a physical jam. The plate is neither on the deck nor "
            "in storage. Someone has to open the door."
        ),
        could_not_observe=(
            "The gripper reports position, not grip. Whether the plate is held, "
            "tilted or already dropped cannot be determined remotely."
        ),
        options=(
            Option("freed_resume", "Operator freed the plate, resume",
                   "Plate returns to storage, step requeued, device reset and released.",
                   authority=OPERATOR, reversible=True),
            Option("plate_lost", "Plate lost, abort run",
                   "Plate marked destroyed, run failed, device reset and released.",
                   authority=OPERATOR, reversible=False),
        ),
        hold_device=True,
        hold_sample=True,
        hold_rationale=(
            "A jam means the gripper is occupied and the deck is unusable. Nothing can "
            "be scheduled here until a person clears it, so both stay locked."
        ),
        sla_seconds=90,
    ),
    FaultKind.CALIBRATION_DRIFT: HumanFaultSpec(
        kind=FaultKind.CALIBRATION_DRIFT,
        title="Calibration drift suspected",
        message=(
            "Control values have moved outside tolerance. The instrument is up and "
            "producing numbers, but every result it has produced since the last good "
            "control is in question."
        ),
        could_not_observe=(
            "Drift is inferred from controls in aggregate, not reported by the "
            "instrument. There is no fault code for 'my numbers are subtly wrong', "
            "and the affected results already look normal."
        ),
        options=(
            Option("quarantine_device", "Quarantine instrument",
                   "Device offline, queued work drains, and the calibration epoch "
                   "this drift was detected in is marked suspect so its results are "
                   "re-examined.",
                   authority=ENGINEER, reversible=True),
            Option("ignore_continue", "Accept drift, keep running",
                   "Device stays in service flagged suspect; results are released with a caveat.",
                   authority=SAMPLE_OWNER, reversible=True),
        ),
        hold_device=False,
        hold_sample=False,
        hold_rationale=(
            "The step itself completed and the plate is intact. We release both so the "
            "fleet keeps moving; the decision is about the instrument's future work and "
            "its past results, not about this plate."
        ),
        cohort_scope=True,
    ),
}


HUMAN_FAULTS[FaultKind.RESULTS_SUSPECT] = HumanFaultSpec(
    kind=FaultKind.RESULTS_SUSPECT,
    title="Delivered results are in question",
    message=(
        "An instrument's calibration epoch has been marked suspect. Every result "
        "it produced during that epoch is affected, including steps already "
        "marked done and results already released."
    ),
    could_not_observe=(
        "Nothing was wrong at the time each of these ran; every step passed and "
        "every number looked normal. The problem is only visible in aggregate, "
        "after the fact, which is why it reaches backwards."
    ),
    options=(
        Option("accept_with_caveat", "Release with a caveat",
               "Results stay released and are annotated with the suspect epoch.",
               authority=SAMPLE_OWNER, reversible=True),
        Option("requeue", "Re-run the affected steps",
               "Affected results are invalidated and their steps are requeued on "
               "other instruments. Costs their credits again.",
               authority=SAMPLE_OWNER, reversible=False),
        Option("invalidate", "Invalidate and withhold",
               "Results are marked invalidated and not delivered. No re-run is scheduled.",
               authority=SAMPLE_OWNER, reversible=False),
    ),
    hold_device=False,
    hold_sample=False,
    hold_rationale=(
        "Nothing is physically pending. This question is about numbers already "
        "produced, so no instrument and no plate is held while it is answered."
    ),
    sla_seconds=600,
    cohort_scope=True,
)


def is_human(kind: str) -> bool:
    return kind in HUMAN_FAULTS


def is_ambiguous(kind: str) -> bool:
    return kind in AMBIGUOUS_KINDS


def spec(kind: str) -> HumanFaultSpec:
    return HUMAN_FAULTS[kind]


def option(kind: str, key: str) -> Option | None:
    for o in HUMAN_FAULTS[kind].options:
        if o.key == key:
            return o
    return None


def options_json(kind: str) -> list[dict]:
    return [
        {"key": o.key, "label": o.label, "consequence": o.consequence,
         "authority": o.authority, "reversible": o.reversible,
         "agent_resolvable": o.agent_resolvable}
        for o in HUMAN_FAULTS[kind].options
    ]


def valid_option(kind: str, key: str) -> bool:
    return option(kind, key) is not None


def required_authority(kind: str) -> str:
    """The weakest authority that can resolve this at all, used as the
    intervention's headline. Each option still carries its own."""
    order = {OPERATOR: 0, ENGINEER: 1, SAMPLE_OWNER: 2}
    return min((o.authority for o in HUMAN_FAULTS[kind].options),
               key=lambda a: order.get(a, 9))
