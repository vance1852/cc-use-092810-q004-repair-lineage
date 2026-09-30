"""维修与翻新谱系的无第三方依赖 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .errors import LineageError, ValidationFailed
from .service import LineageService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    """将 HTTP 路由映射到谱系服务，便于无网络单元测试。

    单个 SQLite 连接被多线程服务器共享，请求在锁内串行处理。
    """

    def __init__(self, service: LineageService) -> None:
        self.service = service
        self._lock = threading.RLock()

    @staticmethod
    def _actor(headers: Mapping[str, str]) -> str:
        actor = headers.get("x-actor-id", "").strip()
        if not actor:
            raise ValidationFailed("缺少 X-Actor-Id")
        return actor

    @staticmethod
    def _idempotency_key(headers: Mapping[str, str]) -> str:
        key = headers.get("idempotency-key", "").strip()
        if not key:
            raise ValidationFailed("缺少 Idempotency-Key")
        return key

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return value

    def handle(
        self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b""
    ) -> Response:
        with self._lock:
            return self._dispatch(method, target, headers, body)

    def _dispatch(
        self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b""
    ) -> Response:
        normalized_headers = {key.lower(): value for key, value in (headers or {}).items()}
        path = urlparse(target).path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            if method == "POST" and path == "/users":
                return Response(
                    201, self.service.create_user(payload["user_id"], payload["display_name"], payload["role"])
                )
            actor = self._actor(normalized_headers)
            if method == "POST" and path == "/components":
                return Response(
                    201,
                    self.service.register_component(
                        actor, payload["component_id"], payload["kind"], payload["model_name"],
                        payload.get("state", "reuse"), payload.get("reason", ""),
                    ),
                )
            if method == "POST" and path == "/assemblies":
                return Response(
                    201,
                    self.service.register_assembly(
                        actor, payload["assembly_id"], payload["label"], payload["kind"],
                        payload["origin"], payload.get("members", []),
                    ),
                )
            if method == "GET" and len(parts) == 2 and parts[0] == "assemblies":
                return Response(200, self.service.get_assembly(parts[1]))
            if method == "GET" and len(parts) == 3 and parts[0] == "assemblies" and parts[2] == "origins":
                return Response(200, self.service.assembly_origins(actor, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "assemblies" and parts[2] == "confirm":
                return Response(
                    200,
                    self.service.confirm_assembly(
                        actor, parts[1], int(payload["expected_revision"]), payload.get("note", "")
                    ),
                )
            if method == "POST" and len(parts) == 3 and parts[0] == "assemblies" and parts[2] == "rework":
                return Response(200, self.service.rework_assembly(actor, parts[1], payload["reason"]))
            if method == "POST" and len(parts) == 3 and parts[0] == "assemblies" and parts[2] == "release":
                return Response(
                    200,
                    self.service.release_assembly(
                        actor, parts[1], int(payload["expected_revision"]), payload.get("note", "")
                    ),
                )
            if method == "POST" and len(parts) == 3 and parts[0] == "assemblies" and parts[2] == "ship":
                return Response(
                    200,
                    self.service.ship_assembly(
                        actor, parts[1], int(payload["expected_revision"]), payload["destination"]
                    ),
                )
            if method == "POST" and len(parts) == 3 and parts[0] == "assemblies" and parts[2] == "void":
                return Response(200, self.service.void_assembly(actor, parts[1], payload["reason"]))
            if method == "POST" and path == "/work_orders":
                return Response(
                    201,
                    self.service.create_work_order(
                        actor, payload["work_order_id"], payload["kind"], payload["assembly_id"]
                    ),
                )
            if method == "GET" and len(parts) == 2 and parts[0] == "work_orders":
                return Response(200, self.service.get_work_order(parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "work_orders" and parts[2] == "freeze":
                return Response(
                    200,
                    self.service.freeze_work_order(
                        actor, parts[1], payload["fault_evidence"], int(payload["expected_revision"])
                    ),
                )
            if method == "POST" and len(parts) == 3 and parts[0] == "work_orders" and parts[2] == "disassembly":
                return Response(
                    200,
                    self.service.record_disassembly(
                        actor, parts[1], payload["component_id"], payload["disposition"],
                        payload["reason"], self._idempotency_key(normalized_headers),
                    ),
                )
            if method == "POST" and len(parts) == 3 and parts[0] == "work_orders" and parts[2] == "install":
                return Response(
                    200,
                    self.service.install_component(
                        actor, parts[1], payload["component_id"], payload["position"],
                        int(payload["inspection_id"]),
                        None if payload.get("repair_id") is None else int(payload["repair_id"]),
                        self._idempotency_key(normalized_headers),
                    ),
                )
            if method == "POST" and len(parts) == 3 and parts[0] == "work_orders" and parts[2] == "swap":
                return Response(
                    200,
                    self.service.swap_component(
                        actor, parts[1], payload["position"], payload["component_id"],
                        int(payload["inspection_id"]),
                        None if payload.get("repair_id") is None else int(payload["repair_id"]),
                        payload["reason"], self._idempotency_key(normalized_headers),
                    ),
                )
            if method == "POST" and len(parts) == 3 and parts[0] == "work_orders" and parts[2] == "complete":
                return Response(
                    200, self.service.complete_work_order(actor, parts[1], int(payload["expected_revision"]))
                )
            if method == "POST" and len(parts) == 3 and parts[0] == "work_orders" and parts[2] == "fail":
                return Response(200, self.service.fail_work_order(actor, parts[1], payload["reason"]))
            if method == "POST" and len(parts) == 3 and parts[0] == "work_orders" and parts[2] == "cancel":
                return Response(200, self.service.cancel_work_order(actor, parts[1], payload["reason"]))
            if method == "POST" and len(parts) == 3 and parts[0] == "components" and parts[2] == "inspections":
                return Response(
                    201,
                    self.service.record_inspection(
                        actor, parts[1], payload["result"], payload["metrics"],
                        self._idempotency_key(normalized_headers), payload.get("summary", ""),
                    ),
                )
            if method == "POST" and len(parts) == 3 and parts[0] == "components" and parts[2] == "repairs":
                return Response(201, self.service.open_repair(actor, parts[1], payload["action"]))
            if method == "POST" and len(parts) == 3 and parts[0] == "components" and parts[2] == "redisposition":
                return Response(
                    200, self.service.redisposition_component(actor, parts[1], payload["state"], payload["reason"])
                )
            if method == "GET" and len(parts) == 3 and parts[0] == "components" and parts[2] == "trace":
                return Response(200, self.service.component_trace(actor, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "repairs" and parts[2] == "close":
                return Response(
                    200,
                    self.service.close_repair(
                        actor, int(parts[1]), payload["outcome"], payload.get("note", ""),
                        None if payload.get("inspection_id") is None else int(payload["inspection_id"]),
                    ),
                )
            if method == "GET" and path == "/audit/chain":
                return Response(200, self.service.audit_chain(actor))
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except LineageError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "RefurbLineage/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            response = application.handle(self.command, self.path, dict(self.headers.items()), body)
            encoded = json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动储能电池维修与翻新谱系 HTTP 服务")
    parser.add_argument("--database", type=Path, default=Path("refurb_lineage.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database)
    application = JsonApplication(LineageService(connection))
    server = ThreadingHTTPServer((args.host, args.port), make_handler(application))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
