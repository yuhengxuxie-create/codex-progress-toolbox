"""Small recording HTTP server used to prove that tests never call the internet."""

from __future__ import annotations

import json
import threading
from collections import deque
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass(frozen=True)
class RecordedRequest:
    path: str
    headers: dict[str, str]
    body: bytes

    @property
    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


@dataclass(frozen=True)
class QueuedResponse:
    status: int
    body: bytes
    content_type: str = "application/json"


class RecordingHTTPServer:
    """Context-managed localhost server with deterministic queued responses."""

    def __init__(self) -> None:
        self.requests: list[RecordedRequest] = []
        self._responses: deque[QueuedResponse] = deque()
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> "RecordingHTTPServer":
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802 - stdlib callback name.
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                with owner._lock:
                    owner.requests.append(
                        RecordedRequest(
                            path=self.path,
                            headers={key: value for key, value in self.headers.items()},
                            body=body,
                        )
                    )
                    response = (
                        owner._responses.popleft()
                        if owner._responses
                        else QueuedResponse(200, b"{}")
                    )
                self.send_response(response.status)
                self.send_header("Content-Type", response.content_type)
                self.send_header("Content-Length", str(len(response.body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(response.body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="progress-notify-test-http",
            daemon=True,
        )
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = None
        self._thread = None

    def queue_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        with self._lock:
            self._responses.append(QueuedResponse(status, body))

    def queue_text(
        self,
        text: str,
        status: int = 200,
        content_type: str = "text/plain; charset=utf-8",
    ) -> None:
        with self._lock:
            self._responses.append(
                QueuedResponse(status, text.encode("utf-8"), content_type)
            )

    def url(self, path: str = "/") -> str:
        if self._server is None:
            raise RuntimeError("server is not running")
        if not path.startswith("/"):
            path = "/" + path
        host, port = self._server.server_address
        return f"http://{host}:{port}{path}"
