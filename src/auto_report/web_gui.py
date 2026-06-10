from __future__ import annotations

import json
import logging
import os
import queue
import random
import secrets
import socket
import subprocess
import threading
import time
import webbrowser
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .config import APP_NAME, load_config
from .logging_setup import configure_logging
from .service import AutoReportService
from .user_settings import load_user_settings, save_user_settings


WEB_GUI_VERSION = "WEB GUI v15 ALIGNED SETTINGS GRID 2026-06-10"
SERVICE_LABEL = "com.anubis.auto-report.service"
WEB_GUI_LABEL = "com.anubis.auto-report.webgui"
HKO_CLOCK_URL = "https://time.weather.gov.hk/cgi-bin/clock9a.pr?t=?t="


class QueueLogHandler(logging.Handler):
    def __init__(self, log_queue: queue.Queue[str]):
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record: logging.LogRecord) -> None:
        self.log_queue.put(self.format(record))


class WebGuiController:
    def __init__(self):
        self.config = load_config()
        self.logger = configure_logging(self.config)
        self.log_queue: queue.Queue[str] = queue.Queue()
        handler = QueueLogHandler(self.log_queue)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
        self.logger.addHandler(handler)

        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.started_at = datetime.now()
        self.status = "待機中"
        self.mode = "待機"
        self.last_event = "Web GUI 已啟動"
        self.last_update = datetime.now()
        self.logs: list[str] = ["Web GUI 已啟動，狀態會即時更新。"]
        self.lock = threading.Lock()

    def snapshot(self) -> dict:
        self._drain_logs()
        service = self._launch_agent_state(SERVICE_LABEL)
        service_logs = self._read_log_tail(self.config.log_dir / "service.err.log", limit=90)
        current_service_logs = self._logs_since_latest_service_start(service_logs)
        with self.lock:
            worker_alive = bool(self.worker and self.worker.is_alive())
            now = datetime.now()
            self.last_update = now
            visible_logs = list(self.logs[-30:])
            if current_service_logs:
                visible_logs.append("--- 24/7 service log ---")
                visible_logs.extend(current_service_logs[-90:])
            last_event = current_service_logs[-1] if current_service_logs else self.last_event
            return {
                "app": APP_NAME,
                "version": WEB_GUI_VERSION,
                "status": self.status,
                "mode": self.mode,
                "worker_alive": worker_alive,
                "last_event": last_event,
                "last_update": now.strftime("%Y-%m-%d %H:%M:%S HKT"),
                "started_at": self.started_at.strftime("%Y-%m-%d %H:%M:%S HKT"),
                "web_gui_label": WEB_GUI_LABEL,
                "service_label": SERVICE_LABEL,
                "service_running": service["running"],
                "service_pid": service["pid"],
                "service_state": service["state"],
                "service_message": service["message"],
                "telegram_conflict": any("409: Conflict" in line for line in current_service_logs[-20:]),
                "refresh_interval": self.config.refresh_interval_seconds,
                "report_interval": self.config.report_interval_seconds,
                "daily_report_time": self.config.daily_report_time,
                "env_path": str(self.config.env_path or "未找到 .env"),
                "output_dir": str(self.config.output_dir),
                "logs": visible_logs[-130:],
            }

    def run_once(self) -> str:
        return self._start_worker("單次報表", self._run_once_worker)

    def settings_snapshot(self) -> dict:
        return load_user_settings(self.config.state_dir).public_dict()

    def update_settings(self, payload: dict) -> dict:
        settings = save_user_settings(self.config.state_dir, payload)
        self.logger.info(
            "已更新 Web GUI 設定：文件名=%s 預覽圖片=%s OTP提醒=%s秒",
            settings.telegram_filename_template,
            settings.send_preview_image,
            settings.otp_reminder_interval_seconds,
        )
        self._set_status("待機中", mode="待機", detail="Telegram 與驗證設定已保存並立即生效")
        return settings.public_dict()

    def start_loop(self) -> str:
        return self._start_worker("24/7 循環", self._loop_worker)

    def start_service(self) -> str:
        script = self.config.home / "scripts" / "install_service_launch_agent.command"
        if not script.exists():
            return "找不到 install_service_launch_agent.command"
        self._set_status("正在啟動服務", mode="控制台", detail="正在啟動 24/7 背景服務")
        result = self._run_command([str(script)])
        state = self._launch_agent_state(SERVICE_LABEL)
        if state["running"]:
            self._set_status("服務已啟動", detail=f"24/7 背景服務已運行，PID {state['pid']}")
        else:
            self._set_status("服務啟動異常", detail=result or state["message"])
        return result or state["message"]

    def stop_service(self) -> str:
        script = self.config.home / "scripts" / "uninstall_service_launch_agent.command"
        if not script.exists():
            return "找不到 uninstall_service_launch_agent.command"
        self._set_status("正在停止服務", mode="控制台", detail="正在停止 24/7 背景服務")
        result = self._run_command([str(script)])
        state = self._launch_agent_state(SERVICE_LABEL)
        if state["running"]:
            self._set_status("服務停止異常", detail=state["message"])
        else:
            self._set_status("服務已停止", detail="24/7 背景服務已停止")
        return result or state["message"]

    def stop(self) -> str:
        self.stop_event.set()
        self._set_status("正在停止...", detail="已送出停止信號")
        return "已送出停止信號"

    def _start_worker(self, mode: str, target) -> str:
        with self.lock:
            if self.worker and self.worker.is_alive():
                return "已有任務正在執行"
            self.stop_event.clear()
            self.mode = mode
            self.status = "準備啟動"
            self.last_event = f"準備啟動 {mode}"
            self.last_update = datetime.now()
            self.worker = threading.Thread(target=target, daemon=True)
            self.worker.start()
        return f"已啟動 {mode}"

    def _run_once_worker(self) -> None:
        self._set_status("執行中", mode="單次報表", detail="正在刷新頁面、讀取資料並發送 Telegram")
        try:
            AutoReportService(self.config, self.logger).run_once(send_telegram=True)
            self._set_status("待機中", mode="待機", detail="單次報表已完成，已返回待機")
        except Exception as exc:
            self.logger.exception("執行失敗: %s", exc)
            self._set_status("待機中", mode="待機", detail=f"上次單次報表失敗，已返回待機: {exc}")

    def _loop_worker(self) -> None:
        self._set_status("24/7 循環中", mode="24/7 循環", detail="循環報表服務運行中")
        try:
            AutoReportService(self.config, self.logger).run_forever(stop_flag=self.stop_event.is_set)
            self._set_status("已停止", detail="24/7 循環已停止")
        except Exception as exc:
            self.logger.exception("循環失敗: %s", exc)
            self._set_status("失敗", detail=str(exc))

    def _service_worker(self) -> None:
        self._set_status("常駐服務中", mode="常駐服務 /daily", detail="每 60 秒刷新頁面，並監聽 Telegram /daily")
        try:
            AutoReportService(self.config, self.logger).run_service(stop_flag=self.stop_event.is_set)
            self._set_status("已停止", detail="常駐服務已停止")
        except Exception as exc:
            self.logger.exception("常駐服務失敗: %s", exc)
            self._set_status("失敗", detail=str(exc))

    def _set_status(self, status: str, mode: str | None = None, detail: str | None = None) -> None:
        with self.lock:
            self.status = status
            if mode is not None:
                self.mode = mode
            if detail is not None:
                self.last_event = detail
                self.logs.append(f"{datetime.now().strftime('%H:%M:%S')} {detail}")
            self.last_update = datetime.now()

    def _drain_logs(self) -> None:
        changed = False
        while True:
            try:
                line = self.log_queue.get_nowait()
            except queue.Empty:
                break
            with self.lock:
                self.logs.append(line)
                self.logs = self.logs[-120:]
                self.last_event = line
                self.last_update = datetime.now()
            changed = True
        if changed:
            with self.lock:
                self.logs = self.logs[-120:]

    def _run_command(self, cmd: list[str]) -> str:
        try:
            result = subprocess.run(
                cmd,
                cwd=str(self.config.home),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=45,
                check=False,
            )
        except Exception as exc:
            message = f"執行命令失敗: {exc}"
            self.logger.warning(message)
            return message
        output = (result.stdout or "").strip()
        if output:
            for line in output.splitlines()[-8:]:
                self._set_status(self.status, detail=line)
        if result.returncode != 0:
            message = f"命令返回錯誤碼 {result.returncode}"
            self.logger.warning("%s: %s", message, output)
            return f"{message}: {output}"
        return output

    @staticmethod
    def _launch_agent_state(label: str) -> dict:
        domain = f"gui/{os.getuid()}"
        try:
            result = subprocess.run(
                ["launchctl", "print", f"{domain}/{label}"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=8,
                check=False,
            )
        except Exception as exc:
            return {"running": False, "pid": "", "state": "unknown", "message": str(exc)}
        output = result.stdout or ""
        if result.returncode != 0:
            return {"running": False, "pid": "", "state": "not installed", "message": "未安裝或未啟動"}
        pid = ""
        state = "unknown"
        for line in output.splitlines():
            stripped = line.strip()
            if stripped.startswith("state ="):
                state = stripped.split("=", 1)[1].strip()
            elif stripped.startswith("pid ="):
                pid = stripped.split("=", 1)[1].strip()
        return {
            "running": bool(pid) and state in {"running", "active"},
            "pid": pid,
            "state": state,
            "message": f"{state} PID {pid}" if pid else state,
        }

    @staticmethod
    def _read_log_tail(path: Path, limit: int = 80) -> list[str]:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []
        return lines[-limit:]

    @staticmethod
    def _logs_since_latest_service_start(lines: list[str]) -> list[str]:
        for index in range(len(lines) - 1, -1, -1):
            if "常駐服務啟動" in lines[index]:
                return lines[index:]
        return lines


def _html(csrf_token: str) -> bytes:
    return HTML.replace("__CSRF_TOKEN__", csrf_token).encode("utf-8")


def _clock_frame_html() -> bytes:
    return CLOCK_FRAME_HTML.encode("utf-8")


def _company_icon_svg() -> bytes:
    return COMPANY_ICON_SVG.encode("utf-8")


def _company_logo_png() -> bytes:
    candidates = [
        Path(os.environ.get("AUTO_REPORT_HOME", "")) / "assets" / "company-logo.png",
        Path.home() / "AnubisAutoReport" / "assets" / "company-logo.png",
        Path(__file__).resolve().parents[2] / "assets" / "company-logo.png",
    ]
    for path in candidates:
        try:
            return path.read_bytes()
        except OSError:
            continue
    raise FileNotFoundError("company-logo.png not found")


def _json_response(handler: BaseHTTPRequestHandler, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    _send_security_headers(handler)
    handler.end_headers()
    handler.wfile.write(body)


def _send_security_headers(handler: BaseHTTPRequestHandler) -> None:
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.send_header("X-Frame-Options", "SAMEORIGIN")
    handler.send_header("Referrer-Policy", "no-referrer")
    handler.send_header(
        "Content-Security-Policy",
        "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-src 'self'; "
        "object-src 'none'; base-uri 'none'; frame-ancestors 'self'",
    )


def _hko_sun_shift(in_char: str, in_data: int) -> int:
    value = ord(in_char)
    if value > 0x60:
        value -= 0x3D
    elif value > 0x40:
        value -= 0x37
    else:
        value -= 0x30
    value -= in_data
    value += 0x3E
    return value % 0x3E


def _decode_hko_clock(raw_text: str) -> tuple[int, int]:
    marker, encoded = raw_text.strip().split("=", 1)
    raw_data = "QTXMB"
    raw_seq = "JDGMAO"
    real = ""
    for char in raw_seq:
        real += encoded[ord(char) - 0x40]

    server_seconds = 0x5CB27800
    for index in range(5):
        shift = ord(raw_data[index]) - 0x40
        value = _hko_sun_shift(real[index], shift)
        server_seconds += (60 ** (4 - index)) * value
    server_seconds += _hko_sun_shift(real[5], 0) / 0x3D
    return int(round(server_seconds * 1000)), int(marker or 0)


def _hko_time_payload() -> dict:
    request = Request(
        HKO_CLOCK_URL + str(random.random()),
        headers={"User-Agent": "AnubisAutoReport/1.0"},
    )
    started = time.time()
    try:
        with urlopen(request, timeout=4) as response:
            raw_text = response.read(128).decode("ascii", errors="replace")
        finished = time.time()
        server_epoch_ms, leap_second_indicator = _decode_hko_clock(raw_text)
        sampled_at_epoch_ms = int(round(((started + finished) / 2) * 1000))
        offset_ms = server_epoch_ms - sampled_at_epoch_ms
        return {
            "ok": True,
            "source": "HKO",
            "server_epoch_ms": server_epoch_ms,
            "sampled_at_epoch_ms": sampled_at_epoch_ms,
            "offset_ms": offset_ms,
            "latency_ms": int(round((finished - started) * 1000)),
            "leap_second_indicator": leap_second_indicator,
            "synced_at": datetime.fromtimestamp(finished, timezone.utc).isoformat(),
        }
    except Exception as exc:
        now_ms = int(round(time.time() * 1000))
        return {
            "ok": False,
            "source": "LOCAL",
            "server_epoch_ms": now_ms,
            "sampled_at_epoch_ms": now_ms,
            "offset_ms": 0,
            "latency_ms": None,
            "message": str(exc),
            "synced_at": datetime.now(timezone.utc).isoformat(),
        }


def _port_is_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def make_handler(controller: WebGuiController, csrf_token: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:  # noqa: A002
            controller.logger.debug("Web GUI: " + format, *args)

        def _trusted_local_request(self) -> bool:
            host = self.headers.get("Host", "").split(":", 1)[0].lower()
            return host in {"127.0.0.1", "localhost"}

        def _valid_csrf_request(self) -> bool:
            if not secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), csrf_token):
                return False
            origin = self.headers.get("Origin")
            if not origin:
                return True
            port = self.server.server_address[1]
            return origin in {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

        def do_GET(self) -> None:
            if not self._trusted_local_request():
                _json_response(self, {"ok": False, "message": "Forbidden"}, HTTPStatus.FORBIDDEN)
                return
            parsed = urlparse(self.path)
            if parsed.path in {"/", "/index.html"}:
                body = _html(csrf_token)
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                _send_security_headers(self)
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/clock-frame":
                body = _clock_frame_html()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path in {"/company-icon.svg", "/company-logo-v8.svg"}:
                body = _company_icon_svg()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/company-logo.png":
                try:
                    body = _company_logo_png()
                except OSError:
                    body = _company_icon_svg()
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
                else:
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            if parsed.path == "/api/status":
                _json_response(self, controller.snapshot())
                return
            if parsed.path == "/api/settings":
                _json_response(self, {"ok": True, "settings": controller.settings_snapshot()})
                return
            if parsed.path == "/api/hko-time":
                _json_response(self, _hko_time_payload())
                return
            _json_response(self, {"ok": False, "message": "Not found"}, HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:
            if not self._trusted_local_request() or not self._valid_csrf_request():
                _json_response(self, {"ok": False, "message": "Forbidden"}, HTTPStatus.FORBIDDEN)
                return
            parsed = urlparse(self.path)
            if parsed.path == "/api/settings":
                try:
                    content_length = int(self.headers.get("Content-Length", "0"))
                    if content_length <= 0 or content_length > 65536:
                        raise ValueError("設定資料大小不正確。")
                    payload = json.loads(self.rfile.read(content_length).decode("utf-8"))
                    if not isinstance(payload, dict):
                        raise ValueError("設定資料格式不正確。")
                    settings = controller.update_settings(payload)
                except (ValueError, json.JSONDecodeError) as exc:
                    _json_response(
                        self,
                        {"ok": False, "message": str(exc)},
                        HTTPStatus.BAD_REQUEST,
                    )
                    return
                _json_response(self, {"ok": True, "message": "設定已保存並立即生效", "settings": settings})
                return
            action_map = {
                "/api/run-once": controller.run_once,
                "/api/start-loop": controller.start_loop,
                "/api/start-service": controller.start_service,
                "/api/stop-service": controller.stop_service,
                "/api/stop": controller.stop,
            }
            action = action_map.get(parsed.path)
            if not action:
                _json_response(self, {"ok": False, "message": "Not found"}, HTTPStatus.NOT_FOUND)
                return
            message = action()
            _json_response(self, {"ok": True, "message": message, "status": controller.snapshot()})

    return Handler


def run_web_gui(open_browser: bool = True, port: int = 8765) -> None:
    url = f"http://127.0.0.1:{port}/"
    if _port_is_open(port):
        if open_browser:
            webbrowser.open(url)
            return
        raise RuntimeError(f"Web GUI 端口已被佔用: {url}")
    controller = WebGuiController()
    csrf_token = secrets.token_urlsafe(32)
    server = ReusableThreadingHTTPServer(("127.0.0.1", port), make_handler(controller, csrf_token))
    controller.logger.info("Web GUI 已啟動: %s", url)
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()


def run_web_gui_check() -> Path:
    controller = WebGuiController()
    controller.logger.info("Web GUI 自檢完成")
    controller.snapshot()
    return controller.config.log_dir / "auto_report.log"


COMPANY_ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200" role="img" aria-label="Company icon">
  <circle cx="100" cy="100" r="99" fill="#020202"/>
  <circle cx="100" cy="100" r="98.5" fill="none" stroke="#2b2f34" stroke-width="1"/>
  <g fill="#8BEA00">
    <path d="M39 69C55 49 75 40 100 40s45 9 61 29" fill="none" stroke="#8BEA00" stroke-width="14" stroke-linecap="round"/>
    <path d="M39 131c16 20 36 29 61 29s45-9 61-29" fill="none" stroke="#8BEA00" stroke-width="14" stroke-linecap="round"/>
    <path d="M11 100l13-13 13 13-13 13z"/>
    <path d="M163 100l13-13 13 13-13 13z"/>
    <path fill-rule="evenodd" d="M24 92h40c11 0 19-4 27-13 5-6 11-9 19-9s14 3 19 9c8 9 16 13 27 13h20v16h-20c-11 0-19 4-27 13-5 6-11 9-19 9s-14-3-19-9c-8-9-16-13-27-13H24zm76-12l25 20-25 20-25-20z"/>
    <path d="M100 92l10 8-10 8-10-8z"/>
  </g>
</svg>"""


CLOCK_FRAME_HTML = """<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    html, body {
      margin: 0;
      width: 100%;
      height: 100%;
      overflow: hidden;
      background: transparent;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Arial, sans-serif;
    }
    .stage {
      width: 100vw;
      height: 100vh;
      display: grid;
      place-items: center;
    }
    .clock-wrap {
      width: min(92vw, 180px);
      aspect-ratio: 1;
      position: relative;
    }
    svg { width: 100%; height: 100%; display: block; overflow: visible; }
    .source {
      position: absolute;
      left: 50%;
      bottom: -15px;
      transform: translateX(-50%);
      color: rgba(255,255,255,.86);
      font-size: 10px;
      font-weight: 800;
      letter-spacing: .02em;
      white-space: nowrap;
      text-shadow: 0 1px 4px rgba(0,0,0,.36);
    }
  </style>
</head>
<body>
  <div class="stage">
    <div class="clock-wrap">
      <svg viewBox="0 0 320 320" aria-label="HKO analog clock">
        <defs>
          <filter id="dropShadow" x="-20%" y="-20%" width="140%" height="140%">
            <feDropShadow dx="0" dy="12" stdDeviation="10" flood-color="#000" flood-opacity=".25"/>
          </filter>
          <radialGradient id="faceGrad" cx="44%" cy="35%" r="72%">
            <stop offset="0" stop-color="#ffffff"/>
            <stop offset=".58" stop-color="#f6f7fb"/>
            <stop offset="1" stop-color="#e3e6ed"/>
          </radialGradient>
          <linearGradient id="rimGrad" x1="65" y1="42" x2="256" y2="279" gradientUnits="userSpaceOnUse">
            <stop offset="0" stop-color="#050607"/>
            <stop offset=".48" stop-color="#232427"/>
            <stop offset="1" stop-color="#070809"/>
          </linearGradient>
        </defs>
        <circle id="outerRim" cx="160" cy="160" r="143" fill="url(#rimGrad)" filter="url(#dropShadow)"></circle>
        <circle id="innerFace" cx="160" cy="160" r="132" fill="url(#faceGrad)" stroke="#25272a" stroke-width="2.5"></circle>
        <g id="numbers" fill="#16191e" font-family="-apple-system, BlinkMacSystemFont, 'Segoe UI', Arial, sans-serif" font-size="32" font-weight="500" text-anchor="middle" dominant-baseline="middle"></g>
        <line id="hourHand" x1="160" y1="160" x2="160" y2="91" stroke="#191b1e" stroke-width="15" stroke-linecap="round"/>
        <line id="minuteHand" x1="160" y1="160" x2="160" y2="55" stroke="#191b1e" stroke-width="11" stroke-linecap="round"/>
        <circle cx="160" cy="160" r="12" fill="#191b1e"/>
        <circle cx="160" cy="160" r="4" fill="#34373d"/>
      </svg>
      <div id="source" class="source">HKO sync</div>
    </div>
  </div>
  <script>
    let offsetMs = 0;
    let syncedAt = 0;
    function placeNumbers() {
      const group = document.getElementById('numbers');
      for (let n = 1; n <= 12; n++) {
        const angle = (Math.PI * 2 * n / 12) - Math.PI / 2;
        const r = 108;
        const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
        text.setAttribute('x', (160 + Math.cos(angle) * r).toFixed(2));
        text.setAttribute('y', (160 + Math.sin(angle) * r).toFixed(2));
        text.textContent = String(n);
        group.appendChild(text);
      }
    }
    function render() {
      const now = new Date(Date.now() + offsetMs);
      const h = now.getHours() % 12;
      const m = now.getMinutes();
      const s = now.getSeconds() + now.getMilliseconds() / 1000;
      const hourAngle = (h + m / 60 + s / 3600) * 30;
      const minuteAngle = (m + s / 60) * 6;
      document.getElementById('hourHand').setAttribute('transform', `rotate(${hourAngle} 160 160)`);
      document.getElementById('minuteHand').setAttribute('transform', `rotate(${minuteAngle} 160 160)`);
      const age = syncedAt ? Math.round((Date.now() - syncedAt) / 1000) : 0;
      document.getElementById('source').textContent = syncedAt ? `HKO · ${age}s` : 'HKO sync';
      requestAnimationFrame(render);
    }
    async function syncHko() {
      const started = performance.now();
      try {
        const res = await fetch('/api/hko-time', { cache: 'no-store' });
        const data = await res.json();
        if (!data.ok || !Number.isFinite(data.offset_ms)) throw new Error(data.message || 'HKO unavailable');
        offsetMs = data.offset_ms - ((performance.now() - started) / 2);
        syncedAt = Date.now();
      } catch (err) {
        offsetMs = 0;
        syncedAt = 0;
        document.getElementById('source').textContent = 'Local HKT';
      }
    }
    placeNumbers();
    syncHko();
    render();
    setInterval(syncHko, 60000);
  </script>
</body>
</html>"""


HTML = """<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Anubis Auto Report</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f8fb;
      --panel: #ffffff;
      --ink: #132238;
      --muted: #607086;
      --line: #dce5ef;
      --navy: #102a43;
      --cyan: #00a6d6;
      --green: #10a05b;
      --green-soft: #dff8eb;
      --red: #d14343;
      --red-soft: #fde7e7;
      --amber: #b7791f;
      --amber-soft: #fff3cd;
      --off: #e8eef6;
      --shadow: 0 12px 34px rgba(16, 42, 67, .10);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      background:
        linear-gradient(135deg, rgba(0,166,214,.12), rgba(16,160,91,.08) 42%, rgba(255,255,255,0) 72%),
        var(--bg);
      color: var(--ink);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft JhengHei", Arial, sans-serif;
      position: relative;
      overflow-x: hidden;
    }
    body::before {
      content: "";
      position: fixed;
      inset: -28%;
      z-index: 0;
      pointer-events: none;
      opacity: .62;
      background:
        linear-gradient(112deg, transparent 0 23%, rgba(255,255,255,.48) 34%, rgba(129,229,255,.42) 41%, rgba(16,160,91,.28) 48%, transparent 61% 100%),
        linear-gradient(68deg, transparent 0 39%, rgba(255,255,255,.28) 47%, rgba(128,220,255,.22) 54%, transparent 67% 100%),
        radial-gradient(circle at 16% 22%, rgba(0,166,214,.28), transparent 32%),
        radial-gradient(circle at 86% 18%, rgba(16,160,91,.22), transparent 30%);
      background-size: 230% 230%, 210% 210%, 120% 120%, 120% 120%;
      animation: ambientFlow 13s ease-in-out infinite;
    }
    @keyframes ambientFlow {
      0% { transform: translate3d(-9%, -5%, 0) rotate(0deg); background-position: 0% 42%, 100% 58%, 0 0, 0 0; }
      50% { transform: translate3d(9%, 5%, 0) rotate(1.4deg); background-position: 100% 58%, 0% 44%, 0 0, 0 0; }
      100% { transform: translate3d(-9%, -5%, 0) rotate(0deg); background-position: 0% 42%, 100% 58%, 0 0, 0 0; }
    }
    .wrap { max-width: 1180px; margin: 0 auto; padding: 22px; position: relative; z-index: 1; }
    .hero {
      background: linear-gradient(135deg, #102a43 0%, #106b87 58%, #10a05b 100%);
      color: white;
      border-radius: 8px;
      padding: 22px;
      box-shadow: var(--shadow);
    }
    .hero-top {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 18px;
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 13px;
      min-width: 0;
    }
    .company-logo {
      width: 58px;
      height: 58px;
      border-radius: 50%;
      background: #030303;
      box-shadow: 0 10px 24px rgba(0,0,0,.24);
      padding: 0;
      border: 1px solid rgba(255,255,255,.28);
      flex: 0 0 auto;
    }
    h1 { margin: 0; font-size: 30px; letter-spacing: 0; }
    .subtle { color: rgba(255,255,255,.74); font-size: 13px; margin-top: 6px; }
    .clock {
      padding: 0;
      border: 1px solid rgba(255,255,255,.22);
      border-radius: 8px;
      background: rgba(255,255,255,.13);
      width: 184px;
      height: 198px;
      overflow: hidden;
      display: grid;
      place-items: center;
    }
    .clock-frame {
      width: 184px;
      height: 198px;
      border: 0;
      display: block;
      background: transparent;
    }
    .hero-grid {
      display: grid;
      grid-template-columns: 230px 1fr;
      gap: 18px;
      margin-top: 20px;
      align-items: stretch;
    }
    .status-badge {
      display: grid;
      place-items: center;
      min-height: 106px;
      border-radius: 8px;
      color: #06351e;
      background: #baf3d2;
      font-size: 30px;
      font-weight: 900;
      text-align: center;
      box-shadow: inset 0 0 0 1px rgba(255,255,255,.46);
    }
    .status-badge.idle { color: #233348; background: #e8eef6; }
    .status-badge.work { color: #07324d; background: #bfefff; }
    .status-badge.warn { color: #5f3b04; background: #ffe6a6; }
    .status-badge.bad { color: #7f1416; background: #ffd4d4; }
    .facts {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }
    .fact {
      border: 1px solid rgba(255,255,255,.18);
      border-radius: 8px;
      background: rgba(255,255,255,.12);
      padding: 12px;
      min-height: 62px;
    }
    .fact small { display: block; color: rgba(255,255,255,.70); margin-bottom: 6px; }
    .fact b { display: block; color: white; font-size: 16px; word-break: break-word; }
    .toolbar {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
      margin: 16px 0;
      align-items: center;
    }
    .progress-panel {
      display: none;
      align-items: center;
      gap: 12px;
      margin: -4px 0 16px;
      padding: 11px 13px;
      background: rgba(255,255,255,.82);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: 0 7px 18px rgba(16, 42, 67, .05);
    }
    .progress-panel.active { display: flex; }
    .progress-label {
      flex: 0 0 auto;
      min-width: 126px;
      color: #27405d;
      font-size: 13px;
      font-weight: 900;
    }
    .progress-track {
      position: relative;
      flex: 1 1 auto;
      height: 10px;
      overflow: hidden;
      border-radius: 999px;
      background: #dfe8f2;
      box-shadow: inset 0 1px 2px rgba(16,42,67,.12);
    }
    .progress-bar {
      position: absolute;
      inset: 0 auto 0 0;
      width: 42%;
      border-radius: inherit;
      background: linear-gradient(90deg, #00a6d6, #10a05b, #9be564);
      animation: progressSlide 1.25s ease-in-out infinite;
    }
    @keyframes progressSlide {
      0% { transform: translateX(-105%); }
      55% { transform: translateX(82%); }
      100% { transform: translateX(240%); }
    }
    .progress-note {
      flex: 0 0 auto;
      color: var(--muted);
      font-size: 12px;
      font-weight: 750;
    }
    button {
      border: 1px solid var(--line);
      background: white;
      color: var(--ink);
      border-radius: 8px;
      padding: 11px 14px;
      font-size: 14px;
      font-weight: 800;
      cursor: pointer;
      transition: transform .08s ease, background .16s ease, color .16s ease, border-color .16s ease;
    }
    button:hover { transform: translateY(-1px); }
    button:disabled { cursor: not-allowed; opacity: .55; transform: none; }
    button.btn-on:disabled { opacity: 1; cursor: default; }
    .btn-blue { background: #102a43; color: white; border-color: #102a43; }
    .btn-on { background: var(--green); color: white; border-color: var(--green); }
    .btn-off { background: #eef3f8; color: #42526b; border-color: #d3dde9; }
    .btn-danger { background: var(--red-soft); color: #9d1f23; border-color: #f7b9ba; }
    .btn-refresh { margin-left: auto; background: #e8fbff; color: #075a72; border-color: #a6e5f2; }
    .cards {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 14px;
      margin-bottom: 14px;
    }
    .card {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 15px;
      box-shadow: 0 7px 18px rgba(16, 42, 67, .05);
    }
    .card h2 { margin: 0 0 10px; font-size: 17px; }
    .card p { margin: 7px 0; color: var(--muted); line-height: 1.45; overflow-wrap: anywhere; }
    .pill {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      border-radius: 999px;
      padding: 7px 10px;
      font-weight: 900;
      font-size: 13px;
    }
    .pill::before {
      content: "";
      width: 9px;
      height: 9px;
      border-radius: 50%;
      background: currentColor;
    }
    .pill.on { color: #087240; background: var(--green-soft); }
    .pill.off { color: #526173; background: var(--off); }
    .pill.bad { color: #9d1f23; background: var(--red-soft); }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      overflow: hidden;
      box-shadow: 0 7px 18px rgba(16, 42, 67, .05);
    }
    .panel-head {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      align-items: center;
      padding: 13px 15px;
      border-bottom: 1px solid var(--line);
    }
    .panel-head h2 { margin: 0; font-size: 17px; }
    .settings-panel { margin-bottom: 14px; }
    .settings-body {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 16px;
      padding: 16px;
    }
    .field { min-width: 0; }
    .field.full { grid-column: 1 / -1; }
    .field.equal-field {
      display: grid;
      grid-template-rows: auto 44px minmax(18px, auto);
      align-content: start;
    }
    .field label {
      display: block;
      margin-bottom: 7px;
      color: #263b54;
      font-size: 13px;
      font-weight: 850;
    }
    .field input[type="text"],
    .field input[type="number"],
    .field textarea {
      width: 100%;
      border: 1px solid #cbd8e6;
      border-radius: 8px;
      background: #fbfdff;
      color: var(--ink);
      padding: 11px 12px;
      font: inherit;
      outline: none;
      transition: border-color .15s ease, box-shadow .15s ease, background .15s ease;
    }
    .field input[type="number"] { height: 44px; }
    .field textarea { min-height: 92px; resize: vertical; line-height: 1.45; }
    .field input:focus, .field textarea:focus {
      border-color: #10a05b;
      background: #fff;
      box-shadow: 0 0 0 3px rgba(16,160,91,.13);
    }
    .template-vars { margin-top: 7px; color: var(--muted); font-size: 12px; line-height: 1.5; }
    .template-vars code {
      display: inline-block;
      margin: 2px 3px 0 0;
      padding: 2px 5px;
      border-radius: 5px;
      background: #edf3f8;
      color: #26445f;
      font-size: 11px;
    }
    .toggle-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      height: 44px;
      min-height: 44px;
      padding: 0 12px;
      border: 1px solid #d7e2ed;
      border-radius: 8px;
      background: #f8fbfd;
    }
    .toggle-copy { color: #263b54; font-size: 13px; font-weight: 750; }
    .toggle-row input { width: 20px; height: 20px; accent-color: var(--green); flex: 0 0 auto; }
    .settings-actions {
      grid-column: 1 / -1;
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 10px;
      padding-top: 2px;
    }
    .settings-state { color: var(--muted); font-size: 13px; font-weight: 750; }
    .settings-state.dirty { color: var(--amber); }
    .settings-state.saved { color: var(--green); }
    .log {
      height: 280px;
      overflow: auto;
      white-space: pre-wrap;
      background: #0c1627;
      color: #e9eef7;
      padding: 14px;
      font: 13px/1.55 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }
    .toast {
      position: fixed;
      right: 18px;
      bottom: 18px;
      max-width: 420px;
      background: #102a43;
      color: white;
      border-radius: 8px;
      padding: 12px 14px;
      box-shadow: var(--shadow);
      opacity: 0;
      transform: translateY(10px);
      pointer-events: none;
      transition: opacity .16s ease, transform .16s ease;
      font-weight: 750;
    }
    .toast.show { opacity: 1; transform: translateY(0); }
    @media (max-width: 860px) {
      .wrap { padding: 14px; }
      .hero-top, .hero-grid { display: block; }
      .brand { align-items: flex-start; }
      .company-logo { width: 50px; height: 50px; }
      h1 { font-size: 25px; }
      .clock { margin-top: 12px; width: 172px; height: 186px; }
      .clock-frame { width: 172px; height: 186px; }
      .status-badge { margin-bottom: 14px; }
      .facts, .cards { grid-template-columns: 1fr; }
      .settings-body { grid-template-columns: 1fr; padding: 14px; }
      .field.full, .settings-actions { grid-column: 1; }
      .settings-actions { align-items: stretch; flex-direction: column; }
      .settings-actions button { width: 100%; }
      .btn-refresh { margin-left: 0; }
      .toolbar button { flex: 1 1 100%; }
      .progress-panel { display: none; align-items: stretch; flex-direction: column; gap: 8px; }
      .progress-panel.active { display: flex; }
      .progress-label, .progress-note { flex: 0 0 auto; }
    }
  </style>
</head>
<body>
  <main class="wrap">
    <section class="hero">
      <div class="hero-top">
        <div class="brand">
          <img class="company-logo" src="/company-logo.png" alt="Company icon">
          <div>
            <h1>Anubis Auto Report 控制台</h1>
            <div id="version" class="subtle">載入中...</div>
          </div>
        </div>
        <div class="clock">
          <iframe id="clockFrame" class="clock-frame" src="/clock-frame" title="HKO 類比時鐘"></iframe>
        </div>
      </div>
      <div class="hero-grid">
        <div id="statusBadge" class="status-badge idle">連線中</div>
        <div class="facts">
          <div class="fact"><small>目前模式</small><b id="mode">-</b></div>
          <div class="fact"><small>背景 24/7 服務</small><b id="serviceSummary">檢查中</b></div>
          <div class="fact"><small>最後更新</small><b id="lastUpdate">-</b></div>
          <div class="fact"><small>最後事件</small><b id="lastEvent">-</b></div>
        </div>
      </div>
    </section>

    <div class="toolbar">
      <button id="btnRunOnce" class="btn-blue" onclick="postAction('/api/run-once')">立即刷新並發送</button>
      <button id="btnStartService" class="btn-off" onclick="postAction('/api/start-service')">啟動 24/7 服務</button>
      <button id="btnStopService" class="btn-danger" onclick="postAction('/api/stop-service')">停止 24/7 服務</button>
      <button id="btnStopTask" class="btn-off" onclick="postAction('/api/stop')">停止本頁任務</button>
      <button class="btn-refresh" onclick="refreshStatus(true)">刷新狀態</button>
    </div>
    <div id="operationProgress" class="progress-panel" aria-live="polite">
      <div id="progressLabel" class="progress-label">正在處理</div>
      <div class="progress-track"><div class="progress-bar"></div></div>
      <div id="progressNote" class="progress-note">請稍候...</div>
    </div>

    <section class="cards">
      <div class="card">
        <h2>服務狀態</h2>
        <span id="servicePill" class="pill off">檢查中</span>
        <p>Label: <b id="serviceLabel">-</b></p>
        <p>PID: <b id="servicePid">-</b></p>
      </div>
      <div class="card">
        <h2>排程</h2>
        <p>刷新 Chrome: 每 <b id="refreshInterval">-</b> 秒</p>
        <p>每日自動發送: <b id="dailyTime">-</b> HKT</p>
        <p>循環間隔: <b id="reportInterval">-</b> 秒</p>
      </div>
      <div class="card">
        <h2>路徑</h2>
        <p>設定檔: <b id="envPath">-</b></p>
        <p>輸出: <b id="outputDir">-</b></p>
      </div>
    </section>

    <section class="panel settings-panel">
      <div class="panel-head">
        <h2>Telegram 與驗證設定</h2>
        <span id="settingsState" class="settings-state">載入中...</span>
      </div>
      <form id="settingsForm" class="settings-body" onsubmit="saveSettings(event)">
        <div class="field full">
          <label for="telegramFilename">Telegram Excel 文件名</label>
          <input id="telegramFilename" name="telegram_filename_template" type="text" maxlength="160" autocomplete="off" spellcheck="false" required>
          <div class="template-vars">可用變數：<code>{date}</code><code>{time}</code><code>{datetime}</code><code>{status}</code><code>{total_in}</code><code>{total_out}</code><code>{net}</code></div>
        </div>
        <div class="field full">
          <label for="telegramCaption">隨 Excel 文件發送的文字</label>
          <textarea id="telegramCaption" name="telegram_document_caption" maxlength="900"></textarea>
          <div class="template-vars">文字同樣支援以上變數；留空時只發送文件。</div>
        </div>
        <div class="field equal-field">
          <label for="sendPreviewImage">發送報表預覽截圖</label>
          <div class="toggle-row">
            <div class="toggle-copy">發送 PNG 預覽圖片</div>
            <input id="sendPreviewImage" name="send_preview_image" type="checkbox">
          </div>
          <div class="template-vars">關閉後只發送 Excel 文件。</div>
        </div>
        <div class="field equal-field">
          <label for="otpReminderInterval">等待驗證碼提醒間隔（秒）</label>
          <input id="otpReminderInterval" name="otp_reminder_interval_seconds" type="number" min="0" max="1800" step="30" inputmode="numeric">
          <div class="template-vars">0 代表只提示一次；其他數值最少 30 秒。</div>
        </div>
        <div class="settings-actions">
          <span id="settingsHint" class="settings-state">保存後立即生效</span>
          <button id="btnSaveSettings" class="btn-blue" type="submit">保存設定</button>
        </div>
      </form>
    </section>

    <section class="panel">
      <div class="panel-head">
        <h2>即時日誌</h2>
        <span id="connectionPill" class="pill on">Web GUI 連線正常</span>
      </div>
      <div id="logs" class="log">載入中...</div>
    </section>
  </main>
  <div id="toast" class="toast"></div>
  <script>
    const csrfToken = '__CSRF_TOKEN__';
    let busy = false;
    let settingsLoaded = false;
    let progressHideTimer = null;
    function setProgress(active, label = '正在處理', note = '請稍候...') {
      const panel = document.getElementById('operationProgress');
      document.getElementById('progressLabel').textContent = label;
      document.getElementById('progressNote').textContent = note;
      window.clearTimeout(progressHideTimer);
      if (active) {
        panel.classList.add('active');
      } else {
        progressHideTimer = window.setTimeout(() => panel.classList.remove('active'), 450);
      }
    }
    function updateProgress(data) {
      const statusText = `${data.status || ''} ${data.mode || ''} ${data.last_event || ''}`;
      const active = busy || !!data.worker_alive || /正在|執行中|準備啟動/.test(statusText);
      if (active) {
        const label = data.worker_alive ? (data.mode || '任務執行中') : '正在處理';
        const note = data.last_event || data.status || '請稍候...';
        setProgress(true, label, note);
      } else {
        setProgress(false);
      }
    }
    function showToast(message) {
      const toast = document.getElementById('toast');
      toast.textContent = message;
      toast.classList.add('show');
      window.clearTimeout(showToast.timer);
      showToast.timer = window.setTimeout(() => toast.classList.remove('show'), 2800);
    }
    function setText(id, value) {
      document.getElementById(id).textContent = value || '-';
    }
    function setSettingsState(text, className = '') {
      const state = document.getElementById('settingsState');
      state.textContent = text;
      state.className = `settings-state ${className}`.trim();
    }
    function markSettingsDirty() {
      if (settingsLoaded) setSettingsState('未保存', 'dirty');
    }
    function validateSettingsForm() {
      const filename = document.getElementById('telegramFilename').value;
      const caption = document.getElementById('telegramCaption').value;
      const interval = Number(document.getElementById('otpReminderInterval').value || 0);
      const allowedFields = new Set(['date', 'time', 'datetime', 'status', 'total_in', 'total_out', 'net']);
      const fields = [...filename.matchAll(/\\{([^{}]+)\\}/g), ...caption.matchAll(/\\{([^{}]+)\\}/g)];
      if ([...filename].some(char => !(char.match(/[\\p{L}\\p{N}]/u) || ' _-.()[]{}'.includes(char)))) {
        throw new Error('文件名只可包含文字、數字、空格、底線、連字號、括號及句點。');
      }
      if (filename.includes('..') || filename.startsWith('.')) {
        throw new Error('文件名不可包含連續句點，也不可用句點開頭。');
      }
      if ((filename.match(/\\{/g) || []).length !== (filename.match(/\\}/g) || []).length ||
          (caption.match(/\\{/g) || []).length !== (caption.match(/\\}/g) || []).length) {
        throw new Error('模板括號格式不正確。');
      }
      for (const match of fields) {
        if (!allowedFields.has(match[1])) throw new Error(`不支援模板變數 {${match[1]}}。`);
      }
      const unsafe = /<\\s*\\/?\\s*[a-z][^>]*>|javascript\\s*:|\\bunion\\s+(?:all\\s+)?select\\b|\\bdrop\\s+table\\b|\\binsert\\s+into\\b|\\bdelete\\s+from\\b|(?:['"]\\s*(?:or|and)\\s+\\d+\\s*=\\s*\\d+)/i;
      if (unsafe.test(filename) || unsafe.test(caption)) {
        throw new Error('輸入內容包含不允許的程式碼或注入語句。');
      }
      if (!Number.isInteger(interval) || (interval !== 0 && (interval < 30 || interval > 1800))) {
        throw new Error('驗證碼提醒間隔必須為 0，或介乎 30 至 1800 秒。');
      }
    }
    async function loadSettings() {
      try {
        const res = await fetch('/api/settings', { cache: 'no-store' });
        const data = await res.json();
        if (!res.ok || !data.ok) throw new Error(data.message || '無法讀取設定');
        const settings = data.settings || {};
        document.getElementById('telegramFilename').value = settings.telegram_filename_template || '';
        document.getElementById('telegramCaption').value = settings.telegram_document_caption || '';
        document.getElementById('sendPreviewImage').checked = !!settings.send_preview_image;
        document.getElementById('otpReminderInterval').value = settings.otp_reminder_interval_seconds ?? 120;
        settingsLoaded = true;
        setSettingsState('已保存', 'saved');
      } catch (err) {
        setSettingsState('讀取失敗', 'dirty');
        showToast('設定讀取失敗：' + err.message);
      }
    }
    async function saveSettings(event) {
      event.preventDefault();
      const button = document.getElementById('btnSaveSettings');
      try {
        validateSettingsForm();
      } catch (err) {
        setSettingsState('輸入無效', 'dirty');
        showToast(err.message);
        return;
      }
      button.disabled = true;
      setSettingsState('保存中...');
      const payload = {
        telegram_filename_template: document.getElementById('telegramFilename').value,
        telegram_document_caption: document.getElementById('telegramCaption').value,
        send_preview_image: document.getElementById('sendPreviewImage').checked,
        otp_reminder_interval_seconds: Number(document.getElementById('otpReminderInterval').value || 0)
      };
      try {
        const res = await fetch('/api/settings', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
          body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (!res.ok || !data.ok) throw new Error(data.message || '保存失敗');
        setSettingsState('已保存', 'saved');
        showToast(data.message || '設定已保存');
      } catch (err) {
        setSettingsState('保存失敗', 'dirty');
        showToast('設定保存失敗：' + err.message);
      } finally {
        button.disabled = false;
      }
    }
    function setBadge(data) {
      const badge = document.getElementById('statusBadge');
      badge.textContent = data.status || '未知';
      badge.className = 'status-badge';
      const text = (data.status || '') + ' ' + (data.mode || '');
      if (/失敗|異常|錯誤/.test(text)) badge.classList.add('bad');
      else if (/停止/.test(text)) badge.classList.add('warn');
      else if (/執行|啟動|服務|循環|準備/.test(text) || data.worker_alive) badge.classList.add('work');
      else badge.classList.add('idle');
    }
    function updateButtons(data) {
      const serviceOn = !!data.service_running;
      const localBusy = !!data.worker_alive || busy;
      const start = document.getElementById('btnStartService');
      const stop = document.getElementById('btnStopService');
      const runOnce = document.getElementById('btnRunOnce');
      const stopTask = document.getElementById('btnStopTask');
      start.className = serviceOn ? 'btn-on' : 'btn-off';
      start.textContent = serviceOn ? '24/7 服務已啟動' : '啟動 24/7 服務';
      start.disabled = serviceOn || busy;
      stop.disabled = !serviceOn || busy;
      stop.className = serviceOn ? 'btn-danger' : 'btn-off';
      runOnce.disabled = localBusy;
      runOnce.textContent = localBusy ? '正在執行...' : '立即刷新並發送';
      stopTask.disabled = !data.worker_alive;
      updateProgress(data);
    }
    function updateService(data) {
      const pill = document.getElementById('servicePill');
      const conflict = !!data.telegram_conflict;
      pill.className = conflict ? 'pill bad' : (data.service_running ? 'pill on' : 'pill off');
      pill.textContent = conflict ? 'TG 409 衝突' : (data.service_running ? 'ON / 運行中' : 'OFF / 已停止');
      setText('serviceSummary', conflict ? 'Telegram polling conflict' : (data.service_running ? `ON / PID ${data.service_pid || '-'}` : 'OFF / 未運行'));
      setText('serviceLabel', data.service_label);
      setText('servicePid', data.service_pid || data.service_state || '-');
    }
    async function refreshStatus(manual = false) {
      try {
        if (manual) setProgress(true, '刷新狀態', '正在讀取最新狀態...');
        const res = await fetch('/api/status', { cache: 'no-store' });
        const data = await res.json();
        setText('version', data.version);
        setBadge(data);
        updateService(data);
        updateButtons(data);
        setText('mode', data.mode);
        setText('lastUpdate', data.last_update);
        setText('lastEvent', data.last_event);
        setText('refreshInterval', data.refresh_interval);
        setText('dailyTime', data.daily_report_time);
        setText('reportInterval', data.report_interval);
        setText('envPath', data.env_path);
        setText('outputDir', data.output_dir);
        document.getElementById('logs').textContent = (data.logs || []).join('\\n');
        const conn = document.getElementById('connectionPill');
        conn.className = 'pill on';
        conn.textContent = 'Web GUI 連線正常';
        if (manual) showToast('狀態已刷新');
      } catch (err) {
        const conn = document.getElementById('connectionPill');
        conn.className = 'pill bad';
        conn.textContent = 'Web GUI 連線中斷';
        document.getElementById('statusBadge').className = 'status-badge bad';
        document.getElementById('statusBadge').textContent = '離線';
      }
    }
    async function postAction(path) {
      if (busy) return;
      busy = true;
      const actionName = {
        '/api/run-once': '製作並發送報表',
        '/api/start-service': '啟動 24/7 服務',
        '/api/stop-service': '停止 24/7 服務',
        '/api/stop': '停止本頁任務'
      }[path] || '執行操作';
      setProgress(true, actionName, '正在送出指令...');
      updateButtons({ worker_alive: true, service_running: document.getElementById('servicePill').classList.contains('on'), mode: actionName, last_event: '正在送出指令...' });
      try {
        const res = await fetch(path, { method: 'POST', headers: { 'X-CSRF-Token': csrfToken } });
        const data = await res.json();
        showToast(data.message || (data.ok ? '已送出指令' : '執行失敗'));
      } catch (err) {
        showToast('指令送出失敗：' + err);
      } finally {
        busy = false;
        await refreshStatus();
      }
    }
    document.getElementById('settingsForm').addEventListener('input', markSettingsDirty);
    document.getElementById('settingsForm').addEventListener('change', markSettingsDirty);
    loadSettings();
    refreshStatus();
    setInterval(refreshStatus, 2000);
  </script>
</body>
</html>
"""
