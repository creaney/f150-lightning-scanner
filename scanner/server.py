"""scanner/server.py -- local report HTTP server."""
import json
import threading
import time
from pathlib import Path
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

from scanner.config import log, STATE_FILE, _SERVER_PORT, _SERVER_INACTIVITY_SECS, SOLD

class _ReportHandler(BaseHTTPRequestHandler):
    """Lightweight HTTP handler for the live-report feedback server.

    Class attributes set by _start_report_server():
      report_path   – Path to the generated HTML file
      last_activity – threading.Event reset on every request; the server
                      thread watches this to implement the inactivity timeout
    """
    report_path: Path
    last_activity: threading.Event

    # ── silence server log spam ────────────────────────────────────────
    def log_message(self, fmt, *args):  # type: ignore[override]
        pass

    def _send_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _reset_timer(self) -> None:
        _ReportHandler.last_activity.set()

    # ── GET / ─────────────────────────────────────────────────────────
    def do_GET(self) -> None:
        self._reset_timer()
        parsed = urlparse(self.path)
        if parsed.path == "/ping":
            self._send_json({"ok": True, "ts": time.time()})
            return
        if parsed.path != "/":
            self.send_response(404)
            self.end_headers()
            return
        try:
            body = _ReportHandler.report_path.read_bytes()
        except OSError:
            body = b"<p>Report not found.</p>"
        self._send_html(body)

    # ── POST /dismiss  /set-er  /set-511a ─────────────────────────────
    def do_POST(self) -> None:
        self._reset_timer()
        parsed  = urlparse(self.path)
        q_params = parse_qs(parsed.query)

        # Read optional request body (for JSON-body callers, e.g. curl tests)
        body_params: dict = {}
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
            if length > 0:
                raw_body = self.rfile.read(length).decode("utf-8", errors="replace")
                ct = self.headers.get("Content-Type", "")
                if "application/json" in ct:
                    body_params = json.loads(raw_body)
                elif "application/x-www-form-urlencoded" in ct:
                    for kv in raw_body.split("&"):
                        if "=" in kv:
                            k, v = kv.split("=", 1)
                            body_params[k] = v
        except Exception:
            pass

        def _get(key: str, fallback: str = "") -> str:
            """Look up key in URL query params then request body."""
            v = (q_params.get(key) or [""])[0].strip()
            if not v:
                raw = body_params.get(key, fallback)
                v = str(raw).strip() if raw is not None else fallback
            return v

        uid  = _get("uuid") or _get("uid") or _get("id")
        if not uid:
            self._send_json({"ok": False, "error": "missing uuid"}, 400)
            return

        state = load_state()
        entry = state["listings"].get(uid)
        if entry is None:
            self._send_json({"ok": False, "error": f"uuid not found: {uid}"}, 404)
            return

        path = parsed.path.rstrip("/")

        if path == "/dismiss":
            entry["dismissed"] = True
            log.info("Server: dismissed %s (%s)", uid[:12], entry.get("title", ""))

        elif path == "/set-er":
            raw = _get("value").lower()
            if raw not in ("true", "false"):
                self._send_json({"ok": False, "error": "value must be true|false"}, 400)
                return
            val = raw == "true"
            entry["extended_range_confirmed"] = val
            entry["er_note"] = "User confirmed"
            log.info("Server: set-er=%s for %s", val, uid[:12])

        elif path == "/set-511a":
            raw = _get("value").lower()
            if raw not in ("true", "false"):
                self._send_json({"ok": False, "error": "value must be true|false"}, 400)
                return
            val = raw == "true"
            entry["equipment_511a_confirmed"] = val
            entry["equip_note"] = "User confirmed"
            log.info("Server: set-511a=%s for %s", val, uid[:12])

        elif path == "/set-history":
            raw = _get("value").lower().strip()
            _HIST_MAP = {
                "clean":    ("✅ Clean",    "user-set"),
                "accident": ("⚠️ Accident", "user-set"),
                "buyback":  ("🚫 Buyback",  "user-set"),
                "unknown":  ("❓ Unknown",  ""),
            }
            if raw not in _HIST_MAP:
                self._send_json({"ok": False, "error": "value must be clean|accident|buyback|unknown"}, 400)
                return
            flag, note = _HIST_MAP[raw]
            entry["history_flag"] = flag
            entry["history_note"] = note
            log.info("Server: set-history=%s for %s", raw, uid[:12])

        else:
            self._send_json({"ok": False, "error": "unknown endpoint"}, 404)
            return

        # Atomic write: write to a temp file then rename over STATE_FILE
        _tmp = STATE_FILE.with_suffix(".tmp")
        _tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False))
        _tmp.replace(STATE_FILE)
        self._send_json({"ok": True})


def _start_report_server(report_path: Path) -> threading.Thread:
    """Start the report feedback server in a daemon thread.

    Returns the thread (already started).  The server runs until
    _SERVER_INACTIVITY_SECS of inactivity, then shuts down cleanly.
    """
    _ReportHandler.report_path   = report_path
    _ReportHandler.last_activity = threading.Event()

    server = HTTPServer(("localhost", _SERVER_PORT), _ReportHandler)
    server.timeout = 1.0   # handle_request() returns after 1 s if no request arrives

    def _run() -> None:
        deadline = _SERVER_INACTIVITY_SECS
        idle     = 0
        while idle < deadline:
            _ReportHandler.last_activity.clear()
            server.handle_request()
            if _ReportHandler.last_activity.is_set():
                idle = 0   # activity — reset idle counter
            else:
                idle += 1  # 1 second of silence
        log.info("Report server: inactivity timeout — shutting down")
        server.server_close()

    t = threading.Thread(target=_run, daemon=True, name="report-server")
    t.start()
    return t


# ═══════════════════════════════════════════════════════════════════════════
# Startup helpers
# ═══════════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════

