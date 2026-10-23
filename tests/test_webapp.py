"""End-to-end tests against a real server instance on a loopback port."""

import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ps2share import opl, webapp  # noqa: E402
from ps2share.config import Config  # noqa: E402


class ServerTestCase(unittest.TestCase):
    """Spins up the real HTTP server on an ephemeral port."""

    password = ""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.share = os.path.join(cls.tmp.name, "share")
        os.makedirs(cls.share)
        opl.ensure_layout(cls.share)

        config = Config()
        config.config_path = os.path.join(cls.tmp.name, "config.json")
        config.share_path = cls.share
        if cls.password:
            config.set_ui_password(cls.password)

        cls.app = webapp.App(config, state_dir=os.path.join(cls.tmp.name, "state"))
        cls.server = webapp.Server(("127.0.0.1", 0), cls.app)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.app.store.close()
        cls.tmp.cleanup()

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def fetch(self, path, body=None, headers=None, method=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.url(path), data=data, method=method)
        request.add_header("X-PS2Share", "1")
        if data:
            request.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                return response.status, json.loads(response.read().decode() or "{}"), response.headers
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode()
            payload = json.loads(raw) if raw.startswith("{") else {}
            return exc.code, payload, exc.headers


class OpenServerTests(ServerTestCase):
    def test_serves_the_page_and_its_assets(self):
        for path, fragment in (("/", b"PS2 File Share"), ("/static/app.css", b"--series-1"), ("/static/app.js", b"drawThroughput")):
            with urllib.request.urlopen(self.url(path), timeout=10) as response:
                self.assertEqual(response.status, 200)
                self.assertIn(fragment, response.read())

    def test_session_endpoint_reports_no_password(self):
        status, payload, _ = self.fetch("/api/session")
        self.assertEqual(status, 200)
        self.assertFalse(payload["auth_required"])

    def test_state_carries_everything_the_ui_needs(self):
        status, payload, _ = self.fetch("/api/state")
        self.assertEqual(status, 200)
        state = payload["state"]
        for key in ("config", "samba", "health", "network", "opl", "warnings"):
            self.assertIn(key, state)
        self.assertEqual(state["config"]["share_path"], self.share)

    def test_live_snapshot_shape(self):
        status, payload, _ = self.fetch("/api/live")
        self.assertEqual(status, 200)
        self.assertIn("clients", payload)
        self.assertIn("history", payload)
        self.assertIn("current", payload)

    def test_games_endpoint_lists_the_layout(self):
        status, payload, _ = self.fetch("/api/games")
        self.assertEqual(status, 200)
        self.assertEqual(payload["count"], 0)
        self.assertEqual(len(payload["folders"]), len(opl.OPL_FOLDERS))

    def test_usage_endpoint_has_every_window(self):
        status, payload, _ = self.fetch("/api/usage?days=7")
        self.assertEqual(status, 200)
        for key in ("today", "week", "month", "all_time"):
            self.assertIn(key, payload)
        self.assertEqual(len(payload["daily"]), 7)

    def test_preview_renders_the_config(self):
        status, payload, _ = self.fetch("/api/preview")
        self.assertEqual(status, 200)
        self.assertIn("server min protocol = NT1", payload["config"])

    def test_folder_browser_lists_directories(self):
        status, payload, _ = self.fetch(f"/api/fs?path={self.share}")
        self.assertEqual(status, 200)
        names = {entry["name"] for entry in payload["entries"]}
        self.assertIn("DVD", names)
        self.assertIn("VMC", names)

    def test_post_without_the_csrf_header_is_refused(self):
        request = urllib.request.Request(self.url("/api/settings"), data=b"{}", method="POST")
        request.add_header("Content-Type", "application/json")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)
        self.assertEqual(caught.exception.code, 400)

    def test_settings_round_trip(self):
        status, payload, _ = self.fetch("/api/settings", {"share_name": "GAMES", "smb1_only": True})
        self.assertEqual(status, 200)
        self.assertEqual(payload["config"]["share_name"], "GAMES")
        _, state, _ = self.fetch("/api/state?force=1")
        self.assertEqual(state["state"]["config"]["share_name"], "GAMES")
        self.fetch("/api/settings", {"share_name": "PS2SMB", "smb1_only": False})

    def test_apply_refuses_a_folder_that_is_not_there(self):
        status, payload, _ = self.fetch("/api/share", {"share_path": "/nope/not/here"})
        self.assertEqual(status, 400)
        self.assertIn("not a folder", payload["error"])

    def test_prepare_is_idempotent(self):
        status, payload, _ = self.fetch("/api/prepare", {"path": self.share})
        self.assertEqual(status, 200)
        self.assertEqual(payload["created"], [])

    def test_mkdir_creates_a_folder(self):
        target = os.path.join(self.share, "DVD", "nested")
        status, payload, _ = self.fetch("/api/mkdir", {"path": target})
        self.assertEqual(status, 200)
        self.assertTrue(os.path.isdir(target))

    def test_unknown_api_route_is_a_404(self):
        status, _, _ = self.fetch("/api/nope")
        self.assertEqual(status, 404)

    def test_static_path_traversal_is_blocked(self):
        status, _, _ = self.fetch("/static/../../../../etc/passwd")
        self.assertIn(status, (400, 404))


class PasswordProtectedServerTests(ServerTestCase):
    password = "hunter2secret"

    def test_api_is_closed_until_sign_in(self):
        status, _, _ = self.fetch("/api/state")
        self.assertEqual(status, 401)

    def test_session_endpoint_stays_reachable(self):
        status, payload, _ = self.fetch("/api/session")
        self.assertEqual(status, 200)
        self.assertTrue(payload["auth_required"])
        self.assertFalse(payload["authenticated"])

    def test_the_page_itself_still_loads(self):
        with urllib.request.urlopen(self.url("/"), timeout=10) as response:
            self.assertEqual(response.status, 200)

    def test_wrong_password_is_rejected(self):
        status, _, _ = self.fetch("/api/login", {"password": "wrong"})
        self.assertEqual(status, 401)

    def test_sign_in_then_use_the_api(self):
        status, _, headers = self.fetch("/api/login", {"password": self.password})
        self.assertEqual(status, 200)
        cookie = headers["Set-Cookie"].split(";")[0]
        self.assertTrue(cookie.startswith("ps2share_session="))

        status, payload, _ = self.fetch("/api/state", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])

    def test_a_forged_cookie_does_not_work(self):
        status, _, _ = self.fetch("/api/state", headers={"Cookie": "ps2share_session=madeup"})
        self.assertEqual(status, 401)


class PasswordHashingTests(unittest.TestCase):
    def test_round_trip(self):
        config = Config()
        self.assertFalse(config.ui_password_required)
        self.assertTrue(config.check_ui_password("anything"))  # no password set

        config.set_ui_password("correct horse")
        self.assertTrue(config.ui_password_required)
        self.assertTrue(config.check_ui_password("correct horse"))
        self.assertFalse(config.check_ui_password("wrong horse"))

    def test_clearing_the_password(self):
        config = Config()
        config.set_ui_password("x" * 10)
        config.set_ui_password("")
        self.assertFalse(config.ui_password_required)

    def test_the_hash_never_leaves_the_server(self):
        config = Config()
        config.set_ui_password("secret value")
        public = config.public_dict()
        self.assertNotIn("ui_password_hash", public)
        self.assertNotIn("ui_password_salt", public)
        self.assertTrue(public["ui_password_required"])


class ConfigPersistenceTests(unittest.TestCase):
    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            config = Config()
            config.config_path = path
            config.share_path = "/srv/ps2"
            config.share_name = "GAMES"
            config.set_ui_password("a strong one")
            config.save()

            loaded = Config.load(path)
            self.assertEqual(loaded.share_path, "/srv/ps2")
            self.assertEqual(loaded.share_name, "GAMES")
            self.assertTrue(loaded.check_ui_password("a strong one"))

    def test_a_missing_file_gives_defaults(self):
        config = Config.load("/nonexistent/path/config.json")
        self.assertEqual(config.share_name, "PS2SMB")

    def test_a_corrupt_file_gives_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "config.json")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("{not json")
            self.assertEqual(Config.load(path).share_name, "PS2SMB")

    def test_normalise_repairs_bad_values(self):
        config = Config()
        config.access_mode = "nonsense"
        config.share_name = "   "
        config.ui_port = "not a number"
        config.normalise()
        self.assertEqual(config.access_mode, "guest")
        self.assertEqual(config.share_name, "PS2SMB")
        self.assertEqual(config.ui_port, 8723)


if __name__ == "__main__":
    unittest.main()
