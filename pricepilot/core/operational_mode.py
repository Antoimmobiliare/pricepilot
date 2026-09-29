"""Current owner-operated mode, reversible without removing SaaS primitives."""
from __future__ import annotations

import os

OPERATIONAL_PLAN = "plus"


def operational_mode_enabled() -> bool:
    """Enable the single-owner workflow only when the deployment opts in.

    SaaS billing and plan definitions remain available for a future public launch.
    Set ``PRICEPILOT_OPERATIONAL_MODE=1`` for Luma; omit it or set it to ``0``
    to expose the future public SaaS experience.
    """
    configured = os.getenv("PRICEPILOT_OPERATIONAL_MODE", "").strip().lower()
    return configured in {"1", "true", "yes", "on"}


def operational_plan() -> str:
    """The owner workflow always uses the existing approval-capable Plus rules."""
    return OPERATIONAL_PLAN
