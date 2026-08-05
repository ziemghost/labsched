"""Runtime configuration. Everything overridable by env var."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(REPO_ROOT / ".env")


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


@dataclass(frozen=True)
class Settings:
    database_url: str = field(
        default_factory=lambda: os.environ.get("DATABASE_URL", "postgresql://localhost/labsched")
    )
    # Private key for minting biscuits. Generated on first boot if absent and
    # persisted to sim_config, so tokens survive restarts.
    root_key_hex: str | None = field(default_factory=lambda: os.environ.get("LABSCHED_ROOT_KEY"))

    # Scheduler
    tick_interval_s: float = field(default_factory=lambda: _float("TICK_INTERVAL_S", 0.5))
    heartbeat_interval_s: float = field(default_factory=lambda: _float("HEARTBEAT_INTERVAL_S", 1.0))
    heartbeat_timeout_s: int = field(default_factory=lambda: _int("HEARTBEAT_TIMEOUT_S", 6))
    # Grace on top of a step's declared duration before we call it a timeout.
    step_timeout_grace_s: int = field(default_factory=lambda: _int("STEP_TIMEOUT_GRACE_S", 8))
    # How long a plate takes to move between tiles.
    transit_s: int = field(default_factory=lambda: _int("TRANSIT_S", 3))
    # Grace before the loop re-runs QC over a result still in `pending_qc`. QC
    # runs outside the commit so a QC failure cannot roll back a step the robot
    # finished, which means something has to notice when it never ran.
    qc_sweep_grace_s: int = field(default_factory=lambda: _int("QC_SWEEP_GRACE_S", 15))
    retry_backoff_base_s: float = field(default_factory=lambda: _float("RETRY_BACKOFF_BASE_S", 1.5))

    # One simulated second stands for this many seconds of lab time. The
    # scheduler ignores it; it exists so the UI can show plausible durations
    # instead of "this incubation took 10s".
    time_scale: int = field(default_factory=lambda: _int("TIME_SCALE", 60))

    @property
    def dsn(self) -> str:
        return self.database_url


settings = Settings()
