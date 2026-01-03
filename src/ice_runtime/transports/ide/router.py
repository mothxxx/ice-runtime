from __future__ import annotations
import logging
import os
import re
from pathlib import Path
from engine.api.types import ActionContext
from engine.storage.session.context import SessionContext
from engine.storage.exceptions import WorkspaceNotFoundError

logger = logging.getLogger(__name__)


# ============================================================
# UTILS
# ============================================================

def _pack(ok: bool, data=None, error=None):
    return {
        "ok": ok,
        "data": data,
        "error": error,
        "errors": [error] if error else [],
    }


async def _apply_patch(server, file_path: str, content: str):
    """
    Scrive il contenuto su disco + notifica l’IDE con open_file.
    """
    try:
        p = Path(file_path).expanduser().resolve()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

        if server:
            await server.send_status(f"Scritto: {p}")
            await server.send_open_file(str(p), content)

        return True, None
    except Exception as e:
        return False, str(e)


def _extract_code_block(text: str) -> str | None:
    if not text:
        return None
    m = re.search(r"```(?:python)?\s*(.*?)```", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return None


def _resolve_ws_root(runtime, workspace_id: str) -> str | None:
    try:
        p = runtime.session_manager.get_workspace_path(workspace_id)  # type: ignore
        if p:
            return str(p)
        base = getattr(runtime.session_manager, "base_path", None)
        if base:
            return str(Path(base) / workspace_id)
    except Exception:
        return None


async def _ensure_workspace_active(runtime, workspace_id: str):
    """
    Attiva il workspace richiesto; se non esiste lo crea in modo minimale.
    Aggiorna il SessionContext corrente.
    """
    sm = runtime.session_manager
    try:
        ctx = await sm.activate_ai_workspace(workspace_id)
    except WorkspaceNotFoundError:
        # crea un workspace AI minimale
        await sm.create_ai_workspace(
            name=workspace_id,
            workspace_type="multi_agent",
            workspace_id=workspace_id,
            backends=sm.default_backend_configs,
        )
        ctx = await sm.activate_ai_workspace(workspace_id)

    SessionContext.set_current(ctx)
    return ctx


# ============================================================
# MAIN ROUTER
# ============================================================

async def handle_ide_request(request: dict, runtime) -> dict:
    """
    WebSocket router stabile per VSCode.
    Tutta la pipeline è async-safe.
    """
    command = request.get("command")
    params = request.get("params") or {}
    workspace_id = request.get("workspace_id", "default")
    panel_context = request.get("panel_context") or params.get("panel_context") or "global"

    orch = runtime.orchestrator
    api_registry = runtime.api_registry
    session_manager = runtime.session_manager
    server = runtime.ide_server

    logger.info(
        "IDE request",
        extra={
            "command": command,
            "workspace_id": workspace_id,
            "panel_context": panel_context,
        },
    )

    # ActionContext factory
    def ctx():
        return ActionContext(
            workspace_id=workspace_id,
            source="ide",
            panel_context=panel_context,
        )

    # ============================================================
    # GENERIC LLM / RAG / KNOWLEDGE / AGENTS
    # ============================================================

    if command == "agents.list":
        r = await orch.call_async("system.agents.list", {}, context=ctx())
        return _pack(r.is_ok(), r.data, None if r.is_ok() else str(r.errors))

    if command == "llm.complete":
        r = await orch.call_async("llm.complete", params, context=ctx())
        return _pack(r.is_ok(), r.data, None if r.is_ok() else str(r.errors))

    if command == "rag.query":
        r = await orch.call_async("rag.query", params, context=ctx())
        return _pack(r.is_ok(), r.data, None if r.is_ok() else str(r.errors))

    if command == "knowledge.search":
        r = await orch.call_async("knowledge.search", params, context=ctx())
        return _pack(r.is_ok(), r.data, None if r.is_ok() else str(r.errors))

    # ============================================================
    # WORKSPACE.INFO
    # ============================================================

    if command == "workspace.info":
        active = session_manager._registry.get_active_contexts()
        active_ws = active[0].workspace.id if active else workspace_id

        all_ws = await session_manager.list_workspaces(include_inactive=True)

        return _pack(True, {
            "active_workspace": active_ws,
            "all_workspaces": all_ws,
            "agents": api_registry.list_agent_names(),
            "actions": api_registry.list_actions_names(),
            "domains": api_registry.list_domains(),
        })

    # ============================================================
    # IDE.OPEN_FILE — lato IDE
    # ============================================================

    if command == "ide.open_file":
        path = params.get("path")
        content = params.get("content", "")

        if not path:
            return _pack(False, None, "Missing 'path'")

        if not server:
            return _pack(False, None, "IDE server not attached")

        await server.send_open_file(path, content)
        return _pack(True, {"sent": True})
    
    # ============================================================
    # IDE.HELLO — handshake base
    # ============================================================

    if command == "ide.hello":
        client = request.get("client", "unknown")
        version = request.get("version", "0.0")

        # Diciamo al client che tutto è ok
        return _pack(True, {
            "message": "hello",
            "client": client,
            "version": version
        })


    # ============================================================
    # IDE.ATTACH — attacca workspace IDE
    # ============================================================

    if command == "ide.attach":
        wid = request.get("workspace_id") or params.get("workspace_id") or "default"

        # Attiva workspace esistente oppure creane uno
        try:
            await _ensure_workspace_active(runtime, wid)
        except Exception as e:
            return _pack(False, None, f"Workspace attach error: {e}")

        if not server:
            return _pack(False, None, "IDE server not attached (no server instance)")

        # Registra workspace per l'IDE server
        server.attach_workspace(wid)

        return _pack(True, {"attached": wid})


    # ============================================================
    # IDE.CHAT — PIPELINE COMPLETA
    # ============================================================

    if command == "ide.chat":

        # --------------------------------------------------------
        # INPUT
        # --------------------------------------------------------
        prompt = (
            request.get("prompt")
            or params.get("prompt")
            or ""
        )
        root_param = params.get("root") if isinstance(params, dict) else request.get("root")
        if not prompt:
            return _pack(False, None, "Missing 'prompt'")

        if not server:
            return _pack(False, None, "IDE server not attached")

        ws_root = root_param or _resolve_ws_root(runtime, workspace_id)
        await _ensure_workspace_active(runtime, workspace_id)
        if ws_root:
            ws_root = str(Path(ws_root).expanduser())
            Path(ws_root).mkdir(parents=True, exist_ok=True)

        # Context locale con metadata workspace_root se fornito
        ctx_chat = ActionContext(
            workspace_id=workspace_id,
            source="ide",
            panel_context=panel_context,
            metadata={"workspace_root": ws_root} if ws_root else {},
        )

        await server.send_status("Pianificazione in corso…")

        # --------------------------------------------------------
        # 1) PLANNER
        # --------------------------------------------------------
        plan_res = await orch.call_async("workflow.plan", {"goal": prompt}, context=ctx_chat)
        if not plan_res.is_ok():
            return _pack(False, None, str(plan_res.errors))

        plan = plan_res.data
        await server._broadcast({"type": "plan", "plan": plan})

        # --------------------------------------------------------
        # 2) SCANNER
        # --------------------------------------------------------
        scan = None
        if ws_root and Path(ws_root).exists():
            await server.send_status("Scanner in esecuzione…")
            r = await orch.call_async(
                "scanner.project.scan",
                {"root": ws_root},
                context=ctx_chat,
            )
            if r.is_ok():
                scan = r.data

        # --------------------------------------------------------
        # 3) ANALYZER
        # --------------------------------------------------------
        analysis = None
        if ws_root and Path(ws_root).exists():
            await server.send_status("Analyzer in esecuzione…")
            r = await orch.call_async(
                "analyzer.code.hotspots",
                {"root": ws_root},
                context=ctx_chat,
            )
            if r.is_ok():
                analysis = r.data

        # --------------------------------------------------------
        # 4) RISPOSTA LLM NATURALE
        # --------------------------------------------------------
        llm_res = await orch.call_async(
            "llm.complete",
            {"prompt": prompt},
            context=ctx_chat,
        )
        answer = (
            llm_res.data.get("text")
            if llm_res and llm_res.is_ok()
            else None
        )
        answer_block = _extract_code_block(answer) if answer else None

        if answer:
            await server._broadcast({"type": "answer", "message": answer})

        # --------------------------------------------------------
        # 5) CODER (code_generate)
        # --------------------------------------------------------
        generated = None
        if ws_root:
            await server.send_status("Coder in esecuzione…")
            code_res = await orch.call_async(
                "code_generate",
                {
                    "prompt": prompt,
                    "plan": plan,
                    "scan": scan,
                    "analysis": analysis,
                },
                context=ctx_chat,
            )

            if code_res.is_ok():
                out = code_res.data.get("output")
                if out:
                    generated = {
                        "file_path": str(Path(ws_root) / "main.py"),
                        "code": out,
                    }

        # --------------------------------------------------------
        # 6) CODE_EDIT embedded nel planner
        # --------------------------------------------------------
        edits_applied = []
        raw_actions = plan.get("raw", {}).get("actions", [])

        for action in raw_actions:
            if action.get("type") != "code_edit":
                continue

            payload = action.get("payload") or {}
            file_path = payload.get("file_path")
            content = payload.get("content", "")

            if not file_path:
                continue

            ok, err = await _apply_patch(server, file_path, content)
            edits_applied.append({
                "file": file_path,
                "ok": ok,
                "error": err,
            })

        # --------------------------------------------------------
        # 7) SE NON CI SONO EDITS → SCRIVI IL CODICE GENERATO
        # --------------------------------------------------------
        written_path = None
        written_code = None

        # Se non ci sono code_edit, prova a scrivere il codice generato o il blocco dall'answer
        if ws_root and not edits_applied and (generated or answer_block):
            file_path = (generated or {}).get("file_path") or str(Path(ws_root) / "main.py")
            content = (generated or {}).get("code") or ""
            if answer_block:
                content = answer_block

            ok, err = await _apply_patch(server, file_path, content)

            if ok:
                written_path = file_path
                written_code = content
            else:
                await server.send_status(f"Errore scrivendo {file_path}: {err}", level="error")

        # --------------------------------------------------------
        # OUTPUT COMPLETO
        # --------------------------------------------------------
        return _pack(True, {
            "plan": plan,
            "answer": answer,
            "scan": scan,
            "analysis": analysis,
            "generated_code": generated,
            "applied_edits": edits_applied,
            "written_file": written_path,
            "written_code": written_code,
        })

    # ============================================================
    # PROJECT.GENERATE (rimasto invariato)
    # ============================================================

    if command == "project.generate":
        root = params.get("root")
        prompt = params.get("prompt")

        if not root or not prompt:
            return _pack(False, None, "Missing 'root' or 'prompt'")

        root_abs = str(Path(root).expanduser().resolve())
        os.makedirs(root_abs, exist_ok=True)

        plan = await orch.call_async("workflow.plan", {"goal": prompt}, context=ctx())
        if not plan.is_ok():
            return _pack(False, None, str(plan.errors))

        file_path = f"{root_abs}/main.py"
        content = "# Auto-generated\nprint('Hello from Cortex IDE')\n"
        Path(file_path).write_text(content, encoding="utf-8")

        if server:
            await server.send_open_project(root_abs)
            await server.send_open_file(file_path, content)

        return _pack(True, {
            "created_files": [file_path],
            "plan": plan.data,
        })

    # ============================================================
    # FALLBACK
    # ============================================================

    return _pack(False, None, f"Unknown command: {command}")
