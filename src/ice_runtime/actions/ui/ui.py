from __future__ import annotations

from engine.api.types import ActionContext, ActionResult, ResultStatus
from engine.storage.session.manager import SessionManager


async def set_panel(params: dict, context: ActionContext, session_manager: SessionManager) -> ActionResult:
    workspace_id = params.get("workspace_id") or getattr(context, "workspace_id", None) or "default"
    panel = params.get("panel")

    if not panel:
        ar = ActionResult(name="ui.panel.set", status=ResultStatus.FAILED)
        ar.add_error(code="missing_panel", message="Parameter 'panel' is required")
        return ar

    await session_manager.set_workspace_panel(workspace_id, panel)

    return ActionResult(
        name="ui.panel.set",
        status=ResultStatus.SUCCESS,
        data={"workspace_id": workspace_id, "panel": panel},
    )
