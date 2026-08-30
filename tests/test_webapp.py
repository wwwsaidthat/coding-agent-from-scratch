from pathlib import Path
import base64
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from coding_agent.config import Settings, load_env_file
from coding_agent.webapp import LocalWebApplication, RunRecord, RunStore, create_http_server


class EnvFileTests(unittest.TestCase):
    def test_load_env_file_preserves_existing_shell_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            env_file = Path(temporary) / ".env"
            env_file.write_text(
                'WEBAPP_TEST_EXISTING="file-value"\nWEBAPP_TEST_NEW="new-value"\n',
                encoding="utf-8",
            )
            previous_existing = os.environ.get("WEBAPP_TEST_EXISTING")
            previous_new = os.environ.get("WEBAPP_TEST_NEW")
            try:
                os.environ["WEBAPP_TEST_EXISTING"] = "shell-value"
                os.environ.pop("WEBAPP_TEST_NEW", None)
                load_env_file(env_file)
                self.assertEqual(os.environ["WEBAPP_TEST_EXISTING"], "shell-value")
                self.assertEqual(os.environ["WEBAPP_TEST_NEW"], "new-value")
            finally:
                if previous_existing is None:
                    os.environ.pop("WEBAPP_TEST_EXISTING", None)
                else:
                    os.environ["WEBAPP_TEST_EXISTING"] = previous_existing
                if previous_new is None:
                    os.environ.pop("WEBAPP_TEST_NEW", None)
                else:
                    os.environ["WEBAPP_TEST_NEW"] = previous_new


class WebApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temporary.name)
        settings = Settings(api_key=None, max_steps=10, command_timeout=5)
        self.server = create_http_server(
            settings,
            host="127.0.0.1",
            port=0,
            default_workspace=self.workspace,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address[:2]
        self.base_url = f"http://{host}:{port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def get_json(self, path: str) -> dict:
        with urlopen(self.base_url + path, timeout=3) as response:
            return json.loads(response.read().decode("utf-8"))

    def post_json(self, path: str, body: dict, *, include_client: bool = True) -> dict:
        headers = {"Content-Type": "application/json"}
        if include_client:
            headers["X-Agent-Client"] = "web-ui"
        request = Request(
            self.base_url + path,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers=headers,
        )
        with urlopen(request, timeout=3) as response:
            return json.loads(response.read().decode("utf-8"))

    def delete_json(self, path: str, *, include_client: bool = True) -> dict:
        headers = {"Content-Type": "application/json"}
        if include_client:
            headers["X-Agent-Client"] = "web-ui"
        request = Request(
            self.base_url + path,
            data=b"{}",
            method="DELETE",
            headers=headers,
        )
        with urlopen(request, timeout=3) as response:
            return json.loads(response.read().decode("utf-8"))

    def wait_for_run(self, run: dict, timeout: float = 5) -> dict:
        deadline = time.monotonic() + timeout
        while run["status"] in {"queued", "running", "waiting_approval"}:
            if run["status"] == "waiting_approval":
                approval = run["pending_approval"]
                run = self.post_json(
                    f"/api/runs/{run['id']}/approvals/{approval['id']}",
                    {"approved": True},
                )
            if time.monotonic() >= deadline:
                break
            time.sleep(0.03)
            run = self.get_json(f"/api/runs/{run['id']}")
        return run

    def test_health_config_and_static_page(self) -> None:
        self.assertTrue(self.get_json("/api/health")["ok"])
        config = self.get_json("/api/config")
        self.assertFalse(config["api_configured"])
        self.assertEqual(config["context_budget_tokens"], 96_000)
        self.assertEqual(config["default_workspace"], str(self.workspace.resolve()))
        with urlopen(self.base_url + "/", timeout=3) as response:
            html = response.read().decode("utf-8")
            self.assertIn("LOOPCODER", html)
            self.assertIn('id="session-list"', html)
            self.assertIn('id="approval-card"', html)
            self.assertIn('id="attachment-input"', html)
            self.assertIn("application/pdf", html)
            self.assertIn('id="run-plan"', html)
            self.assertIn('id="memory-checkpoint"', html)
            self.assertLess(html.index('id="conversation-memory-title"'), html.index('id="activity-title"'))
            self.assertNotIn('id="result-card"', html)
            self.assertIn("执行路径与确认", html)
            self.assertIn("Content-Security-Policy", response.headers)
        with urlopen(self.base_url + "/assets/app.js", timeout=3) as response:
            javascript = response.read().decode("utf-8")
            self.assertIn("function renderMarkdown", javascript)
            self.assertIn("function deleteSession", javascript)
            self.assertIn('method: "DELETE"', javascript)
            self.assertNotIn(".innerHTML", javascript)

    def test_post_requires_same_origin_client_header(self) -> None:
        with self.assertRaises(HTTPError) as caught:
            self.post_json(
                "/api/runs",
                {"task": "demo", "demo": True},
                include_client=False,
            )
        self.assertEqual(caught.exception.code, 403)

    def test_offline_demo_completes_through_http_api(self) -> None:
        (self.workspace / "agent_demo.txt").write_text("old value\n", encoding="utf-8")
        created = self.post_json(
            "/api/runs",
            {
                "task": "Run the web demo",
                "workspace": str(self.workspace),
                "demo": True,
                "max_steps": 10,
            },
        )
        self.assertIn(
            created["status"],
            {"queued", "running", "waiting_approval", "completed"},
        )

        deadline = time.monotonic() + 5
        current = created
        observed_diff = None
        while current["status"] in {"queued", "running", "waiting_approval"} and time.monotonic() < deadline:
            if current["status"] == "waiting_approval":
                approval = current["pending_approval"]
                observed_diff = approval["proposal"]["files"][0]["diff"]
                current = self.post_json(
                    f"/api/runs/{current['id']}/approvals/{approval['id']}",
                    {"approved": True},
                )
            time.sleep(0.05)
            current = self.get_json(f"/api/runs/{created['id']}")

        self.assertEqual(current["status"], "completed")
        self.assertEqual(current["steps"], 5)
        self.assertEqual(current["tool_calls"], 4)
        self.assertIn("Offline demo completed", current["final_output"])
        self.assertIn("-old value", observed_diff)
        self.assertIn("+Created by the offline agent demo.", observed_diff)
        self.assertTrue((self.workspace / "agent_demo.txt").is_file())
        event_types = {event["type"] for event in current["events"]}
        self.assertIn("tool_start", event_types)
        self.assertIn("completed", event_types)
        decisions = [
            event for event in current["events"] if event["type"] == "model_response"
        ]
        self.assertTrue(all(event["payload"]["thought"] for event in decisions))
        self.assertTrue(
            all("reasoning_content" not in event["payload"] for event in decisions)
        )
        self.assertIsNotNone(current["duration_ms"])
        trace_path = self.workspace / ".coding-agent" / "traces" / f"{created['id']}.json"
        self.assertTrue(trace_path.is_file())
        trace = json.loads(trace_path.read_text(encoding="utf-8"))["trace"]
        self.assertEqual(trace["status"], "completed")
        self.assertIn("model_usage", trace)
        traced_request = next(
            event for event in trace["events"] if event["type"] == "model_request"
        )
        self.assertIn("request_messages", traced_request["payload"])
        public_request = next(
            event for event in current["events"] if event["type"] == "model_request"
        )
        self.assertNotIn("request_messages", public_request["payload"])

    def test_multi_turn_session_is_persisted_and_restored(self) -> None:
        session = self.post_json(
            "/api/sessions",
            {
                "workspace": str(self.workspace),
                "demo": True,
                "max_steps": 10,
            },
        )

        for prompt in ("first turn", "follow-up turn"):
            run = self.post_json(
                f"/api/sessions/{session['id']}/messages",
                {"content": prompt},
            )
            run = self.wait_for_run(run)
            self.assertEqual(run["status"], "completed")

        current = self.get_json(f"/api/sessions/{session['id']}")
        self.assertEqual(current["turn_count"], 2)
        self.assertEqual(
            [message["role"] for message in current["messages"]],
            ["user", "assistant", "user", "assistant"],
        )
        self.assertEqual(current["context"]["total_exchanges"], 2)
        self.assertEqual(current["memory"]["goal"], "follow-up turn")
        self.assertIn("agent_demo.txt", current["memory"]["modified_files"])

        restored_app = LocalWebApplication(
            Settings(api_key=None, max_steps=10, command_timeout=5),
            self.workspace,
        )
        restored = restored_app.runs.get_session_public(session["id"])
        self.assertIsNotNone(restored)
        self.assertEqual(restored["turn_count"], 2)
        self.assertEqual(restored["messages"][-1]["role"], "assistant")
        self.assertEqual(restored["memory"], current["memory"])
        restored_prompt = restored_app.runs._sessions[session["id"]].conversation.api_messages()[0]["content"]
        self.assertIn("deepseek-v4-pro", restored_prompt)
        self.assertIn("qwen3.6-flash", restored_prompt)

    def test_session_does_not_inject_workspace_instruction_files(self) -> None:
        (self.workspace / "AGENTS.md").write_text(
            "Always validate the smallest relevant scope.", encoding="utf-8"
        )
        session = self.post_json(
            "/api/sessions",
            {"workspace": str(self.workspace), "demo": True, "max_steps": 10},
        )
        restored_app = LocalWebApplication(
            Settings(api_key=None, max_steps=10, command_timeout=5),
            self.workspace,
        )
        conversation = restored_app.runs._sessions[session["id"]].conversation
        visible = json.dumps(conversation.api_messages(), ensure_ascii=False)
        self.assertNotIn("Always validate the smallest relevant scope", visible)
        self.assertNotIn("project_rules_chars", session["context"])

    def test_image_upload_is_bound_to_session_and_message(self) -> None:
        session = self.post_json(
            "/api/sessions",
            {"workspace": str(self.workspace), "demo": True, "max_steps": 10},
        )
        image = b"\x89PNG\r\n\x1a\n" + b"local-image"
        uploaded = self.post_json(
            f"/api/sessions/{session['id']}/images",
            {
                "filename": "screen.png",
                "data_base64": base64.b64encode(image).decode("ascii"),
            },
        )
        self.assertTrue(uploaded["path"].startswith(f".agent-images/{session['id']}/"))
        run = self.post_json(
            f"/api/sessions/{session['id']}/messages",
            {"content": "inspect this", "attachments": [uploaded["path"]]},
        )
        run = self.wait_for_run(run)
        current = self.get_json(f"/api/sessions/{session['id']}")
        self.assertEqual(current["messages"][0]["attachments"], [uploaded["path"]])

    def test_pdf_upload_is_bound_to_session_and_message(self) -> None:
        session = self.post_json(
            "/api/sessions",
            {"workspace": str(self.workspace), "demo": True, "max_steps": 10},
        )
        pdf = b"%PDF-1.7\nlocal-pdf"
        uploaded = self.post_json(
            f"/api/sessions/{session['id']}/pdfs",
            {
                "filename": "paper.pdf",
                "data_base64": base64.b64encode(pdf).decode("ascii"),
            },
        )
        self.assertTrue(uploaded["path"].startswith(f".agent-files/{session['id']}/"))
        self.assertEqual(uploaded["mime_type"], "application/pdf")
        run = self.post_json(
            f"/api/sessions/{session['id']}/messages",
            {"content": "summarize this", "attachments": [uploaded["path"]]},
        )
        run = self.wait_for_run(run)
        current = self.get_json(f"/api/sessions/{session['id']}")
        self.assertEqual(current["messages"][0]["attachments"], [uploaded["path"]])

    def test_delete_session_removes_messages_traces_and_uploaded_images(self) -> None:
        session = self.post_json(
            "/api/sessions",
            {"workspace": str(self.workspace), "demo": True, "max_steps": 10},
        )
        uploaded = self.post_json(
            f"/api/sessions/{session['id']}/images",
            {
                "filename": "delete-me.png",
                "data_base64": base64.b64encode(
                    b"\x89PNG\r\n\x1a\n" + b"delete-me"
                ).decode("ascii"),
            },
        )
        uploaded_pdf = self.post_json(
            f"/api/sessions/{session['id']}/pdfs",
            {
                "filename": "delete-me.pdf",
                "data_base64": base64.b64encode(b"%PDF-1.7\ndelete-me").decode("ascii"),
            },
        )
        run = self.post_json(
            f"/api/sessions/{session['id']}/messages",
            {
                "content": "temporary conversation",
                "attachments": [uploaded["path"], uploaded_pdf["path"]],
            },
        )
        run = self.wait_for_run(run)
        self.assertEqual(run["status"], "completed")
        session_file = self.workspace / ".coding-agent" / "sessions" / f"{session['id']}.json"
        trace_file = self.workspace / ".coding-agent" / "traces" / f"{run['id']}.json"
        image_file = self.workspace / uploaded["path"]
        pdf_file = self.workspace / uploaded_pdf["path"]
        self.assertTrue(session_file.is_file())
        self.assertTrue(trace_file.is_file())
        self.assertTrue(image_file.is_file())
        self.assertTrue(pdf_file.is_file())

        deleted = self.delete_json(f"/api/sessions/{session['id']}")
        self.assertTrue(deleted["deleted"])
        self.assertFalse(session_file.exists())
        self.assertFalse(trace_file.exists())
        self.assertFalse(image_file.exists())
        self.assertFalse(pdf_file.exists())
        self.assertFalse(
            any(
                item["id"] == session["id"]
                for item in self.get_json("/api/sessions")["sessions"]
            )
        )

    def test_session_list_does_not_expose_conversation_in_summary(self) -> None:
        created = self.post_json(
            "/api/sessions",
            {"workspace": str(self.workspace), "demo": True, "max_steps": 10},
        )
        listed = self.get_json("/api/sessions")["sessions"]
        matching = next(item for item in listed if item["id"] == created["id"])
        self.assertNotIn("messages", matching)
        self.assertNotIn("conversation", matching)

    def test_trace_aggregates_tokens_and_final_test_result(self) -> None:
        store = RunStore(
            Settings(api_key=None, max_steps=10, command_timeout=5),
            self.workspace / "trace-test" / "sessions",
        )
        record = RunRecord(
            id="trace-unit",
            session_id="session-unit",
            turn=1,
            task="run tests",
            workspace=str(self.workspace),
            demo=True,
            max_steps=10,
        )
        with store._lock:
            store._append_locked(
                record,
                "model_response",
                {
                    "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
                    "request_messages": [{"role": "user", "content": "private"}],
                },
            )
            store._append_locked(
                record,
                "tool_finish",
                {
                    "name": "run_command",
                    "arguments": '{"argv":["python3","-m","unittest"]}',
                    "result": '{"success":true,"metadata":{"exit_code":0}}',
                    "success": True,
                    "duration_ms": 12.5,
                },
            )
        self.assertEqual(record.model_usage["total_tokens"], 14)
        self.assertTrue(record.final_test_result["success"])
        self.assertEqual(record.final_test_result["exit_code"], 0)
        self.assertTrue((store.trace_dir / "trace-unit.json").is_file())
        restored = RunStore(
            Settings(api_key=None, max_steps=10, command_timeout=5),
            self.workspace / "trace-test" / "sessions",
        ).get_public("trace-unit")
        self.assertIsNotNone(restored)
        self.assertNotIn("request_messages", restored["events"][0]["payload"])

    def test_persistent_trace_keeps_full_tool_output_while_public_view_is_bounded(self) -> None:
        store = RunStore(
            Settings(api_key=None, max_steps=10, command_timeout=5),
            self.workspace / "large-trace" / "sessions",
        )
        record = RunRecord(
            id="large-trace-unit",
            session_id="session-unit",
            turn=1,
            task="large output",
            workspace=str(self.workspace),
            demo=True,
            max_steps=10,
        )
        full_result = "BEGIN-" + "x" * 120_000 + "-END"
        with store._lock:
            store._append_locked(
                record,
                "tool_finish",
                {
                    "name": "read_file",
                    "arguments": '{"path":"large.txt"}',
                    "result": full_result,
                    "success": True,
                },
            )

        trace_path = store.trace_dir / "large-trace-unit.json"
        persisted = json.loads(trace_path.read_text(encoding="utf-8"))
        stored_result = persisted["trace"]["events"][0]["payload"]["result"]
        self.assertEqual(stored_result, full_result)
        public = store.get_public("large-trace-unit")
        self.assertIsNotNone(public)
        public_result = public["events"][0]["payload"]["result"]
        self.assertLess(len(public_result), len(full_result))
        self.assertIn("truncated by web view", public_result)


if __name__ == "__main__":
    unittest.main()
