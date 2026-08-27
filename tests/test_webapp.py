from pathlib import Path
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
from coding_agent.webapp import create_http_server


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

    def test_health_config_and_static_page(self) -> None:
        self.assertTrue(self.get_json("/api/health")["ok"])
        config = self.get_json("/api/config")
        self.assertFalse(config["api_configured"])
        self.assertEqual(config["default_workspace"], str(self.workspace.resolve()))
        with urlopen(self.base_url + "/", timeout=3) as response:
            html = response.read().decode("utf-8")
            self.assertIn("LOOPCODER", html)
            self.assertIn("Content-Security-Policy", response.headers)

    def test_post_requires_same_origin_client_header(self) -> None:
        with self.assertRaises(HTTPError) as caught:
            self.post_json(
                "/api/runs",
                {"task": "demo", "demo": True},
                include_client=False,
            )
        self.assertEqual(caught.exception.code, 403)

    def test_offline_demo_completes_through_http_api(self) -> None:
        created = self.post_json(
            "/api/runs",
            {
                "task": "Run the web demo",
                "workspace": str(self.workspace),
                "demo": True,
                "max_steps": 10,
            },
        )
        self.assertIn(created["status"], {"queued", "running", "completed"})

        deadline = time.monotonic() + 4
        current = created
        while current["status"] in {"queued", "running"} and time.monotonic() < deadline:
            time.sleep(0.05)
            current = self.get_json(f"/api/runs/{created['id']}")

        self.assertEqual(current["status"], "completed")
        self.assertEqual(current["steps"], 4)
        self.assertEqual(current["tool_calls"], 3)
        self.assertIn("Offline demo completed", current["final_output"])
        self.assertTrue((self.workspace / "agent_demo.txt").is_file())
        event_types = {event["type"] for event in current["events"]}
        self.assertIn("tool_start", event_types)
        self.assertIn("completed", event_types)


if __name__ == "__main__":
    unittest.main()
