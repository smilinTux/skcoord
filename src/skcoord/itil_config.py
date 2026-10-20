"""
ITIL configuration settings.

Central place for ITIL behavior toggles that can be changed without code
modifications. This supports the requirement that re-enabling CAB later
must be a single documented setting, not a rebuild.

See card 4655a851 (ITIL-CAB-RETIRE-01) for the retirement rationale.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# CAB (Change Advisory Board) configuration
# ---------------------------------------------------------------------------

# Default: CAB is retired (False) as of 2026-08-29 per card 4655a851.
# The CAB vote requirement no longer gates change execution.
# The three controls that actually caught problems are now hard conditions:
# 1. Fail-closed preflight (custody/safety checks before mutation)
# 2. Stated rollback plan (required for destructive/high-risk changes)
# 3. Chef explicit approval (operator authorization)
#
# Re-enabling CAB is a single boolean switch below. When True:
# - CAB votes are read from cab-decisions/ and folded into change status
# - Approval still requires the three hard conditions above
# - The vote adds a second pair of eyes, not a replacement for the checks
#
# What CAB adds beyond preflight when enabled:
# - Formal approval record with conditions from multiple agents
# - Historical audit trail of who approved what and why
# - Multi-agent coordination (useful if the estate grows beyond one operator)
# - Explicit rejection path that blocks without needing operator intervention
#
# Tradeoff: In a five-node private estate with one operator and one agent,
# a CAB is a single agent voting on its own change (ceremonial, noise).
# The vote cannot fail, which teaches everyone that gates are noise.
# The hard preconditions are the real safety net.

def cab_enabled() -> bool:
    """Return whether CAB voting is enabled for ITIL changes.

    This reads from the environment variable SKCOORD_ITIL_CAB_ENABLED.
    Valid values are '1', 'true', 'yes' (case-insensitive) for True.
    Any other value or absence defaults to False (CAB retired).

    To re-enable CAB:
        export SKCOORD_ITIL_CAB_ENABLED=1

    Or set permanently in ~/.bashrc, ~/.profile, or your shell's rc file.
    """
    val = os.environ.get("SKCOORD_ITIL_CAB_ENABLED", "").lower()
    return val in {"1", "true", "yes"}


def get_cab_config_path() -> Optional[Path]:
    """Return the path to a CAB configuration file, if one exists.

    An optional ~/.skcapstone/config/itil-cab.yaml file can override the
    environment variable. This file, if present, is read and parsed.

    File format (YAML):
        cab_enabled: true
        notes: "Re-enabled for audit trail requirement"

    Returns None if no config file exists.
    """
    config_path = Path.home() / ".skcapstone" / "config" / "itil-cab.yaml"
    if config_path.exists():
        return config_path
    return None


# ---------------------------------------------------------------------------
# Hard precondition configuration
# ---------------------------------------------------------------------------

def rollback_plan_required_for_risk(risk: str) -> bool:
    """Return whether a rollback plan is required for a given risk level.

    By default, HIGH risk changes require a rollback plan.
    This is a hard precondition: a change cannot move to implementing/deployed
    without a stated rollback plan.

    The risk level comes from the change's core.json and is one of:
    - "low"    (Risk.LOW)
    - "medium" (Risk.MEDIUM)
    - "high"   (Risk.HIGH)

    This function can be extended to add more granular control, e.g.:
    - Require rollback for EMERGENCY change type regardless of risk
    - Require rollback for specific change tags (e.g., "security", "key-custody")
    """
    return risk.lower() == "high"


def rollback_plan_required_for_change_type(change_type: str) -> bool:
    """Return whether a rollback plan is required for a given change type.

    EMERGENCY changes always require a rollback plan as a hard precondition.
    This is fail-closed: an emergency change without a rollback plan cannot
    proceed to implementation.

    The change type comes from the change's core.json and is one of:
    - "standard"  (ChangeType.STANDARD)
    - "normal"    (ChangeType.NORMAL)
    - "emergency" (ChangeType.EMERGENCY)
    """
    return change_type.lower() == "emergency"


def is_destructive_change(change_type: str, risk: str) -> bool:
    """Return whether a change is classified as destructive or high risk.

    Destructive/high-risk changes are subject to hard preconditions:
    1. Fail-closed preflight must pass
    2. Rollback plan must be stated
    3. Operator explicit approval required

    This matches the operator-seat classifier taxonomy and the controls
    that actually caught problems on chg-ca4d0ea5.

    Returns True for:
    - EMERGENCY change type (any risk level)
    - HIGH risk (any change type)
    """
    return change_type.lower() == "emergency" or risk.lower() == "high"


# ---------------------------------------------------------------------------
# Preflight configuration
# ---------------------------------------------------------------------------

def preflight_required_for_change(change_type: str, risk: str) -> bool:
    """Return whether a preflight check is required before execution.

    Preflight is required for destructive/high-risk changes. The preflight
    is fail-closed: if it fails, execution stops before any mutation and
    the change records the failure reason.

    The preflight implementation is external to this module (typically in
    skcapstone/change_deploy or a custom preflight runner). This function
    only declares whether it is required, not how to run it.

    Returns True for the same conditions as is_destructive_change().
    """
    return is_destructive_change(change_type, risk)
