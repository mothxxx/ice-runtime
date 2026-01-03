"""
Formato standard RPC tra IDE ↔ Cortex IDE server.

Request:
{
    "id": <req-id>,
    "command": "llm.complete",
    "params": { ... },
    "workspace_id": "default",
    "panel_context": "global"  # opzionale, identifica tab/panel IDE
}

Response:
{
    "id": <req-id>,
    "result": { ... },
    "error": null | { ... }
}
"""
