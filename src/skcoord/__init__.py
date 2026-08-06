"""skcoord: sovereign multi-agent coordination + ITIL service management.

Extracted from ``skcapstone`` (CR-4.1) as the standalone coordination core.
Holds the conflict-free task board, the unified kanban Card projection, the
event-sourced CardStore, ITIL (incident/problem/change/CAB/KEDB), the CMDB,
and the shareable agent identity card.

Import-time dependencies flow one way: ``skcapstone`` depends on ``skcoord``.
The few reverse edges into skcapstone internals (skjoule, active_agent_name,
gtd_tools, pubsub, activity) are runtime-lazy inside the methods that use them,
so there is no import-time cycle. ``skcapstone.coordination`` / ``.card`` /
``.card_store`` / ``.itil`` / ``.cmdb`` / ``.agent_card`` / ``.atomic_io``
remain as re-export shims so every existing importer keeps working.
"""

from __future__ import annotations

__version__ = "0.1.0"

from .agent_card import AgentCapability, AgentCard
from .atomic_io import atomic_write_text
from .card import (
    Card,
    CardEvent,
    CardEventLog,
    Column,
    KanbanBoard,
    Kind,
    render_html,
)
from .coordination import (
    AgentFile,
    AgentState,
    Board,
    Task,
    TaskPriority,
    TaskStatus,
    TaskView,
    get_briefing_json,
    get_briefing_text,
)

__all__ = [
    "AgentCapability",
    "AgentCard",
    "AgentFile",
    "AgentState",
    "Board",
    "Card",
    "CardEvent",
    "CardEventLog",
    "Column",
    "KanbanBoard",
    "Kind",
    "Task",
    "TaskPriority",
    "TaskStatus",
    "TaskView",
    "atomic_write_text",
    "get_briefing_json",
    "get_briefing_text",
    "render_html",
]
