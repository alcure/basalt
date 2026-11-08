"""The maintenance reservation: the admission closes, the pack can be swapped, nothing in flight is harmed."""
from __future__ import annotations

import json
import socket
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from serve.engine.mock import MockEngine
from serve.frontend import ChatTemplate
from serve.handler import serve
from serve.router import Router
from serve.service import Service
from serve.util.text import ByteTokenizer

ROOT = Path(__file__).resolve().parents[2]
ALIAS = "qwen3.8-flash-next"


class ScriptedEngine(MockEngine):
    """A scripted engine that can be unloaded and loaded again, like the real one."""

    def __init__(self, tokenizer, script: str, **kwargs):
        super().__init__(tokenizer, script, **kwargs)
        self.unloaded = False
        self.model_path = ""

    def alive(self):
        return not self.unloaded

    def restart(self, tries: int = 1):
        self.unloaded = False

    def unload(self):
        self.unloaded = True


class BlockingEngine(ScriptedEngine):
    """The same engine, holding a request until the test lets it go."""

    def __init__(self, tokenizer, script: str, **kwargs):
        super().__init__(tokenizer, script, **kwargs)
        self.generating = threading.Event()
        self.release = threading.Event()

    def generate(self, ids, max_new, sampling, cancel, embeddings=None):
        self.generating.set()
        self.release.wait(30)
        yield from super().generate(ids, max_new, sampling, cancel, embeddings)


class Maintenance(unittest.TestCase):
    def setUp(self):
        tok = ByteTokenizer()
        self.engine = ScriptedEngine(tok, "Hello.")
        self.svc = Service(self.engine, tok, ChatTemplate.from_file(ROOT / "serve/chat_template.jinja"), model_name=ALIAS)
        self.router = Router([self.svc])
        self.httpd = serve(self.router, port=0)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.router.stop()
        self.httpd.server_close()

    def request(self, path, body=None):
        req = urllib.request.Request(self.base + path,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read() or b"{}")

    def chat(self):
        return self.request("/v1/chat/completions", {"model": ALIAS,
                                                     "messages": [{"role": "user", "content": "hi"}]})

    def count_tokens(self):
        return self.request("/v1/messages/count_tokens", {"model": ALIAS,
                                                           "messages": [{"role": "user", "content": "hi"}]})

    def reserve(self, reserve=True):
        return self.request("/v1/unload", {"model": ALIAS, "reserve": reserve})

    def test_reserve_closes_the_admission_and_unloads_between_requests(self):
        status, body = self.reserve()
        self.assertEqual(status, 200)
        self.assertIs(body["maintenance"], True)
        self.assertEqual(body["status"], "reserved")
        self.assertTrue(self.engine.unloaded, "the weights must be gone before the pack file is swapped")

        status, health = self.request("/health")
        self.assertEqual(status, 200)
        self.assertIs(health["maintenance"], True)
        self.assertIn("maintenance_reservation", health["control_features"])

        status, detail = self.request("/v1/status")
        self.assertEqual(status, 200)
        self.assertIs(detail["maintenance"], True)
        self.assertIn("maintenance_reservation", detail["control_features"])

    def test_reserve_refuses_new_work(self):
        self.assertEqual(self.reserve()[0], 200)
        status, body = self.chat()
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["type"], "model_busy")
        status, body = self.count_tokens()
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["type"], "model_busy")

    def test_release_opens_the_admission_again(self):
        self.assertEqual(self.reserve()[0], 200)
        status, body = self.reserve(reserve=False)
        self.assertEqual(status, 200)
        self.assertIs(body["maintenance"], False)
        status, _ = self.chat()
        self.assertEqual(status, 200)

    def test_an_invalid_reserve_changes_nothing(self):
        for bad in ("yes", 1, None, []):
            status, _ = self.request("/v1/unload", {"model": ALIAS, "reserve": bad})
            self.assertEqual(status, 400, f"reserve={bad!r} must be refused")
            status, health = self.request("/health")
            self.assertIs(health["maintenance"], False)
            self.assertEqual(self.chat()[0], 200, "the server must keep serving after a refused reserve")

    def test_a_reserve_without_the_flag_keeps_the_old_unload(self):
        status, body = self.request("/v1/unload", {"model": ALIAS})
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "unloaded")
        self.assertIs(body["maintenance"], False)
        status, health = self.request("/health")
        self.assertIs(health["maintenance"], False)

    def test_the_pack_is_the_one_the_engine_got(self):
        with tempfile.TemporaryDirectory() as folder:
            served = Path(folder) / "served.basalt"
            served.write_bytes(b"the pack the engine was given")
            other = Path(folder) / "other.basalt"
            other.write_bytes(b"another file")
            self.engine.model_path = str(served)

            _, health = self.request("/health")
            info = health["pack"]
            self.assertEqual(info["filename"], "served.basalt")
            self.assertEqual(info["size_bytes"], served.stat().st_size)
            self.assertEqual(info["mtime_ns"], served.stat().st_mtime_ns)
            self.assertNotEqual(info["size_bytes"], other.stat().st_size)

            _, detail = self.request("/v1/status")
            self.assertEqual(detail["pack"], info)

    def test_a_missing_pack_file_is_reported_as_no_pack(self):
        self.engine.model_path = str(Path(tempfile.gettempdir()) / "does-not-exist.basalt")
        _, health = self.request("/health")
        self.assertIsNone(health["pack"])

    def test_a_load_during_the_reservation_is_refused(self):
        """The legacy /load must not answer "loaded" while the reservation holds the door."""
        self.assertEqual(self.reserve()[0], 200)
        status, body = self.request("/load", {})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["type"], "model_busy")
        status, body = self.request("/v1/load", {"model": ALIAS})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["type"], "model_busy")
        _, health = self.request("/health")
        self.assertIs(health["maintenance"], True, "the reservation must still stand")

    def test_a_second_reservation_does_not_take_the_first_one_over(self):
        """A second operation cannot take a reservation it did not make over."""
        self.assertEqual(self.reserve()[0], 200)
        status, body = self.reserve()
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["type"], "model_busy")
        _, health = self.request("/health")
        self.assertIs(health["maintenance"], True, "the standing reservation must be untouched")
        self.assertEqual(self.reserve(reserve=False)[0], 200)

    def test_a_refused_request_does_not_leave_the_body_pending(self):
        """The answer of a refused request must arrive whole, and leave nothing pending."""
        self.assertEqual(self.reserve()[0], 200)
        body = json.dumps({"model": ALIAS, "messages": [{"role": "user", "content": "hi"}]}).encode()
        head = (f"POST /v1/chat/completions HTTP/1.1\r\n"
                f"Host: 127.0.0.1\r\n"
                f"Content-Type: application/json\r\n"
                f"Connection: keep-alive\r\n"
                f"Content-Length: {len(body)}\r\n\r\n").encode()
        with socket.create_connection(("127.0.0.1", self.httpd.server_address[1]), timeout=30) as sock:
            sock.sendall(head + body)
            answer = b""
            while b"\r\n\r\n" not in answer:
                piece = sock.recv(4096)
                self.assertTrue(piece, "the server must answer before closing")
                answer += piece
            head_text, _, rest = answer.partition(b"\r\n\r\n")
            length = 0
            for line in head_text.split(b"\r\n"):
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":", 1)[1])
            while len(rest) < length:
                rest += sock.recv(4096)
            self.assertIn(b"409", head_text.split(b"\r\n")[0])
            self.assertEqual(json.loads(rest[:length])["error"]["type"], "model_busy")
            # the server is done with this connection: nothing of the body is left to be read
            self.assertEqual(sock.recv(4096), b"", "the refused request must not leave the connection open")
        # the server is untouched by the refusal
        self.assertEqual(self.chat()[0], 409)


class ReservationWhileBusy(unittest.TestCase):
    """A request in flight keeps the reservation out and is not harmed by it."""

    def setUp(self):
        tok = ByteTokenizer()
        self.engine = BlockingEngine(tok, "Hello.")
        self.svc = Service(self.engine, tok, ChatTemplate.from_file(ROOT / "serve/chat_template.jinja"), model_name=ALIAS)
        self.router = Router([self.svc])
        self.httpd = serve(self.router, port=0)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.engine.release.set()
        self.httpd.shutdown()
        self.router.stop()
        self.httpd.server_close()

    def request(self, path, body=None):
        req = urllib.request.Request(self.base + path,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read() or b"{}")

    def test_a_running_request_blocks_the_reservation_and_survives_it(self):
        answer = {}

        def run_chat():
            answer["result"] = self.request("/v1/chat/completions",
                                            {"model": ALIAS, "messages": [{"role": "user", "content": "hi"}]})

        thread = threading.Thread(target=run_chat)
        thread.start()
        self.assertTrue(self.engine.generating.wait(10), "the request must be in flight")

        status, body = self.request("/v1/unload", {"model": ALIAS, "reserve": True})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["type"], "model_busy")

        self.engine.release.set()
        thread.join(30)
        self.assertEqual(answer["result"][0], 200, "the request in flight must not be harmed")
        self.assertEqual(answer["result"][1]["choices"][0]["message"]["content"], "Hello.")

        _, health = self.request("/health")
        self.assertIs(health["maintenance"], False, "a refused reservation leaves the admission open")


class SlowUnloadEngine(ScriptedEngine):
    """An engine whose unload takes a while: the reservation closes the door before it finishes."""

    def __init__(self, tokenizer, script: str, **kwargs):
        super().__init__(tokenizer, script, **kwargs)
        self.unloading = threading.Event()
        self.finish_unload = threading.Event()

    def unload(self):
        self.unloading.set()
        self.finish_unload.wait(30)
        super().unload()


class ReservationInProgress(unittest.TestCase):
    """While the reservation unloads, the door is shut and /load may not answer "loaded"."""

    def setUp(self):
        tok = ByteTokenizer()
        self.engine = SlowUnloadEngine(tok, "Hello.")
        self.svc = Service(self.engine, tok, ChatTemplate.from_file(ROOT / "serve/chat_template.jinja"), model_name=ALIAS)
        self.router = Router([self.svc])
        self.httpd = serve(self.router, port=0)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.engine.finish_unload.set()
        self.httpd.shutdown()
        self.router.stop()
        self.httpd.server_close()

    def request(self, path, body=None):
        req = urllib.request.Request(self.base + path,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read() or b"{}")

    def test_controls_cannot_reopen_admission_while_the_reservation_unloads(self):
        answer = {}

        def reserve():
            answer["result"] = self.request("/v1/unload", {"model": ALIAS, "reserve": True})

        thread = threading.Thread(target=reserve)
        thread.start()
        self.assertTrue(self.engine.unloading.wait(10), "the reservation must be unloading")
        self.assertFalse(self.engine.unloaded, "the weights are still in memory while the unload runs")
        status, body = self.request("/v1/unload", {"model": ALIAS, "reserve": False})
        self.assertEqual(status, 409)
        self.assertTrue(self.svc.maintenance)

        status, body = self.request("/load", {})
        self.assertEqual(status, 409, "the door is shut: /load may not answer loaded")
        self.assertEqual(body["error"]["type"], "model_busy")

        self.engine.finish_unload.set()
        thread.join(30)
        self.assertEqual(answer["result"][0], 200)
        self.assertIs(answer["result"][1]["maintenance"], True)


class MultiModelMaintenance(unittest.TestCase):
    """A reservation belongs to its named model even when another model is shown."""

    request = Maintenance.request
    chat = Maintenance.chat
    count_tokens = Maintenance.count_tokens

    def setUp(self):
        tok = ByteTokenizer()
        template = ChatTemplate.from_file(ROOT / "serve/chat_template.jinja")
        self.engines = {name: ScriptedEngine(tok, "Hello.") for name in (ALIAS, "other")}
        self.engines["other"].unloaded = True
        self.services = {name: Service(engine, tok, template, model_name=name)
                         for name, engine in self.engines.items()}
        self.router = Router(list(self.services.values()))
        self.httpd = serve(self.router, port=0)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.router.stop()
        self.httpd.server_close()

    def test_reserving_another_model_closes_admission_without_unloading_the_shown_model(self):
        self.assertEqual(self.chat()[0], 200)
        status, body = self.request("/v1/unload", {"model": "other", "reserve": True})
        self.assertEqual(status, 200, body)
        self.assertIs(body["maintenance"], True)
        self.assertTrue(self.engines["other"].unloaded)
        self.assertFalse(self.engines[ALIAS].unloaded)
        self.assertTrue(self.services["other"].maintenance)
        self.assertFalse(self.services[ALIAS].maintenance)

        _, health = self.request("/health")
        self.assertIs(health["maintenance"], False)
        self.assertEqual(self.chat()[0], 409)
        self.assertEqual(self.count_tokens()[0], 409)
        status, body = self.request("/v1/unload", {"model": "other", "reserve": False})
        self.assertEqual(status, 200, body)
        self.assertFalse(self.services["other"].maintenance)
        self.assertEqual(self.chat()[0], 200)

    def test_unknown_model_cannot_reserve_or_release_a_model(self):
        for reserve in (True, False):
            with self.subTest(reserve=reserve):
                status, _ = self.request("/v1/unload", {"model": "missing", "reserve": reserve})
                self.assertEqual(status, 404)
                self.assertFalse(any(svc.maintenance for svc in self.services.values()))


if __name__ == "__main__":
    unittest.main()
