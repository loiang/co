"""Candidate CLI to official Code Mode host integration over real app-server I/O."""

import json
import os
import select
import shutil
import subprocess
import tempfile
import threading
import time
from collections import deque
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, BinaryIO, Iterator

import pytest


def _required_binary(variable: str) -> Path:
    value = os.environ.get(variable)
    if not value:
        pytest.skip(f"host integration requires {variable}")
    path = Path(value)
    assert path.is_file() and os.access(path, os.X_OK), path
    return path


def _event_stream(events: list[dict[str, Any]]) -> bytes:
    chunks: list[str] = []
    for event in events:
        chunks.append(f"event: {event['type']}\n")
        chunks.append(f"data: {json.dumps(event, separators=(',', ':'))}\n\n")
    return "".join(chunks).encode()


def _created(identifier: str) -> dict[str, Any]:
    return {"type": "response.created", "response": {"id": identifier}}


def _completed(identifier: str) -> dict[str, Any]:
    return {
        "type": "response.completed",
        "response": {
            "id": identifier,
            "usage": {
                "input_tokens": 0,
                "input_tokens_details": None,
                "output_tokens": 0,
                "output_tokens_details": None,
                "total_tokens": 0,
            },
        },
    }


class _ResponsesServer(ThreadingHTTPServer):
    responses: deque[bytes]
    requests: list[dict[str, Any]]


class _ResponsesHandler(BaseHTTPRequestHandler):
    server: _ResponsesServer

    def do_POST(self) -> None:
        """Serve one official-format Responses SSE body and capture its request."""
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length))
        assert isinstance(request, dict)
        self.server.requests.append(request)
        if self.path != "/v1/responses" or not self.server.responses:
            self.send_error(500)
            return
        body = self.server.responses.popleft()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        """Keep the smoke output limited to actionable process diagnostics."""


@contextmanager
def _model_server() -> Iterator[_ResponsesServer]:
    first = _event_stream(
        [
            _created("resp-1"),
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "custom_tool_call",
                    "call_id": "co-host-call",
                    "name": "exec",
                    "input": 'text("co-cli-host-integration");',
                },
            },
            _completed("resp-1"),
        ]
    )
    second = _event_stream(
        [
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "message",
                    "role": "assistant",
                    "id": "msg-1",
                    "content": [{"type": "output_text", "text": "Done"}],
                },
            },
            _completed("resp-2"),
        ]
    )
    server = _ResponsesServer(("127.0.0.1", 0), _ResponsesHandler)
    server.responses = deque((first, second))
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _read_line(source: BinaryIO, timeout: float = 10.0) -> str:
    ready, _, _ = select.select([source], [], [], timeout)
    assert ready, "timed out waiting for child process output"
    line = source.readline()
    assert line, "child process closed stdout"
    return line.decode().rstrip("\n")


def _write_json(output: BinaryIO, value: dict[str, Any]) -> None:
    output.write(json.dumps(value, separators=(",", ":")).encode() + b"\n")
    output.flush()


def _read_until(
    source: BinaryIO, predicate: Any, timeout: float = 20.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        message = json.loads(_read_line(source, max(0.1, deadline - time.monotonic())))
        assert isinstance(message, dict)
        if predicate(message):
            return message
    raise AssertionError("timed out waiting for matching app-server message")


def _rpc(
    output: BinaryIO, identifier: int, method: str, params: dict[str, Any]
) -> None:
    _write_json(output, {"id": identifier, "method": method, "params": params})


def _response(source: BinaryIO, identifier: int) -> dict[str, Any]:
    return _read_until(source, lambda message: message.get("id") == identifier)


def _write_config(home: Path, server: _ResponsesServer) -> None:
    base_url = f"http://127.0.0.1:{server.server_port}/v1"
    content = f'''model = "mock-model"
approval_policy = "never"
sandbox_mode = "read-only"
model_provider = "mock_provider"

[features]
code_mode_only = true

[model_providers.mock_provider]
name = "Local integration provider"
base_url = "{base_url}"
wire_api = "responses"
request_max_retries = 0
stream_max_retries = 0
'''
    (home / "config.toml").write_text(content, encoding="utf-8")


def _start_host(host: Path) -> tuple[subprocess.Popen[bytes], str]:
    process = subprocess.Popen(
        [str(host), "--listen", "grpc://127.0.0.1:0"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    return process, _read_line(process.stdout)


def _stage_stdio_pair(codex: Path, host: Path, home: Path) -> Path:
    install_dir = home / "stdio-bin"
    install_dir.mkdir()
    for source, name in ((codex, "codex"), (host, "codex-code-mode-host")):
        destination = install_dir / name
        try:
            os.link(source, destination)
        except OSError:
            shutil.copy2(source, destination)
    return install_dir / "codex"


def _start_app(
    codex: Path, host_url: str | None, home: Path
) -> subprocess.Popen[bytes]:
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(home)
    environment["CODEX_APP_SERVER_DISABLE_MANAGED_CONFIG"] = "1"
    command = [str(codex), "app-server"]
    if host_url is not None:
        command.extend(("--code-mode-host", host_url))
    return subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
        cwd=home,
    )


def _exercise_app(process: subprocess.Popen[bytes], workspace: Path) -> None:
    assert process.stdin is not None and process.stdout is not None
    _rpc(
        process.stdin,
        1,
        "initialize",
        {
            "clientInfo": {
                "name": "co-host-integration",
                "title": None,
                "version": "1",
            },
            "capabilities": {"experimentalApi": True},
        },
    )
    assert "result" in _response(process.stdout, 1)
    _write_json(process.stdin, {"method": "initialized"})
    _rpc(process.stdin, 2, "thread/start", {"cwd": str(workspace), "ephemeral": True})
    started = _response(process.stdout, 2)
    thread_id = started["result"]["thread"]["id"]
    _rpc(
        process.stdin,
        3,
        "turn/start",
        {"threadId": thread_id, "input": [{"type": "text", "text": "run code mode"}]},
    )
    assert "result" in _response(process.stdout, 3)
    completed = _read_until(
        process.stdout, lambda item: item.get("method") == "turn/completed"
    )
    assert completed["params"]["turn"]["status"] == "completed"


@pytest.mark.parametrize("transport", ["stdio", "grpc"])
def test_candidate_cli_executes_code_through_official_host(transport: str) -> None:
    codex = _required_binary("CODEX_BIN")
    host = _required_binary("CODEX_CODE_MODE_HOST_BIN")
    with (
        _model_server() as server,
        tempfile.TemporaryDirectory(prefix="co-host-home-") as raw,
    ):
        home = Path(raw)
        workspace = home / "workspace"
        workspace.mkdir()
        _write_config(home, server)
        host_process = None
        host_url = None
        if transport == "grpc":
            host_process, host_url = _start_host(host)
        else:
            codex = _stage_stdio_pair(codex, host, home)
        app_process = _start_app(codex, host_url, home)
        try:
            try:
                _exercise_app(app_process, workspace)
            except AssertionError as error:
                if app_process.poll() is not None and app_process.stderr is not None:
                    detail = app_process.stderr.read().decode(errors="replace").strip()
                    raise AssertionError(
                        f"{error}; app-server stderr: {detail}"
                    ) from error
                raise
            assert len(server.requests) == 2
            assert "co-cli-host-integration" in json.dumps(server.requests[1])
        finally:
            app_process.terminate()
            if host_process is not None:
                host_process.terminate()
            app_process.wait(timeout=5)
            if host_process is not None:
                host_process.wait(timeout=5)
