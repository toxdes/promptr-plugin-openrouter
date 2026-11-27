#!/usr/bin/env python3
"""Exercise the provider against local fixtures."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import json
import os
import selectors
import signal
import subprocess
import tempfile
import threading
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parent.parent


class Peer:
    def __init__(self, executable, environment):
        self.process = subprocess.Popen(
            [str(executable)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=environment, start_new_session=True,
        )
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.buffer = b""

    def send(self, method, params, request_id):
        envelope = {"jsonrpc": "2.0", "method": method, "id": request_id, "params": params}
        self.process.stdin.write(json.dumps(envelope).encode() + b"\n")
        self.process.stdin.flush()

    def read(self, timeout=15):
        deadline = time.monotonic() + timeout
        while b"\n" not in self.buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self.selector.select(remaining):
                raise TimeoutError("Plugin did not answer")
            data = os.read(self.process.stdout.fileno(), 16384)
            if not data:
                raise EOFError("Plugin exited before answering")
            self.buffer += data
        line, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(line)

    def initialize(self, settings, purpose="submit"):
        self.send("provider/initialize", {"protocol_version": "2.0", "config": settings, "purpose": purpose}, 1)
        response = self.read()
        assert response["result"]["protocol_version"] == "2.0", response

    def request(self, params):
        self.send("provider/submit", params, 2)
        output = ""
        deadline = time.monotonic() + 30
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Plugin did not finish within 30 seconds")
            response = self.read(min(15, remaining))
            if "method" in response:
                assert response["params"]["thread_id"] == params["thread_id"]
                assert response["params"]["request_id"] == params["request_id"]
                output += response["params"]["output"]
            elif response["id"] == 2:
                return output, response

    def close(self):
        self.selector.close()
        if self.process.stdin and not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait()
            raise
        finally:
            self.process.stdout.close()
            self.process.stderr.close()


class Backend(BaseHTTPRequestHandler):
    received = []
    mode = "normal"

    def log_message(self, *_args):
        pass

    def do_GET(self):
        body = json.dumps({"data": [{"id": "fixture/first"}, {"id": "fixture/second"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).received.append((body, self.headers["Authorization"]))
        if self.mode == "denied":
            self.send_response(403)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            for text in ("pon", "g"):
                self.wfile.write(f'data: {json.dumps({"id": "chatcmpl-fixture", "object": "chat.completion.chunk", "created": 0, "model": "chat", "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]})}\n\n'.encode())
                self.wfile.flush()
            if self.mode != "truncated":
                self.wfile.write(b'data: {"id":"chatcmpl-fixture","object":"chat.completion.chunk","created":0,"model":"chat","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n')
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


class AdapterTests(unittest.TestCase):

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="promptr-adapters-")
        self.base = Path(self.temporary.name)
        self.environment = {
            **os.environ, "XDG_CONFIG_HOME": str(self.base / "config"),
            "XDG_DATA_HOME": str(self.base / "data"), "XDG_CACHE_HOME": str(self.base / "cache"),
        }
        for name in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            self.environment.pop(name, None)
        self.params = {
            "thread_id": str(uuid.uuid4()), "request_id": str(uuid.uuid4()),
            "messages": [{"role": "user", "content": "one question"}],
            "agent": {"name": "example", "content": "The complete Markdown context."},
            "model": "fixture/model",
        }


    def tearDown(self):
        self.temporary.cleanup()


    @contextmanager
    def peer(self):
        peer = Peer(ROOT / "promptr-openrouter", self.environment)
        try:
            yield peer
        finally:
            peer.close()


    @contextmanager
    def http_backend(self, mode="normal"):
        Backend.mode = mode
        Backend.received = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), Backend)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            yield {"api_key": "fake-token", "base_url": f"http://127.0.0.1:{server.server_port}"}
        finally:
            server.shutdown()
            server.server_close()
            worker.join()


    def test_openrouter_context_stream_and_models(self):
        with self.http_backend() as settings, self.peer() as peer:
            peer.initialize(settings)
            output, response = peer.request(self.params)
            self.assertEqual(output, "pong")
            self.assertEqual(response["result"]["status"], "completed")
            body, auth = Backend.received[0]
            self.assertEqual(auth, "Bearer fake-token")
            self.assertEqual(body["messages"], [
                {"role": "system", "content": "The complete Markdown context."},
                {"role": "user", "content": "one question"},
            ])
        with self.http_backend() as settings, self.peer() as peer:
            peer.initialize(settings)
            peer.send("provider/models", {}, 2)
            self.assertEqual(peer.read()["result"]["models"], ["fixture/first", "fixture/second"])


    def test_openrouter_missing_key_and_public_models(self):
        self.environment.pop("OPENROUTER_API_KEY", None)
        with self.http_backend() as settings:
            settings.pop("api_key")
            with self.peer() as peer:
                peer.send("provider/initialize", {"protocol_version": "2.0", "config": settings}, 1)
                error = peer.read()["error"]
                self.assertEqual(error["code"], -32010)
                self.assertIn("No OpenRouter API key configured", error["message"])
                self.assertIn("Preferences", error["message"])
                self.assertEqual(Backend.received, [])
            with self.peer() as peer:
                peer.initialize(settings, purpose="models")
                peer.send("provider/models", {}, 2)
                self.assertEqual(peer.read()["result"]["models"], ["fixture/first", "fixture/second"])


    def test_openrouter_failures_and_compatibility(self):
        for mode in ("denied", "truncated"):
            with self.subTest(mode=mode), self.http_backend(mode) as settings, self.peer() as peer:
                peer.initialize(settings)
                _output, response = peer.request(self.params)
                self.assertIn("error", response)
        with self.peer() as peer:
            peer.send("provider/initialize", {"protocol_version": "1.0", "config": {}}, 1)
            self.assertEqual(peer.read()["error"]["code"], -32001)



if __name__ == "__main__":
    unittest.main(verbosity=2)
