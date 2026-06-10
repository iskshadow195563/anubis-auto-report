from __future__ import annotations

import json
import logging
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from auto_report.web_gui import ReusableThreadingHTTPServer, make_handler


class _Controller:
    logger = logging.getLogger("test_web_gui_security")

    def snapshot(self) -> dict:
        return {"status": "test"}

    def run_once(self) -> str:
        return "run-once"

    def start_loop(self) -> str:
        return "loop-started"

    def start_service(self) -> str:
        return "service-started"

    def stop_service(self) -> str:
        return "service-stopped"

    def stop(self) -> str:
        return "stopped"


class WebGuiSecurityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.token = "test-csrf-token"
        self.server = ReusableThreadingHTTPServer(
            ("127.0.0.1", 0),
            make_handler(_Controller(), self.token),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_page_contains_csrf_token(self) -> None:
        with urlopen(f"{self.base_url}/", timeout=2) as response:
            body = response.read().decode("utf-8")
        self.assertIn(f"const csrfToken = '{self.token}'", body)
        self.assertNotIn("__CSRF_TOKEN__", body)

    def test_post_requires_token_and_same_origin(self) -> None:
        request = Request(f"{self.base_url}/api/stop", method="POST")
        with self.assertRaises(HTTPError) as missing_token:
            urlopen(request, timeout=2)
        self.assertEqual(missing_token.exception.code, 403)

        bad_origin = Request(
            f"{self.base_url}/api/stop",
            method="POST",
            headers={"X-CSRF-Token": self.token, "Origin": "https://example.com"},
        )
        with self.assertRaises(HTTPError) as wrong_origin:
            urlopen(bad_origin, timeout=2)
        self.assertEqual(wrong_origin.exception.code, 403)

        allowed = Request(
            f"{self.base_url}/api/stop",
            method="POST",
            headers={"X-CSRF-Token": self.token, "Origin": self.base_url},
        )
        with urlopen(allowed, timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["message"], "stopped")


if __name__ == "__main__":
    unittest.main()
