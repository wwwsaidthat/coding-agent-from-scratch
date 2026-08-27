"""Local full-stack web application for the coding agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import threading
import time
from typing import Any, Mapping
from urllib.parse import unquote, urlparse
from uuid import uuid4

from .agent import Agent, AgentCancelledError, AgentError
from .cli import build_registry
from .config import Settings
from .models import DeepSeekChatModel, ModelAPIError, ScriptedDemoModel


STATIC_ROOT = Path(__file__).resolve().parent / "web"
MAX_REQUEST_BYTES = 64_000
MAX_EVENT_STRING = 24_000
MAX_RUNS = 25


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _safe_value(value: Any) -> Any:
    if isinstance(value, str):
        if len(value) > MAX_EVENT_STRING:
            return value[:MAX_EVENT_STRING] + "\n… output truncated by web view …"
        return value
    if isinstance(value, Mapping):
        return {str(key): _safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_value(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


@dataclass(slots=True)
class RunRecord:
    id: str
    task: str
    workspace: str
    demo: bool
    max_steps: int
    status: str = "queued"
    created_at: str = field(default_factory=_now)
    started_at: str | None = None
    finished_at: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    final_output: str | None = None
    error: str | None = None
    steps: int = 0
    tool_calls: int = 0
    next_event_seq: int = field(default=1, repr=False)
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)


class RunStore:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._runs: dict[str, RunRecord] = {}
        self._lock = threading.RLock()

    def create(
        self,
        *,
        task: str,
        workspace: Path,
        demo: bool,
        max_steps: int,
    ) -> RunRecord:
        record = RunRecord(
            id=uuid4().hex,
            task=task,
            workspace=str(workspace),
            demo=demo,
            max_steps=max_steps,
        )
        with self._lock:
            self._runs[record.id] = record
            self._append_locked(record, "queued", {"message": "任务已进入执行队列"})
            self._prune_locked()
        threading.Thread(
            target=self._run_worker,
            args=(record.id,),
            name=f"agent-run-{record.id[:8]}",
            daemon=True,
        ).start()
        return record

    def get_public(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._runs.get(run_id)
            return self._public_locked(record) if record else None

    def cancel(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                return None
            if record.status in {"queued", "running"} and not record.cancel_event.is_set():
                record.cancel_event.set()
                self._append_locked(
                    record,
                    "cancel_requested",
                    {"message": "已请求停止；正在进行的模型请求返回后将终止"},
                )
            return self._public_locked(record)

    def _run_worker(self, run_id: str) -> None:
        with self._lock:
            record = self._runs[run_id]
            record.status = "running"
            record.started_at = _now()
            self._append_locked(record, "started", {"message": "Agent 开始执行"})

        try:
            workspace = Path(record.workspace)
            registry = build_registry(workspace, self.settings.command_timeout)
            model = ScriptedDemoModel() if record.demo else DeepSeekChatModel(self.settings)

            def on_event(event: str, payload: Mapping[str, Any]) -> None:
                with self._lock:
                    current = self._runs[run_id]
                    if event == "model_request":
                        current.steps = max(current.steps, int(payload.get("step", 0)))
                    elif event == "tool_start":
                        current.tool_calls += 1
                    self._append_locked(current, event, payload)

            agent = Agent(
                model,
                registry,
                max_steps=record.max_steps,
                on_event=on_event,
                should_stop=record.cancel_event.is_set,
            )
            result = agent.run(record.task)
            with self._lock:
                current = self._runs[run_id]
                current.status = "completed"
                current.finished_at = _now()
                current.final_output = result.final_output
                current.steps = result.steps
                current.tool_calls = result.tool_calls
        except AgentCancelledError:
            with self._lock:
                current = self._runs[run_id]
                current.status = "cancelled"
                current.finished_at = _now()
                current.error = "任务已由用户停止"
                self._append_locked(current, "cancelled", {"message": current.error})
        except (ValueError, AgentError, ModelAPIError) as exc:
            with self._lock:
                current = self._runs[run_id]
                current.status = "error"
                current.finished_at = _now()
                current.error = str(exc)
                self._append_locked(current, "error", {"message": str(exc)})
        except Exception as exc:  # Prevent a worker crash from disappearing silently.
            with self._lock:
                current = self._runs[run_id]
                current.status = "error"
                current.finished_at = _now()
                current.error = f"{type(exc).__name__}: {exc}"
                self._append_locked(current, "error", {"message": current.error})

    def _append_locked(
        self, record: RunRecord, event_type: str, payload: Mapping[str, Any]
    ) -> None:
        record.events.append(
            {
                "seq": record.next_event_seq,
                "type": event_type,
                "timestamp": _now(),
                "payload": _safe_value(payload),
            }
        )
        record.next_event_seq += 1
        if len(record.events) > 400:
            record.events[:] = record.events[-400:]

    def _public_locked(self, record: RunRecord) -> dict[str, Any]:
        return {
            "id": record.id,
            "task": record.task,
            "workspace": record.workspace,
            "demo": record.demo,
            "max_steps": record.max_steps,
            "status": record.status,
            "created_at": record.created_at,
            "started_at": record.started_at,
            "finished_at": record.finished_at,
            "events": list(record.events),
            "final_output": record.final_output,
            "error": record.error,
            "steps": record.steps,
            "tool_calls": record.tool_calls,
        }

    def _prune_locked(self) -> None:
        if len(self._runs) <= MAX_RUNS:
            return
        removable = [
            record
            for record in self._runs.values()
            if record.status not in {"queued", "running"}
        ]
        for record in sorted(removable, key=lambda item: item.created_at):
            if len(self._runs) <= MAX_RUNS:
                break
            self._runs.pop(record.id, None)


class LocalWebApplication:
    def __init__(self, settings: Settings, default_workspace: Path) -> None:
        self.settings = settings
        self.default_workspace = default_workspace.resolve()
        self.runs = RunStore(settings)

    def config(self) -> dict[str, Any]:
        return {
            "api_configured": bool(self.settings.api_key),
            "model": self.settings.model,
            "thinking": self.settings.thinking,
            "reasoning_effort": self.settings.reasoning_effort,
            "default_workspace": str(self.default_workspace),
            "max_steps": self.settings.max_steps,
        }

    def create_run(self, body: Mapping[str, Any]) -> dict[str, Any]:
        task = body.get("task")
        if not isinstance(task, str) or not task.strip():
            raise WebRequestError("Prompt 不能为空")
        if len(task) > 20_000:
            raise WebRequestError("Prompt 不能超过 20,000 个字符")

        raw_workspace = body.get("workspace", str(self.default_workspace))
        if not isinstance(raw_workspace, str) or not raw_workspace.strip():
            raise WebRequestError("工作区路径不能为空")
        workspace = Path(raw_workspace).expanduser()
        if not workspace.is_absolute():
            workspace = self.default_workspace / workspace
        workspace = workspace.resolve()
        if not workspace.is_dir():
            raise WebRequestError("工作区不存在或不是文件夹")

        demo = body.get("demo", False)
        if not isinstance(demo, bool):
            raise WebRequestError("demo 必须是布尔值")
        if not demo and not self.settings.api_key:
            raise WebRequestError("尚未配置 DEEPSEEK_API_KEY，可先启用离线演示")

        max_steps = body.get("max_steps", self.settings.max_steps)
        if isinstance(max_steps, bool) or not isinstance(max_steps, int):
            raise WebRequestError("最大步骤必须是整数")
        if not 1 <= max_steps <= 50:
            raise WebRequestError("最大步骤必须在 1 到 50 之间")

        record = self.runs.create(
            task=task.strip(),
            workspace=workspace,
            demo=demo,
            max_steps=max_steps,
        )
        public = self.runs.get_public(record.id)
        assert public is not None
        return public


class WebRequestError(ValueError):
    pass


def make_handler(application: LocalWebApplication) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "LoopCoder/0.2"

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/api/health":
                self._json({"ok": True, "time": _now()})
                return
            if parsed.path == "/api/config":
                self._json(application.config())
                return
            if parsed.path.startswith("/api/runs/"):
                run_id = parsed.path.removeprefix("/api/runs/").strip("/")
                record = application.runs.get_public(run_id)
                if record is None:
                    self._json({"error": "找不到该任务"}, HTTPStatus.NOT_FOUND)
                else:
                    self._json(record)
                return
            if parsed.path == "/favicon.ico":
                self.send_response(HTTPStatus.NO_CONTENT)
                self.end_headers()
                return
            self._serve_static(parsed.path)

        def do_POST(self) -> None:  # noqa: N802
            if self.headers.get("X-Agent-Client") != "web-ui":
                self._json({"error": "缺少本地客户端标识"}, HTTPStatus.FORBIDDEN)
                return
            parsed = urlparse(self.path)
            try:
                body = self._read_json()
                if parsed.path == "/api/runs":
                    self._json(application.create_run(body), HTTPStatus.ACCEPTED)
                    return
                if parsed.path.startswith("/api/runs/") and parsed.path.endswith("/cancel"):
                    run_id = parsed.path.removeprefix("/api/runs/").removesuffix(
                        "/cancel"
                    ).strip("/")
                    record = application.runs.cancel(run_id)
                    if record is None:
                        self._json({"error": "找不到该任务"}, HTTPStatus.NOT_FOUND)
                    else:
                        self._json(record)
                    return
                self._json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)
            except WebRequestError as exc:
                self._json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)

        def _read_json(self) -> Mapping[str, Any]:
            if self.headers.get_content_type() != "application/json":
                raise WebRequestError("请求必须使用 application/json")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise WebRequestError("无效的 Content-Length") from exc
            if length <= 0 or length > MAX_REQUEST_BYTES:
                raise WebRequestError("请求内容为空或过大")
            try:
                value = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise WebRequestError("请求不是有效 JSON") from exc
            if not isinstance(value, Mapping):
                raise WebRequestError("请求 JSON 必须是对象")
            return value

        def _serve_static(self, request_path: str) -> None:
            routes = {
                "/": "index.html",
                "/index.html": "index.html",
                "/assets/styles.css": "styles.css",
                "/assets/app.js": "app.js",
            }
            filename = routes.get(unquote(request_path))
            if filename is None:
                self._json({"error": "页面不存在"}, HTTPStatus.NOT_FOUND)
                return
            path = STATIC_ROOT / filename
            try:
                content = path.read_bytes()
            except FileNotFoundError:
                self._json({"error": "前端资源缺失"}, HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self._security_headers()
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(content)

        def _json(self, value: Mapping[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
            content = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self._security_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(content)

        def _security_headers(self) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'",
            )

        def log_message(self, format: str, *args: Any) -> None:
            del format, args

    return Handler


def create_http_server(
    settings: Settings,
    *,
    host: str,
    port: int,
    default_workspace: Path,
) -> ThreadingHTTPServer:
    if not 0 <= port <= 65_535:
        raise ValueError("port must be between 0 and 65535")
    application = LocalWebApplication(settings, default_workspace)
    return ThreadingHTTPServer((host, port), make_handler(application))


def run_web_server(
    settings: Settings,
    *,
    host: str,
    port: int,
    default_workspace: Path,
) -> int:
    server = create_http_server(
        settings,
        host=host,
        port=port,
        default_workspace=default_workspace,
    )
    actual_host, actual_port = server.server_address[:2]
    print(f"\nLoopCoder Web is running at http://{actual_host}:{actual_port}")
    print("Press Ctrl+C to stop.\n")
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
        time.sleep(0.05)
    return 0
