from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Dict, Any
from urllib.parse import urlparse, parse_qs
import webbrowser

from protocols.security.identity import get_local_identity, NodeRole
from protocols.transport.udp.udp_responder import start_udp_responder


# -----------------------------------------------------------------------------
UI_DIR = Path(__file__).parent / "ui"

def setup_logging() -> None:
    root = logging.getLogger()

    if root.handlers:
        return

    root.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s - %(message)s")

    sh = logging.StreamHandler()
    sh.setFormatter(formatter)
    root.addHandler(sh)

    logging.getLogger("ice.network.udp_responder").setLevel(logging.INFO)
    logging.getLogger("ice.daemon").setLevel(logging.INFO)


logger = logging.getLogger("ice.daemon")


# -----------------------------------------------------------------------------
# STATO LOCALE PAIRING (IN-MEMORY, PER LA UI HOST)
# -----------------------------------------------------------------------------
# Chiave: request_id -> info pairing (pending/approved)
PAIRING_REQUESTS: Dict[str, Dict[str, Any]] = {}

def _json_bytes(payload: Dict[str, Any]) -> bytes:
    return json.dumps(payload).encode("utf-8")


def _auto_approve_with_preboot(request_id: str, preboot_ip: str) -> bool:
    """
    Chiama il preboot che ha iniziato il pairing (IP lato client)
    per approvare la request.
    """
    import urllib.request
    import urllib.error

    base = os.environ.get("ICE_PREBOOT_URL") or f"http://{preboot_ip}:7040"

    try:
        req = urllib.request.Request(
            f"{base}/preboot/pairing/approve",
            data=_json_bytes({"request_id": request_id}),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            if resp.status == 200:
                logger.info(
                    "[DAEMON] pairing approved via preboot (preboot=%s, request_id=%s)",
                    base,
                    request_id,
                )
                return True
            logger.warning(
                "[DAEMON] pairing approve HTTP non-200 (status=%s, request_id=%s)",
                resp.status,
                request_id,
            )
            return False
    except urllib.error.URLError as err:
        logger.error(
            "[DAEMON] pairing approve failed (network error, preboot=%s): %s",
            base,
            err,
        )
        return False
    except Exception as err:
        logger.error("[DAEMON] pairing approve failed: %s", err)
        return False


# -----------------------------------------------------------------------------
# HTTP HANDLER DEL DAEMON
# -----------------------------------------------------------------------------
class DaemonHandler(BaseHTTPRequestHandler):
    def _cors(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _serve_ui_file(self, rel_path: str, default: str = "index.html") -> None:
        from mimetypes import guess_type

        if not rel_path or rel_path == "/":
            rel_path = default

        fs_path = (UI_DIR / rel_path).resolve()

        if not str(fs_path).startswith(str(UI_DIR.resolve())):
            self.send_response(403)
            self.end_headers()
            return

        if not fs_path.exists():
            self.send_response(404)
            self.end_headers()
            return

        content_type, _ = guess_type(str(fs_path))
        content_type = content_type or "application/octet-stream"

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.end_headers()

        with fs_path.open("rb") as f:
            self.wfile.write(f.read())

    def _json(self, code: int, payload: Dict[str, Any]) -> None:
        try:
            self.send_response(code)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(_json_bytes(payload))
        except BrokenPipeError:
            logger.info("Client closed connection before response could be sent")

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.end_headers()

    # ------------------------------------------------------------------ GET
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path.startswith("/daemon/ui"):
            rel = path[len("/daemon/ui"):].lstrip("/")
            self._serve_ui_file(rel or "index.html")
            return
        # Healthcheck semplice
        if path == "/daemon/health":
            try:
                identity = get_local_identity(NodeRole.HOST)
                self._json(
                    200,
                    {
                        "ok": True,
                        "ts": int(time.time()),
                        "identity": asdict(identity),
                    },
                )
            except Exception as err:
                self._json(
                    500,
                    {
                        "ok": False,
                        "error": "identity_error",
                        "detail": str(err),
                    },
                )
            return

        if path == "/daemon/pairing/status":
            request_id = params.get("request_id", [""])[0].strip()
            if not request_id:
                self._json(400, {"error": "missing_request_id"})
                return

            state = PAIRING_REQUESTS.get(request_id)
            if not state:
                self._json(404, {"error": "unknown_request_id"})
                return

            self._json(
                200,
                {
                    "request_id": request_id,
                    "status": state.get("status", "pending"),
                    "host_id": state.get("host_id"),
                    "client_ip": state.get("client_ip"),
                    "message": state.get("message"),
                },
            )
            return

        if path == "/daemon/pairing/requests":
            items = []
            now = time.time()
            # cleanup + build response
            for rid, st in list(PAIRING_REQUESTS.items()):
                age = now - st.get("created_at", now)
                status = st.get("status")
                # drop vecchie richieste non approvate/dismesse (es. >600s)
                if status not in ("approved", "dismissed") and age > 600:
                    PAIRING_REQUESTS.pop(rid, None)
                    continue

                items.append(
                    {
                        "request_id": rid,
                        "status": status,
                        "host_id": st.get("host_id"),
                        "client_ip": st.get("client_ip"),
                        "message": st.get("message"),
                        "age_sec": int(age),
                    }
                )
            self._json(200, {"requests": items})
            return


        self._json(404, {"error": "not_found", "path": path})

    # ------------------------------------------------------------------ POST
    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            data = {}

        # Notify dalla macchina client (via preboot): richiesta di pairing
        if path == "/daemon/ui/pairing":
            host_id = data.get("host_id")
            request_id = data.get("request_id")
            client_ip = data.get("client_ip")
            message = data.get("message") or "Pairing request pending approval"

            if not host_id or not request_id:
                self._json(
                    400,
                    {"error": "missing_fields", "required": ["host_id", "request_id"]},
                )
                return

            preboot_ip = self.client_address[0]

            logger.info(
                "[DAEMON] pairing notify received: %s",
                {
                    "host_id": host_id,
                    "request_id": request_id,
                    "client_ip": client_ip,
                    "preboot_ip": preboot_ip,
                },
            )

            now = time.time()
            PAIRING_REQUESTS[request_id] = {
                "host_id": host_id,
                "client_ip": client_ip,
                "preboot_ip": preboot_ip,
                "message": message,
                "status": "pending",
                "created_at": now,
                "updated_at": now,
            }

            pending_count = sum(
                1 for info in PAIRING_REQUESTS.values() if info.get("status") == "pending"
            )
            if pending_count == 1:
                try:
                    webbrowser.open("http://127.0.0.1:7030/daemon/ui", new=1)
                except Exception as err:
                    logger.warning("Unable to open daemon UI automatically: %s", err)

            self._json(
                200,
                {
                    "ok": True,
                    "request_id": request_id,
                    "status": "pending",
                },
            )
            return

        if path == "/daemon/pairing/approve":
            req_id = data.get("request_id")
            if not req_id:
                self._json(400, {"error": "missing_request_id"})
                return

            state = PAIRING_REQUESTS.get(req_id)
            if not state:
                self._json(404, {"error": "unknown_request_id"})
                return

            preboot_ip = state.get("preboot_ip")
            if not preboot_ip:
                self._json(400, {"error": "missing_preboot_ip"})
                return

            ok = _auto_approve_with_preboot(req_id, preboot_ip)
            if ok:
                state["status"] = "approved"
                state["updated_at"] = time.time()
                # 👉 una volta approvata, la togliamo dalla lista
                PAIRING_REQUESTS.pop(req_id, None)

            self._json(
                200,
                {
                    "ok": ok,
                    "request_id": req_id,
                    "status": state.get("status", "pending"),
                },
            )
            return

        if path == "/daemon/pairing/dismiss":
            req_id = data.get("request_id")
            if not req_id:
                self._json(400, {"error": "missing_request_id"})
                return

            state = PAIRING_REQUESTS.get(req_id)
            if not state:
                self._json(404, {"error": "unknown_request_id"})
                return

            state["status"] = "dismissed"
            state["updated_at"] = time.time()

            self._json(
                200,
                {
                    "ok": True,
                    "request_id": req_id,
                    "status": "dismissed",
                },
            )
            return

        self._json(404, {"error": "not_found", "path": path})


# -----------------------------------------------------------------------------
# MAIN
# -----------------------------------------------------------------------------
def _start_http_server(bind_host: str = "0.0.0.0", port: int = 7030) -> threading.Thread:
    server = HTTPServer((bind_host, port), DaemonHandler)

    def _serve() -> None:
        logger.info("ICE Daemon HTTP server listening on %s:%s", bind_host, port)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            logger.info("ICE Daemon HTTP server interrupted")
        finally:
            server.server_close()
            logger.info("ICE Daemon HTTP server stopped")

    thread = threading.Thread(target=_serve, name="daemon-http", daemon=True)
    thread.start()
    return thread


def main() -> None:
    setup_logging()
    logger.info("ICE Daemon starting (UDP discovery + HTTP control)")

    try:
        identity = get_local_identity(NodeRole.HOST)
        payload = identity.__dict__
    except Exception as err:
        logger.error("Failed to load local identity: %s", err)
        return

    udp_thread = start_udp_responder(payload)
    if not udp_thread:
        logger.error("UDP responder not started; exiting")
        return

    logger.info("UDP responder active on port 7042 (thread=%s)", udp_thread.name)

    http_thread = _start_http_server("0.0.0.0", 7030)
    logger.info(
        "ICE Daemon running. UDP thread=%s, HTTP thread=%s",
        udp_thread.name,
        http_thread.name,
    )

    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        logger.info("ICE Daemon interrupted; shutting down")


if __name__ == "__main__":
    main()
