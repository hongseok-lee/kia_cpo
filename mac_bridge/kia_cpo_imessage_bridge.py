#!/usr/bin/env python3
"""Small Tailscale-only webhook bridge for Kia CPO notifications.

Receives GitHub Actions JSON payloads and sends a concise iMessage through
macOS Messages.app. It uses only the Python standard library and osascript.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in raw_line:
            continue
        key, value = raw_line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


load_env_file(Path.home() / ".kia-cpo-imessage.env")

HOST = os.environ.get("KIA_CPO_BRIDGE_HOST", "0.0.0.0")
PORT = int(os.environ.get("KIA_CPO_BRIDGE_PORT", "8787"))
TOKEN = os.environ.get("KIA_CPO_WEBHOOK_TOKEN", "")
RECIPIENT = os.environ.get("IMESSAGE_RECIPIENT", "")
LOG_PATH = Path(os.environ.get("KIA_CPO_BRIDGE_LOG", str(Path.home() / "Library/Logs/kia-cpo-imessage.log")))
TOKEN_PATTERN = re.compile(r"token=[^&\s\"]+")


def log(message: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    with LOG_PATH.open("a", encoding="utf-8") as file:
        file.write(f"{now} {message}\n")


def redact_secrets(message: str) -> str:
    return TOKEN_PATTERN.sub("token=<redacted>", message)


def format_message(payload: dict) -> str:
    vehicles = payload.get("vehicles") or payload.get("added_vehicles") or []
    lines = [f"Kia CPO 새 차량 {len(vehicles)}대"]
    for vehicle in vehicles[:5]:
        options = vehicle.get("selectable_option_names") or []
        option_text = ", ".join(options[:4])
        if len(options) > 4:
            option_text += f" 외 {len(options) - 4}"
        summary = " / ".join(
            part
            for part in [
                str(vehicle.get("plate_number") or ""),
                str(vehicle.get("price_text") or ""),
                str(vehicle.get("first_registered_month") or ""),
                str(vehicle.get("mileage_text") or ""),
                str(vehicle.get("fuel_label") or ""),
                str(vehicle.get("trim_group") or ""),
            ]
            if part
        )
        lines.append(summary)
        if option_text:
            lines.append(f"옵션: {option_text}")
        if vehicle.get("detail_url"):
            lines.append(str(vehicle["detail_url"]))
    if len(vehicles) > 5:
        lines.append(f"외 {len(vehicles) - 5}대")
    return "\n".join(lines)


def send_imessage(message: str) -> None:
    if not RECIPIENT:
        log("IMESSAGE_RECIPIENT is empty; message logged but not sent")
        return
    script = """
    on run argv
      set targetBuddy to item 1 of argv
      set targetMessage to item 2 of argv

      tell application "Messages" to launch

      tell application "Messages"
        set targetService to 1st service whose service type = iMessage
        set targetHandle to buddy targetBuddy of targetService
        send targetMessage to targetHandle
      end tell

      try
        tell application "System Events" to set visible of process "Messages" to false
      end try
    end run
    """
    subprocess.run(["osascript", "-e", script, RECIPIENT, message], check=True, timeout=20)


class Handler(BaseHTTPRequestHandler):
    server_version = "KiaCPOiMessageBridge/1.0"

    def log_message(self, fmt: str, *args) -> None:  # noqa: N802 - stdlib API
        log(redact_secrets(f"{self.address_string()} {fmt % args}"))

    def respond(self, status: int, body: dict) -> None:
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802 - stdlib API
        if urlparse(self.path).path == "/health":
            self.respond(200, {"ok": True, "recipient_configured": bool(RECIPIENT)})
            return
        self.respond(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib API
        parsed = urlparse(self.path)
        if parsed.path != "/kia-cpo":
            self.respond(404, {"ok": False, "error": "not found"})
            return

        supplied_token = parse_qs(parsed.query).get("token", [""])[0]
        if TOKEN and supplied_token != TOKEN:
            self.respond(403, {"ok": False, "error": "forbidden"})
            return

        try:
            length = int(self.headers.get("content-length", "0"))
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8"))
            message = format_message(payload)
            log(f"received payload: {json.dumps(payload, ensure_ascii=False)}")
            send_imessage(message)
            self.respond(200, {"ok": True, "sent": bool(RECIPIENT), "message": message})
        except Exception as exc:  # Keep the bridge alive and preserve diagnostics.
            log(f"error: {type(exc).__name__}: {exc}")
            self.respond(500, {"ok": False, "error": type(exc).__name__, "message": str(exc)})


def main() -> None:
    log(f"starting bridge host={HOST} port={PORT} recipient_configured={bool(RECIPIENT)}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
