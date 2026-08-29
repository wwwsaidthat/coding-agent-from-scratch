"""Local full-stack web application for the coding agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import base64
import binascii
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import re
import shutil
import threading
import time
from typing import Any, Mapping, Sequence
from urllib.parse import unquote, urlparse
from uuid import uuid4

from .agent import Agent, AgentCancelledError, AgentError
from .cli import build_registry
from .config import Settings
from .conversation import Conversation
from .models import DeepSeekChatModel, ModelAPIError, ScriptedDemoModel
from .prompts import system_prompt_for_models
from .tools.external import (
    MAX_IMAGE_BYTES,
    MAX_PDF_BYTES,
    detect_image_mime,
    detect_pdf_mime,
)


STATIC_ROOT = Path(__file__).resolve().parent / "web"
MAX_REQUEST_BYTES = 64_000
MAX_UPLOAD_REQUEST_BYTES = 14_000_000
MAX_PDF_UPLOAD_REQUEST_BYTES = 28_000_000
MAX_EVENT_STRING = 80_000
MAX_TRACE_STRING = 2_500_000
MAX_RUNS = 25
MAX_SESSIONS_RETURNED = 50
SESSION_STATE_VERSION = 1
ACTIVE_STATUSES = {"queued", "running", "waiting_approval"}
PROJECT_RULE_FILES = ("AGENTS.md", "PROJECT_RULES.md")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _safe_value(value: Any) -> Any:
    return _bounded_value(value, MAX_EVENT_STRING, "web view")


def _trace_value(value: Any) -> Any:
    return _bounded_value(value, MAX_TRACE_STRING, "persistent trace")


def _bounded_value(value: Any, limit: int, label: str) -> Any:
    if isinstance(value, str):
        if len(value) > limit:
            return value[:limit] + f"\n… output truncated by {label} …"
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _bounded_value(item, limit, label)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_bounded_value(item, limit, label) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def _load_project_rules(workspace: Path) -> str:
    sections: list[str] = []
    remaining = 20_000
    for name in PROJECT_RULE_FILES:
        path = workspace / name
        if not path.is_file() or remaining <= 0:
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="replace")[:remaining]
        except OSError:
            continue
        sections.append(f"# {name}\n{content}")
        remaining -= len(content)
    return "\n\n".join(sections)


@dataclass(slots=True)
class PendingApproval:
    id: str
    proposal: dict[str, Any]
    created_at: str = field(default_factory=_now)
    decision: bool | None = None
    event: threading.Event = field(default_factory=threading.Event, repr=False)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "proposal": _safe_value(self.proposal),
        }


@dataclass(slots=True)
class RunRecord:
    id: str
    session_id: str
    turn: int
    task: str
    workspace: str
    demo: bool
    max_steps: int
    status: str = "queued"
    created_at: str = field(default_factory=_now)
    started_at: str | None = None
    finished_at: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    trace_events: list[dict[str, Any]] = field(default_factory=list, repr=False)
    final_output: str | None = None
    error: str | None = None
    steps: int = 0
    tool_calls: int = 0
    model_usage: dict[str, int | float] = field(default_factory=dict)
    duration_ms: float | None = None
    final_test_result: dict[str, Any] | None = None
    plan: list[dict[str, str]] = field(default_factory=list)
    plan_explanation: str = ""
    pending_approval: PendingApproval | None = None
    next_event_seq: int = field(default=1, repr=False)
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False)
    started_clock: float | None = field(default=None, repr=False)


@dataclass(slots=True)
class SessionRecord:
    id: str
    title: str
    workspace: str
    demo: bool
    max_steps: int
    conversation: Conversation
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    messages: list[dict[str, Any]] = field(default_factory=list)
    active_run_id: str | None = None


class RunStore:
    def __init__(self, settings: Settings, state_dir: Path) -> None:
        self.settings = settings
        self.state_dir = state_dir.resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.trace_dir = self.state_dir.parent / "traces"
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        self._runs: dict[str, RunRecord] = {}
        self._sessions: dict[str, SessionRecord] = {}
        self._lock = threading.RLock()
        self._load_sessions()

    def create_session(
        self,
        *,
        workspace: Path,
        demo: bool,
        max_steps: int,
    ) -> SessionRecord:
        session = SessionRecord(
            id=uuid4().hex,
            title="新会话",
            workspace=str(workspace),
            demo=demo,
            max_steps=max_steps,
            conversation=Conversation(
                self._system_prompt(),
                max_context_tokens=self.settings.context_tokens,
                project_rules=_load_project_rules(workspace),
            ),
        )
        with self._lock:
            self._sessions[session.id] = session
            self._save_session_locked(session)
        return session

    def list_sessions_public(self) -> list[dict[str, Any]]:
        with self._lock:
            sessions = sorted(
                self._sessions.values(), key=lambda item: item.updated_at, reverse=True
            )[:MAX_SESSIONS_RETURNED]
            return [self._session_summary_locked(session) for session in sessions]

    def _system_prompt(self) -> str:
        return system_prompt_for_models(
            self.settings.model,
            self.settings.qwen_model,
        )

    def get_session_public(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            session = self._sessions.get(session_id)
            return self._session_public_locked(session) if session else None

    def delete_session(self, session_id: str) -> dict[str, Any] | None:
        """Permanently remove one inactive session and its app-owned artifacts."""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            if session.active_run_id is not None:
                active = self._runs.get(session.active_run_id)
                if active is not None and active.status in ACTIVE_STATUSES:
                    raise WebRequestError("会话仍在运行或等待确认，请先停止任务再删除")
            if not re.fullmatch(r"[0-9a-f]{32}", session.id):
                raise WebRequestError("会话标识无效，拒绝删除")

            run_ids = {
                str(message.get("run_id"))
                for message in session.messages
                if isinstance(message.get("run_id"), str)
                and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(message.get("run_id")))
            }
            image_root = (Path(session.workspace) / ".agent-images").resolve()
            image_directory = (image_root / session.id).resolve()
            if image_directory.parent != image_root:
                raise WebRequestError("会话图片目录异常，拒绝删除")
            file_root = (Path(session.workspace) / ".agent-files").resolve()
            file_directory = (file_root / session.id).resolve()
            if file_directory.parent != file_root:
                raise WebRequestError("会话文件目录异常，拒绝删除")

            removed_images = image_directory.is_dir()
            removed_files = file_directory.is_dir()
            try:
                if removed_images:
                    shutil.rmtree(image_directory)
                if removed_files:
                    shutil.rmtree(file_directory)
                for run_id in run_ids:
                    (self.trace_dir / f"{run_id}.json").unlink(missing_ok=True)
                (self.state_dir / f"{session.id}.json").unlink(missing_ok=True)
            except OSError as exc:
                raise WebRequestError(f"删除会话文件失败：{exc}") from exc

            for run_id in run_ids:
                self._runs.pop(run_id, None)
            self._sessions.pop(session.id, None)
            return {
                "deleted": True,
                "session_id": session.id,
                "trace_count": len(run_ids),
                "removed_images": removed_images,
                "removed_files": removed_files,
            }

    def add_message(
        self, session_id: str, task: str, attachments: Sequence[str] = ()
    ) -> RunRecord:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise WebRequestError("找不到该会话")
            if session.active_run_id is not None:
                active = self._runs.get(session.active_run_id)
                if active is not None and active.status in ACTIVE_STATUSES:
                    raise WebRequestError("当前会话仍有任务在运行")
                session.active_run_id = None

            safe_attachments = self._validate_attachments_locked(session, attachments)
            agent_task = task
            if safe_attachments:
                rendered = "\n".join(f"- {path}" for path in safe_attachments)
                agent_task += (
                    "\n\nAttached workspace files:\n"
                    f"{rendered}\nUse analyze_image for an attached image and analyze_pdf "
                    "for an attached PDF when understanding its contents is needed."
                )

            turn = 1 + sum(
                1 for message in session.messages if message.get("role") == "user"
            )
            record = RunRecord(
                id=uuid4().hex,
                session_id=session.id,
                turn=turn,
                task=agent_task,
                workspace=session.workspace,
                demo=session.demo,
                max_steps=session.max_steps,
            )
            if not session.messages:
                title = task.strip().splitlines()[0]
                session.title = title[:42] + ("…" if len(title) > 42 else "")
            session.messages.append(
                {
                    "id": uuid4().hex,
                    "role": "user",
                    "content": task,
                    "attachments": safe_attachments,
                    "created_at": _now(),
                    "run_id": record.id,
                    "turn": turn,
                }
            )
            session.active_run_id = record.id
            session.updated_at = _now()
            self._runs[record.id] = record
            self._append_locked(record, "queued", {"message": "任务已进入执行队列"})
            self._save_session_locked(session)
            self._prune_locked()

        threading.Thread(
            target=self._run_worker,
            args=(record.id,),
            name=f"agent-run-{record.id[:8]}",
            daemon=True,
        ).start()
        return record

    def save_image(self, session_id: str, filename: str, content: bytes) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise WebRequestError("找不到该会话")
            if not content or len(content) > MAX_IMAGE_BYTES:
                raise WebRequestError("图片不能为空且不能超过 10 MB")
            try:
                mime = detect_image_mime(content, filename)
            except Exception as exc:
                message = getattr(exc, "message", str(exc))
                raise WebRequestError(message) from exc
            basename = Path(filename).name
            basename = re.sub(r"[^A-Za-z0-9._-]+", "_", basename).strip("._")
            if not basename:
                basename = "image"
            directory = Path(session.workspace) / ".agent-images" / session.id
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / f"{uuid4().hex[:10]}-{basename}"
            temporary = target.with_suffix(target.suffix + ".tmp")
            try:
                temporary.write_bytes(content)
                temporary.chmod(0o600)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
            relative = str(target.relative_to(Path(session.workspace)))
            return {
                "path": relative,
                "name": basename,
                "mime_type": mime,
                "size_bytes": len(content),
            }

    def save_pdf(self, session_id: str, filename: str, content: bytes) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise WebRequestError("找不到该会话")
            if not content or len(content) > MAX_PDF_BYTES:
                raise WebRequestError("PDF 不能为空且不能超过 20 MB")
            try:
                mime = detect_pdf_mime(content, filename)
            except Exception as exc:
                message = getattr(exc, "message", str(exc))
                raise WebRequestError(message) from exc
            basename = Path(filename).name
            basename = re.sub(r"[^A-Za-z0-9._-]+", "_", basename).strip("._")
            if not basename:
                basename = "document.pdf"
            if not basename.lower().endswith(".pdf"):
                basename += ".pdf"
            directory = Path(session.workspace) / ".agent-files" / session.id
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / f"{uuid4().hex[:10]}-{basename}"
            temporary = target.with_suffix(target.suffix + ".tmp")
            try:
                temporary.write_bytes(content)
                temporary.chmod(0o600)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
            relative = str(target.relative_to(Path(session.workspace)))
            return {
                "path": relative,
                "name": basename,
                "mime_type": mime,
                "size_bytes": len(content),
            }

    @staticmethod
    def _validate_attachments_locked(
        session: SessionRecord, attachments: Sequence[str]
    ) -> list[str]:
        if len(attachments) > 5:
            raise WebRequestError("每轮最多附加 5 个文件")
        root = Path(session.workspace).resolve()
        allowed_directories = {
            (root / ".agent-images" / session.id).resolve(),
            (root / ".agent-files" / session.id).resolve(),
        }
        normalized: list[str] = []
        for relative in attachments:
            if not isinstance(relative, str) or not relative:
                raise WebRequestError("附件路径无效")
            candidate = (root / relative).resolve()
            if (
                not any(allowed in candidate.parents for allowed in allowed_directories)
                or not candidate.is_file()
            ):
                raise WebRequestError("附件不属于当前会话")
            normalized.append(str(candidate.relative_to(root)))
        return normalized

    def create(
        self,
        *,
        task: str,
        workspace: Path,
        demo: bool,
        max_steps: int,
    ) -> RunRecord:
        session = self.create_session(
            workspace=workspace,
            demo=demo,
            max_steps=max_steps,
        )
        return self.add_message(session.id, task)

    def get_public(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._runs.get(run_id)
            if record:
                return self._public_locked(record)
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", run_id):
                return None
            path = self.trace_dir / f"{run_id}.json"
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                trace = payload.get("trace")
                if not isinstance(trace, Mapping):
                    return None
                public = dict(trace)
                public["events"] = [
                    self._public_trace_event(event)
                    for event in public.get("events", [])
                    if isinstance(event, Mapping)
                ]
                public["pending_approval"] = None
                return public
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                return None

    @staticmethod
    def _public_trace_event(event: Mapping[str, Any]) -> dict[str, Any]:
        public = dict(event)
        payload = public.get("payload")
        if isinstance(payload, Mapping):
            safe_payload = dict(payload)
            safe_payload.pop("request_messages", None)
            safe_payload.pop("tool_definitions", None)
            public["payload"] = _safe_value(safe_payload)
        return public

    def _update_plan(
        self, run_id: str, payload: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        with self._lock:
            record = self._runs[run_id]
            record.plan = [dict(item) for item in payload.get("plan", [])]
            record.plan_explanation = str(payload.get("explanation") or "")
            self._append_locked(
                record,
                "plan_updated",
                {
                    "message": record.plan_explanation or "执行计划已更新",
                    "plan": record.plan,
                },
            )
            return {
                "explanation": record.plan_explanation,
                "plan": list(record.plan),
            }

    def cancel(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                return None
            if record.status in ACTIVE_STATUSES and not record.cancel_event.is_set():
                record.cancel_event.set()
                if record.pending_approval is not None:
                    record.pending_approval.decision = False
                    record.pending_approval.event.set()
                self._append_locked(
                    record,
                    "cancel_requested",
                    {"message": "已请求停止；正在进行的模型请求返回后将终止"},
                )
            return self._public_locked(record)

    def decide_approval(
        self, run_id: str, approval_id: str, approved: bool
    ) -> dict[str, Any] | None:
        with self._lock:
            record = self._runs.get(run_id)
            if record is None:
                return None
            pending = record.pending_approval
            if pending is None or pending.id != approval_id:
                raise WebRequestError("该操作审批已失效，请刷新任务状态")
            if pending.decision is not None:
                raise WebRequestError("该操作审批已经处理")
            pending.decision = approved
            pending.event.set()
            return self._public_locked(record)

    def _request_tool_approval(
        self, run_id: str, proposal: Mapping[str, Any]
    ) -> bool:
        with self._lock:
            record = self._runs[run_id]
            if record.cancel_event.is_set():
                return False
            pending = PendingApproval(
                id=uuid4().hex,
                proposal=dict(proposal),
            )
            record.pending_approval = pending
            record.status = "waiting_approval"
            self._append_locked(
                record,
                "approval_required",
                {
                    "approval_id": pending.id,
                    "tool": proposal.get("tool"),
                    "kind": proposal.get("kind", "edit"),
                    "files": proposal.get("files", []),
                    "details": proposal.get("details", {}),
                    "message": (
                        "正在等待用户允许外部数据传输"
                        if proposal.get("kind") == "external"
                        else "代码尚未写入，正在等待用户确认 Diff"
                    ),
                },
            )

        pending.event.wait()

        with self._lock:
            record = self._runs[run_id]
            approved = bool(pending.decision) and not record.cancel_event.is_set()
            if record.pending_approval is pending:
                record.pending_approval = None
            if record.status == "waiting_approval":
                record.status = "running"
            self._append_locked(
                record,
                "approval_decision",
                {
                    "approval_id": pending.id,
                    "approved": approved,
                    "message": "用户已同意操作" if approved else "用户已拒绝操作",
                },
            )
            return approved

    def _run_worker(self, run_id: str) -> None:
        with self._lock:
            record = self._runs[run_id]
            session = self._sessions[record.session_id]
            record.status = "running"
            record.started_at = _now()
            record.started_clock = time.perf_counter()
            self._append_locked(
                record,
                "started",
                {"message": f"Agent 开始执行第 {record.turn} 轮对话"},
            )

        try:
            workspace = Path(record.workspace)
            registry = build_registry(
                workspace,
                self.settings.command_timeout,
                approval_handler=lambda proposal: self._request_tool_approval(
                    run_id, proposal
                ),
                settings=self.settings,
                plan_handler=lambda payload: self._update_plan(run_id, payload),
            )
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
                max_context_tokens=self.settings.context_tokens,
                on_event=on_event,
                should_stop=record.cancel_event.is_set,
            )
            result = agent.run(record.task, conversation=session.conversation)
            with self._lock:
                current = self._runs[run_id]
                current_session = self._sessions[current.session_id]
                current.status = "completed"
                current.finished_at = _now()
                self._set_duration_locked(current)
                current.final_output = result.final_output
                current.steps = result.steps
                current.tool_calls = result.tool_calls
                current_session.messages.append(
                    {
                        "id": uuid4().hex,
                        "role": "assistant",
                        "content": result.final_output,
                        "created_at": _now(),
                        "run_id": current.id,
                        "turn": current.turn,
                        "status": "completed",
                    }
                )
                current_session.active_run_id = None
                current_session.updated_at = _now()
                self._save_session_locked(current_session)
                self._save_trace_locked(current)
        except AgentCancelledError:
            with self._lock:
                current = self._runs[run_id]
                current.status = "cancelled"
                current.finished_at = _now()
                self._set_duration_locked(current)
                current.error = "任务已由用户停止"
                self._append_locked(current, "cancelled", {"message": current.error})
                self._finish_failed_turn_locked(current, "本轮已停止，可以继续发送新的 Prompt。")
        except (ValueError, AgentError, ModelAPIError) as exc:
            with self._lock:
                current = self._runs[run_id]
                current.status = "error"
                current.finished_at = _now()
                self._set_duration_locked(current)
                current.error = str(exc)
                self._append_locked(current, "error", {"message": str(exc)})
                self._finish_failed_turn_locked(current, f"本轮执行失败：{exc}")
        except Exception as exc:  # Prevent a worker crash from disappearing silently.
            with self._lock:
                current = self._runs[run_id]
                current.status = "error"
                current.finished_at = _now()
                self._set_duration_locked(current)
                current.error = f"{type(exc).__name__}: {exc}"
                self._append_locked(current, "error", {"message": current.error})
                self._finish_failed_turn_locked(current, "本轮因本地异常终止，可以继续发送新的 Prompt。")

    def _finish_failed_turn_locked(self, record: RunRecord, message: str) -> None:
        session = self._sessions[record.session_id]
        if not session.conversation.has_active_turn:
            session.conversation.start_user_turn(record.task)
        session.conversation.abort_turn(message)
        session.messages.append(
            {
                "id": uuid4().hex,
                "role": "assistant",
                "content": message,
                "created_at": _now(),
                "run_id": record.id,
                "turn": record.turn,
                "status": record.status,
            }
        )
        session.active_run_id = None
        session.updated_at = _now()
        self._save_session_locked(session)

    def _append_locked(
        self, record: RunRecord, event_type: str, payload: Mapping[str, Any]
    ) -> None:
        self._record_metrics_locked(record, event_type, payload)
        timestamp = _now()
        trace_payload = _trace_value(payload)
        trace_event = {
            "seq": record.next_event_seq,
            "type": event_type,
            "timestamp": timestamp,
            "payload": trace_payload,
        }
        record.trace_events.append(trace_event)
        public_payload = _safe_value(payload)
        public_payload = dict(public_payload) if isinstance(public_payload, Mapping) else {}
        public_payload.pop("request_messages", None)
        public_payload.pop("tool_definitions", None)
        record.events.append(
            {
                "seq": record.next_event_seq,
                "type": event_type,
                "timestamp": timestamp,
                "payload": public_payload,
            }
        )
        record.next_event_seq += 1
        if len(record.events) > 400:
            record.events[:] = record.events[-400:]
        self._save_trace_locked(record)

    def _record_metrics_locked(
        self, record: RunRecord, event_type: str, payload: Mapping[str, Any]
    ) -> None:
        if event_type == "model_response":
            usage = payload.get("usage")
            if isinstance(usage, Mapping):
                for name, value in usage.items():
                    if isinstance(value, bool) or not isinstance(value, (int, float)):
                        continue
                    current = record.model_usage.get(str(name), 0)
                    record.model_usage[str(name)] = current + value
        if event_type != "tool_finish" or payload.get("name") != "run_command":
            return
        try:
            arguments = json.loads(str(payload.get("arguments") or "{}"))
            result = json.loads(str(payload.get("result") or "{}"))
        except json.JSONDecodeError:
            return
        if not isinstance(arguments, Mapping) or not isinstance(result, Mapping):
            return
        argv = arguments.get("argv")
        if not self._is_test_command(argv):
            return
        metadata = result.get("metadata")
        if not isinstance(metadata, Mapping):
            metadata = {}
        record.final_test_result = {
            "command": argv,
            "success": bool(payload.get("success")),
            "exit_code": metadata.get("exit_code"),
            "duration_ms": payload.get("duration_ms"),
            "timestamp": _now(),
        }

    @staticmethod
    def _is_test_command(argv: Any) -> bool:
        if not isinstance(argv, list) or not argv or not all(
            isinstance(item, str) for item in argv
        ):
            return False
        executable = Path(argv[0]).name.lower()
        lowered = [item.lower() for item in argv[1:]]
        if executable in {"pytest"}:
            return True
        if executable in {"python", "python3"}:
            return any(item in {"pytest", "unittest"} for item in lowered)
        if executable in {"npm", "pnpm", "yarn"}:
            return "test" in lowered
        if executable in {"cargo", "go"}:
            return "test" in lowered
        return False

    @staticmethod
    def _set_duration_locked(record: RunRecord) -> None:
        if record.started_clock is not None:
            record.duration_ms = round(
                (time.perf_counter() - record.started_clock) * 1_000, 2
            )

    def _save_trace_locked(self, record: RunRecord) -> None:
        target = self.trace_dir / f"{record.id}.json"
        temporary = target.with_suffix(".tmp")
        trace = self._public_locked(record)
        trace["events"] = list(record.trace_events)
        payload = {
            "version": 1,
            "trace": trace,
        }
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.chmod(0o600)
        temporary.replace(target)

    def _public_locked(self, record: RunRecord) -> dict[str, Any]:
        return {
            "id": record.id,
            "session_id": record.session_id,
            "turn": record.turn,
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
            "model_usage": dict(record.model_usage),
            "duration_ms": record.duration_ms,
            "final_test_result": record.final_test_result,
            "plan": list(record.plan),
            "plan_explanation": record.plan_explanation,
            "pending_approval": (
                record.pending_approval.public()
                if record.pending_approval is not None
                else None
            ),
        }

    def _session_summary_locked(self, session: SessionRecord) -> dict[str, Any]:
        return {
            "id": session.id,
            "title": session.title,
            "workspace": session.workspace,
            "demo": session.demo,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "turn_count": sum(
                1 for message in session.messages if message.get("role") == "user"
            ),
            "active_run_id": session.active_run_id,
        }

    def _session_public_locked(self, session: SessionRecord) -> dict[str, Any]:
        return {
            **self._session_summary_locked(session),
            "max_steps": session.max_steps,
            "messages": list(session.messages),
            "context": session.conversation.context_stats(),
            "memory": session.conversation.memory_checkpoint(),
        }

    def _save_session_locked(self, session: SessionRecord) -> None:
        payload = {
            "version": SESSION_STATE_VERSION,
            "id": session.id,
            "title": session.title,
            "workspace": session.workspace,
            "demo": session.demo,
            "max_steps": session.max_steps,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
            "messages": session.messages,
            "conversation": session.conversation.to_state(),
        }
        target = self.state_dir / f"{session.id}.json"
        temporary = target.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.chmod(0o600)
        temporary.replace(target)

    def _load_sessions(self) -> None:
        for path in sorted(self.state_dir.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if payload.get("version") != SESSION_STATE_VERSION:
                    continue
                conversation = Conversation.from_state(payload["conversation"])
                conversation.set_system_prompt(self._system_prompt())
                conversation.abort_turn(
                    "上一轮因服务重启中断，请根据当前项目状态继续。"
                )
                workspace = Path(payload["workspace"]).resolve()
                if not workspace.is_dir():
                    continue
                messages = payload.get("messages", [])
                if not isinstance(messages, list):
                    continue
                session = SessionRecord(
                    id=str(payload["id"]),
                    title=str(payload.get("title") or "已恢复会话"),
                    workspace=str(workspace),
                    demo=bool(payload.get("demo", False)),
                    max_steps=int(payload.get("max_steps", self.settings.max_steps)),
                    conversation=conversation,
                    created_at=str(payload.get("created_at") or _now()),
                    updated_at=str(payload.get("updated_at") or _now()),
                    messages=[dict(message) for message in messages if isinstance(message, Mapping)],
                )
                self._sessions[session.id] = session
                self._save_session_locked(session)
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
                continue

    def _prune_locked(self) -> None:
        if len(self._runs) <= MAX_RUNS:
            return
        removable = [
            record
            for record in self._runs.values()
            if record.status not in ACTIVE_STATUSES
        ]
        for record in sorted(removable, key=lambda item: item.created_at):
            if len(self._runs) <= MAX_RUNS:
                break
            self._runs.pop(record.id, None)


class LocalWebApplication:
    def __init__(
        self, settings: Settings, default_workspace: Path, state_dir: Path | None = None
    ) -> None:
        self.settings = settings
        self.default_workspace = default_workspace.resolve()
        session_dir = state_dir or self.default_workspace / ".coding-agent" / "sessions"
        self.runs = RunStore(settings, session_dir)

    def config(self) -> dict[str, Any]:
        return {
            "api_configured": bool(self.settings.api_key),
            "model": self.settings.model,
            "thinking": self.settings.thinking,
            "reasoning_effort": self.settings.reasoning_effort,
            "default_workspace": str(self.default_workspace),
            "max_steps": self.settings.max_steps,
            "context_budget_tokens": self.settings.context_tokens,
            "vision_configured": bool(self.settings.qwen_api_key),
            "vision_model": self.settings.qwen_model,
            "web_search_configured": bool(self.settings.qwen_api_key),
            "external_provider": "通义千问 / 阿里云百炼",
        }

    def create_session(self, body: Mapping[str, Any]) -> dict[str, Any]:
        workspace, demo, max_steps = self._run_settings(body)
        session = self.runs.create_session(
            workspace=workspace,
            demo=demo,
            max_steps=max_steps,
        )
        public = self.runs.get_session_public(session.id)
        assert public is not None
        return public

    def delete_session(self, session_id: str) -> dict[str, Any]:
        deleted = self.runs.delete_session(session_id)
        if deleted is None:
            raise WebRequestError("找不到该会话")
        return deleted

    def add_session_message(
        self, session_id: str, body: Mapping[str, Any]
    ) -> dict[str, Any]:
        task = self._task(body)
        attachments = body.get("attachments", [])
        if not isinstance(attachments, list) or any(
            not isinstance(path, str) for path in attachments
        ):
            raise WebRequestError("attachments 必须是附件路径数组")
        record = self.runs.add_message(session_id, task, attachments)
        public = self.runs.get_public(record.id)
        assert public is not None
        return public

    def upload_session_image(
        self, session_id: str, body: Mapping[str, Any]
    ) -> dict[str, Any]:
        filename = body.get("filename")
        encoded = body.get("data_base64")
        if not isinstance(filename, str) or not filename.strip():
            raise WebRequestError("图片文件名无效")
        if not isinstance(encoded, str) or not encoded:
            raise WebRequestError("图片内容为空")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise WebRequestError("图片内容不是有效的 Base64") from exc
        return self.runs.save_image(session_id, filename, content)

    def upload_session_pdf(
        self, session_id: str, body: Mapping[str, Any]
    ) -> dict[str, Any]:
        filename = body.get("filename")
        encoded = body.get("data_base64")
        if not isinstance(filename, str) or not filename.strip():
            raise WebRequestError("PDF 文件名无效")
        if not isinstance(encoded, str) or not encoded:
            raise WebRequestError("PDF 内容为空")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise WebRequestError("PDF 内容不是有效的 Base64") from exc
        return self.runs.save_pdf(session_id, filename, content)

    def create_run(self, body: Mapping[str, Any]) -> dict[str, Any]:
        task = self._task(body)
        workspace, demo, max_steps = self._run_settings(body)
        record = self.runs.create(
            task=task,
            workspace=workspace,
            demo=demo,
            max_steps=max_steps,
        )
        public = self.runs.get_public(record.id)
        assert public is not None
        return public

    def decide_approval(
        self, run_id: str, approval_id: str, body: Mapping[str, Any]
    ) -> dict[str, Any]:
        approved = body.get("approved")
        if not isinstance(approved, bool):
            raise WebRequestError("approved 必须是布尔值")
        record = self.runs.decide_approval(run_id, approval_id, approved)
        if record is None:
            raise WebRequestError("找不到该任务")
        return record

    @staticmethod
    def _task(body: Mapping[str, Any]) -> str:
        task = body.get("task", body.get("content"))
        if not isinstance(task, str) or not task.strip():
            raise WebRequestError("Prompt 不能为空")
        if len(task) > 20_000:
            raise WebRequestError("Prompt 不能超过 20,000 个字符")
        return task.strip()

    def _run_settings(
        self, body: Mapping[str, Any]
    ) -> tuple[Path, bool, int]:
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
        return workspace, demo, max_steps


class WebRequestError(ValueError):
    pass


def make_handler(application: LocalWebApplication) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "LoopCoder/0.4"

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path == "/api/health":
                self._json({"ok": True, "time": _now()})
                return
            if parsed.path == "/api/config":
                self._json(application.config())
                return
            if parsed.path == "/api/sessions":
                self._json({"sessions": application.runs.list_sessions_public()})
                return
            if parsed.path.startswith("/api/sessions/"):
                session_id = parsed.path.removeprefix("/api/sessions/").strip("/")
                session = application.runs.get_session_public(session_id)
                if session is None:
                    self._json({"error": "找不到该会话"}, HTTPStatus.NOT_FOUND)
                else:
                    self._json(session)
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
                is_image_upload = (
                    parsed.path.startswith("/api/sessions/")
                    and parsed.path.endswith("/images")
                )
                is_pdf_upload = (
                    parsed.path.startswith("/api/sessions/")
                    and parsed.path.endswith("/pdfs")
                )
                body = self._read_json(
                    MAX_PDF_UPLOAD_REQUEST_BYTES
                    if is_pdf_upload
                    else MAX_UPLOAD_REQUEST_BYTES
                    if is_image_upload
                    else MAX_REQUEST_BYTES
                )
                if parsed.path == "/api/sessions":
                    self._json(application.create_session(body), HTTPStatus.CREATED)
                    return
                if parsed.path.startswith("/api/sessions/") and parsed.path.endswith(
                    "/messages"
                ):
                    session_id = parsed.path.removeprefix("/api/sessions/").removesuffix(
                        "/messages"
                    ).strip("/")
                    self._json(
                        application.add_session_message(session_id, body),
                        HTTPStatus.ACCEPTED,
                    )
                    return
                if is_image_upload:
                    session_id = parsed.path.removeprefix("/api/sessions/").removesuffix(
                        "/images"
                    ).strip("/")
                    self._json(
                        application.upload_session_image(session_id, body),
                        HTTPStatus.CREATED,
                    )
                    return
                if is_pdf_upload:
                    session_id = parsed.path.removeprefix("/api/sessions/").removesuffix(
                        "/pdfs"
                    ).strip("/")
                    self._json(
                        application.upload_session_pdf(session_id, body),
                        HTTPStatus.CREATED,
                    )
                    return
                if parsed.path == "/api/runs":
                    self._json(application.create_run(body), HTTPStatus.ACCEPTED)
                    return
                approval_parts = parsed.path.strip("/").split("/")
                if (
                    len(approval_parts) == 5
                    and approval_parts[:2] == ["api", "runs"]
                    and approval_parts[3] == "approvals"
                ):
                    self._json(
                        application.decide_approval(
                            approval_parts[2], approval_parts[4], body
                        )
                    )
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

        def do_DELETE(self) -> None:  # noqa: N802
            if self.headers.get("X-Agent-Client") != "web-ui":
                self._json({"error": "缺少本地客户端标识"}, HTTPStatus.FORBIDDEN)
                return
            parsed = urlparse(self.path)
            parts = parsed.path.strip("/").split("/")
            if len(parts) != 3 or parts[:2] != ["api", "sessions"]:
                self._json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)
                return
            try:
                self._json(application.delete_session(parts[2]))
            except WebRequestError as exc:
                status = (
                    HTTPStatus.NOT_FOUND
                    if str(exc) == "找不到该会话"
                    else HTTPStatus.BAD_REQUEST
                )
                self._json({"error": str(exc)}, status)

        def _read_json(self, maximum_bytes: int) -> Mapping[str, Any]:
            if self.headers.get_content_type() != "application/json":
                raise WebRequestError("请求必须使用 application/json")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise WebRequestError("无效的 Content-Length") from exc
            if length <= 0 or length > maximum_bytes:
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
    state_dir: Path | None = None,
) -> ThreadingHTTPServer:
    if not 0 <= port <= 65_535:
        raise ValueError("port must be between 0 and 65535")
    application = LocalWebApplication(settings, default_workspace, state_dir)
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
