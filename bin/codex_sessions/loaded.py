"""Query the existing Codex app-server for threads loaded in memory.

The archive command must fail closed when the shared app-server cannot prove
which sessions are loaded.  This module performs only the required handshake
and paginated read; it never starts a server or subscribes to a thread.
"""

import asyncio
import json
from pathlib import Path
from typing import Any

from websockets.asyncio.client import ClientConnection, unix_connect

_RPC_TIMEOUT_SECONDS = 5.0
_SOCKET_RELATIVE_PATH = Path("app-server-control/app-server-control.sock")
_WEBSOCKET_URI = "ws://localhost/rpc"
_CLIENT_INFO = {
    "name": "codex_archive_subagents",
    "title": "Codex Archive Subagents",
    "version": "1.0.0",
}


def _protocol_error() -> RuntimeError:
    return RuntimeError("app-server 返回了无效响应")


async def _request(
    connection: ClientConnection,
    request_id: int,
    method: str,
    params: dict[str, object],
) -> dict[str, Any]:
    request = {"id": request_id, "method": method, "params": params}
    async with asyncio.timeout(_RPC_TIMEOUT_SECONDS):
        await connection.send(json.dumps(request, separators=(",", ":")))
        while True:
            raw_response = await connection.recv()
            if not isinstance(raw_response, str):
                raise _protocol_error()
            try:
                response = json.loads(raw_response)
            except json.JSONDecodeError:
                raise _protocol_error() from None
            if not isinstance(response, dict):
                raise _protocol_error()
            if "method" in response and "id" not in response:
                continue
            response_id = response.get("id")
            if (
                not isinstance(response_id, int)
                or isinstance(response_id, bool)
                or response_id != request_id
            ):
                raise _protocol_error()
            if "error" in response:
                raise RuntimeError("app-server 拒绝了请求")
            result = response.get("result")
            if not isinstance(result, dict):
                raise _protocol_error()
            return result


async def _notify_initialized(connection: ClientConnection) -> None:
    notification = {"method": "initialized"}
    async with asyncio.timeout(_RPC_TIMEOUT_SECONDS):
        await connection.send(json.dumps(notification, separators=(",", ":")))


def _validate_server_home(result: dict[str, Any], codex_home: Path) -> None:
    raw_server_home = result.get("codexHome")
    if not isinstance(raw_server_home, str) or not raw_server_home:
        raise _protocol_error()
    server_home = Path(raw_server_home).expanduser()
    if not server_home.is_absolute():
        raise _protocol_error()
    if server_home.resolve(strict=False) != codex_home:
        raise RuntimeError("app-server 使用了不同的 CODEX_HOME")


def _parse_loaded_page(result: dict[str, Any]) -> tuple[list[str], str | None]:
    data = result.get("data")
    if "nextCursor" not in result:
        raise _protocol_error()
    next_cursor = result.get("nextCursor")
    if not isinstance(data, list) or not all(
        isinstance(item, str) and item for item in data
    ):
        raise _protocol_error()
    if next_cursor is not None and (
        not isinstance(next_cursor, str) or not next_cursor
    ):
        raise _protocol_error()
    return data, next_cursor


async def _load_loaded_ids(codex_home: Path) -> set[str]:
    socket_path = codex_home / _SOCKET_RELATIVE_PATH
    try:
        async with unix_connect(
            str(socket_path),
            uri=_WEBSOCKET_URI,
            open_timeout=_RPC_TIMEOUT_SECONDS,
            close_timeout=_RPC_TIMEOUT_SECONDS,
            compression=None,
        ) as connection:
            initialize_result = await _request(
                connection,
                request_id=0,
                method="initialize",
                params={"clientInfo": _CLIENT_INFO},
            )
            _validate_server_home(initialize_result, codex_home)
            await _notify_initialized(connection)

            loaded_ids: set[str] = set()
            seen_cursors: set[str] = set()
            cursor: str | None = None
            request_id = 1
            while True:
                params: dict[str, object] = {}
                if cursor is not None:
                    params["cursor"] = cursor
                result = await _request(
                    connection,
                    request_id=request_id,
                    method="thread/loaded/list",
                    params=params,
                )
                page, next_cursor = _parse_loaded_page(result)
                loaded_ids.update(page)
                if next_cursor is None:
                    return loaded_ids
                if next_cursor in seen_cursors:
                    raise RuntimeError("app-server 返回了重复分页游标")
                seen_cursors.add(next_cursor)
                cursor = next_cursor
                request_id += 1
    except TimeoutError:
        raise RuntimeError("app-server 请求超时") from None
    except RuntimeError:
        raise
    except Exception:
        raise RuntimeError("无法连接现有 app-server") from None


def load_loaded_ids(codex_home: Path) -> set[str]:
    """Return every thread id loaded by the existing shared app-server.

    A fresh Unix WebSocket connection is initialized for the supplied profile,
    then every loaded-list page is consumed under bounded deadlines.  Any
    transport or protocol ambiguity becomes a sanitized failure so callers do
    not archive sessions based on incomplete evidence.

    Args:
        codex_home: Profile root that owns the existing app-server socket.

    Returns:
        The complete set of thread ids reported as loaded in memory.

    Raises:
        RuntimeError: The server is unavailable, mismatched, invalid, or slow.
    """
    normalized_home = codex_home.expanduser().resolve(strict=False)
    try:
        return asyncio.run(_load_loaded_ids(normalized_home))
    except RuntimeError:
        raise
    except Exception:
        raise RuntimeError("无法查询 app-server 已加载线程") from None
