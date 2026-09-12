import asyncio
import json
import sys
import tempfile
import threading
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
from websockets.asyncio.server import Server, ServerConnection, unix_serve

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "bin"))

from codex_sessions.loaded import load_loaded_ids  # noqa: E402


async def _receive_json(connection: ServerConnection) -> dict[str, object]:
    raw = await connection.recv()
    assert isinstance(raw, str)
    parsed = json.loads(raw)
    assert isinstance(parsed, dict)
    return parsed


async def _complete_handshake(
    connection: ServerConnection,
    codex_home: Path,
) -> None:
    initialize = await _receive_json(connection)
    assert initialize["method"] == "initialize"
    await connection.send(
        json.dumps(
            {
                "id": initialize["id"],
                "result": {"codexHome": str(codex_home.resolve())},
            }
        )
    )
    initialized = await _receive_json(connection)
    assert initialized == {"method": "initialized"}


@pytest.fixture
def codex_home() -> Iterator[Path]:
    """Create a short profile path that fits Linux AF_UNIX limits."""
    with tempfile.TemporaryDirectory(prefix="codex-loaded-", dir="/tmp") as root:
        profile = Path(root) / "home"
        (profile / "app-server-control").mkdir(parents=True)
        yield profile


@contextmanager
def local_app_server(
    socket_path: Path,
    handler: Callable[[ServerConnection], Awaitable[None]],
) -> Iterator[None]:
    loop = asyncio.new_event_loop()
    ready = threading.Event()
    startup_error: list[BaseException] = []

    async def start_server() -> Server:
        return await unix_serve(handler, str(socket_path))

    def run_server() -> None:
        asyncio.set_event_loop(loop)
        try:
            server = loop.run_until_complete(start_server())
        except BaseException as error:
            startup_error.append(error)
            ready.set()
            return
        ready.set()
        loop.run_forever()
        server.close()
        loop.run_until_complete(server.wait_closed())
        loop.close()

    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()
    assert ready.wait(timeout=2)
    if startup_error:
        raise startup_error[0]
    try:
        yield
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        assert not thread.is_alive()


def test_loads_every_page_without_subscribing(codex_home: Path) -> None:
    socket_path = codex_home / "app-server-control/app-server-control.sock"
    requests: list[dict[str, object]] = []

    async def handler(connection: ServerConnection) -> None:
        await _complete_handshake(connection, codex_home)
        first = await _receive_json(connection)
        requests.append(first)
        await connection.send(
            json.dumps(
                {
                    "id": first["id"],
                    "result": {"data": ["thread-a"], "nextCursor": "cursor-a"},
                }
            )
        )
        second = await _receive_json(connection)
        requests.append(second)
        await connection.send(
            json.dumps(
                {
                    "id": second["id"],
                    "result": {"data": ["thread-b"], "nextCursor": None},
                }
            )
        )

    with local_app_server(socket_path, handler):
        assert load_loaded_ids(codex_home) == {"thread-a", "thread-b"}

    assert requests == [
        {"id": 1, "method": "thread/loaded/list", "params": {}},
        {
            "id": 2,
            "method": "thread/loaded/list",
            "params": {"cursor": "cursor-a"},
        },
    ]


def test_rejects_mismatched_codex_home(codex_home: Path) -> None:
    socket_path = codex_home / "app-server-control/app-server-control.sock"

    async def handler(connection: ServerConnection) -> None:
        await _complete_handshake(connection, codex_home.parent / "different-home")

    with local_app_server(socket_path, handler):
        with pytest.raises(RuntimeError, match="不同的 CODEX_HOME"):
            load_loaded_ids(codex_home)


def test_protocol_failure_does_not_leak_response(codex_home: Path) -> None:
    socket_path = codex_home / "app-server-control/app-server-control.sock"

    async def handler(connection: ServerConnection) -> None:
        await _complete_handshake(connection, codex_home)
        request = await _receive_json(connection)
        await connection.send(
            json.dumps(
                {
                    "id": request["id"],
                    "result": {"data": "sensitive-response", "nextCursor": None},
                }
            )
        )

    with local_app_server(socket_path, handler):
        with pytest.raises(RuntimeError) as raised:
            load_loaded_ids(codex_home)

    assert "sensitive-response" not in str(raised.value)
    assert str(raised.value) == "app-server 返回了无效响应"
    assert raised.value.__cause__ is None


def test_rpc_error_does_not_leak_server_detail(codex_home: Path) -> None:
    socket_path = codex_home / "app-server-control/app-server-control.sock"

    async def handler(connection: ServerConnection) -> None:
        await _complete_handshake(connection, codex_home)
        request = await _receive_json(connection)
        await connection.send(
            json.dumps(
                {
                    "id": request["id"],
                    "error": {"code": -32000, "message": "sensitive-detail"},
                }
            )
        )

    with local_app_server(socket_path, handler):
        with pytest.raises(RuntimeError) as raised:
            load_loaded_ids(codex_home)

    assert "sensitive-detail" not in str(raised.value)
    assert str(raised.value) == "app-server 拒绝了请求"


def test_notifications_do_not_extend_rpc_timeout(codex_home: Path) -> None:
    socket_path = codex_home / "app-server-control/app-server-control.sock"

    async def handler(connection: ServerConnection) -> None:
        await _complete_handshake(connection, codex_home)
        await _receive_json(connection)
        while True:
            await connection.send(json.dumps({"method": "server/notification"}))
            await asyncio.sleep(0.02)

    with (
        local_app_server(socket_path, handler),
        patch("codex_sessions.loaded._RPC_TIMEOUT_SECONDS", 0.08),
        pytest.raises(RuntimeError, match="请求超时"),
    ):
        load_loaded_ids(codex_home)


def test_rejects_repeated_pagination_cursor(codex_home: Path) -> None:
    socket_path = codex_home / "app-server-control/app-server-control.sock"

    async def handler(connection: ServerConnection) -> None:
        await _complete_handshake(connection, codex_home)
        for _ in range(2):
            request = await _receive_json(connection)
            await connection.send(
                json.dumps(
                    {
                        "id": request["id"],
                        "result": {"data": [], "nextCursor": "same-cursor"},
                    }
                )
            )

    with local_app_server(socket_path, handler):
        with pytest.raises(RuntimeError, match="重复分页游标"):
            load_loaded_ids(codex_home)
