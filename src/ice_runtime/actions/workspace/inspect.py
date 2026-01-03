from __future__ import annotations

from typing import Any, Dict

from engine.api.types import ActionContext
from engine.storage.session.manager import SessionManager


async def workspace_inspect(
    session_manager: SessionManager,
    workspace_id: str | None,
    context: ActionContext | None = None,
) -> Dict[str, Any]:
    """
    Raccoglie un riepilogo del workspace e dello stato SessionManager.
    """
    ws_id = workspace_id or getattr(context, "workspace_id", None) or "default"

    workspace = await session_manager.get_ai_workspace_info(ws_id)
    all_workspaces = await session_manager.list_workspaces(include_inactive=True)
    stats = session_manager.get_stats()

    return {
        "workspace_id": ws_id,
        "workspace": workspace,
        "all_workspaces": all_workspaces,
        "stats": stats,
        "base_path": str(session_manager.base_path),
    }
