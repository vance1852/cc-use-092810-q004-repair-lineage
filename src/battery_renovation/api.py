"""无第三方依赖的维修与翻新谱系 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .errors import LineageError, ValidationFailed
from .service import RefurbService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    def __init__(self, service: RefurbService) -> None:
        self.service = service

    @staticmethod
    def _actor(headers: Mapping[str, str]) -> str:
        actor = headers.get("x-actor-id", "").strip()
        if not actor:
            raise ValidationFailed("缺少 X-Actor-Id")
        return actor

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

    def handle(self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b"") -> Response:
        normalized = {key.lower(): value for key, value in (headers or {}).items()}
        path = urlparse(target).path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok", "service": "battery-renovation"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            if method == "POST" and path == "/users":
                return Response(201, self.service.create_user(
                    payload["user_id"], payload["display_name"], payload["role"]))
            actor = self._actor(normalized)
            if method == "POST" and path == "/items":
                return Response(201, self.service.register_item(
                    actor, payload["item_id"], payload["item_kind"],
                    payload["model_name"], payload["vendor"], payload.get("state", "in_service")))
            if method == "POST" and len(parts) == 3 and parts[0] == "items" and parts[2] == "memberships":
                return Response(201, self.service.record_as_found_membership(
                    actor, parts[1], payload["child_item_id"], payload["position"]))
            if method == "GET" and len(parts) == 4 and parts[0] == "items" and parts[2] == "lineage":
                if parts[3] == "up":
                    return Response(200, self.service.lineage_up(actor, parts[1]))
                if parts[3] == "down":
                    return Response(200, self.service.lineage_down(actor, parts[1]))
            if method == "GET" and len(parts) == 2 and parts[0] == "items":
                self.service.authorize_read(actor)
                return Response(200, self.service.get_item(parts[1]))
            if method == "POST" and path == "/fault_evidences":
                return Response(201, self.service.record_fault_evidence(
                    actor, payload["evidence_id"], payload["item_id"], payload["evidence_kind"],
                    payload["summary"], payload["content_sha256"]))
            if method == "POST" and path == "/inspections":
                return Response(201, self.service.record_inspection(
                    actor, payload["component_id"], payload["protocol_id"],
                    payload["verdict"], payload["metrics"]))
            if method == "POST" and path == "/repairs":
                return Response(201, self.service.open_repair(
                    actor, payload["repair_order_id"], payload["component_id"]))
            if method == "POST" and len(parts) == 3 and parts[0] == "repairs" and parts[2] == "actions":
                return Response(201, self.service.add_repair_action(
                    actor, parts[1], payload["action_code"], payload["detail"]))
            if method == "POST" and len(parts) == 3 and parts[0] == "repairs" and parts[2] == "complete":
                return Response(200, self.service.complete_repair(
                    actor, parts[1], int(payload["recheck_inspection_id"])))
            if method == "POST" and len(parts) == 3 and parts[0] == "repairs" and parts[2] == "cancel":
                return Response(200, self.service.cancel_repair(
                    actor, parts[1], payload["reason"]))
            if method == "POST" and path == "/components/resolve":
                return Response(200, self.service.resolve_component(
                    actor, payload["component_id"], int(payload["inspection_id"])))
            if method == "POST" and path == "/work_orders/disassembly":
                return Response(201, self.service.create_disassembly(
                    actor, payload["work_order_id"], payload["pack_id"]))
            if method == "POST" and path == "/work_orders/reassembly":
                return Response(201, self.service.create_reassembly(
                    actor, payload["work_order_id"], payload["new_pack_id"],
                    payload["model_name"], payload["vendor"]))
            if len(parts) >= 2 and parts[0] == "work_orders":
                order_id = parts[1]
                if method == "GET" and len(parts) == 2:
                    return Response(200, self.service.get_work_order(order_id))
                if method == "GET" and len(parts) == 3 and parts[2] == "blockers":
                    return Response(200, self.service.delivery_blockers(actor, order_id))
                action = parts[2] if len(parts) == 3 else None
                if method == "POST" and action == "evidence":
                    return Response(201, self.service.attach_evidence(
                        actor, order_id, payload["evidence_id"]))
                if method == "POST" and action == "freeze-disassembly":
                    return Response(200, self.service.freeze_disassembly(
                        actor, order_id, int(payload["expected_revision"])))
                if method == "POST" and action == "start":
                    return Response(200, self.service.start_disassembly(
                        actor, order_id, int(payload["expected_revision"])))
                if method == "POST" and action == "dispositions":
                    return Response(201, self.service.execute_disposition(
                        actor, order_id, payload["component_id"],
                        payload["disposition"], payload.get("note", "")))
                if method == "POST" and action == "complete-disassembly":
                    return Response(200, self.service.complete_disassembly(actor, order_id))
                if method == "POST" and action == "fail":
                    return Response(200, self.service.fail_disassembly(
                        actor, order_id, payload["reason"]))
                if method == "POST" and action == "cancel-disassembly":
                    return Response(200, self.service.cancel_disassembly(
                        actor, order_id, payload["reason"]))
                if method == "POST" and action == "lines":
                    return Response(201, self.service.add_reassembly_line(
                        actor, order_id, payload["component_id"], payload["position"],
                        int(payload["inspection_id"]),
                        None if payload.get("repair_action_id") is None
                        else int(payload["repair_action_id"])))
                if method == "POST" and action == "freeze-reassembly":
                    return Response(200, self.service.freeze_reassembly(
                        actor, order_id, int(payload["expected_revision"])))
                if method == "POST" and action == "start-reassembly":
                    return Response(200, self.service.start_reassembly(
                        actor, order_id, int(payload["expected_revision"])))
                if method == "POST" and action == "technical-confirm":
                    return Response(200, self.service.technical_confirm(
                        actor, order_id, int(payload["expected_revision"])))
                if method == "POST" and action == "quality-release":
                    return Response(200, self.service.quality_release(
                        actor, order_id, int(payload["expected_revision"])))
                if method == "POST" and action == "deliver":
                    return Response(200, self.service.deliver_reassembly(actor, order_id))
                if method == "POST" and action == "cancel-reassembly":
                    return Response(200, self.service.cancel_reassembly(
                        actor, order_id, payload["reason"]))
                if method == "POST" and action == "replace-component":
                    return Response(200, self.service.replace_reassembly_component(
                        actor, order_id, payload["position"], payload["new_component_id"],
                        int(payload["inspection_id"]), payload["reason"],
                        None if payload.get("repair_action_id") is None
                        else int(payload["repair_action_id"])))
            if method == "GET" and path == "/audit/chain":
                return Response(200, self.service.audit_chain(actor))
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except LineageError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "BatteryRenovation/1"

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
    parser = argparse.ArgumentParser(description="启动维修与翻新谱系服务")
    parser.add_argument("--database", type=Path, default=Path("battery_renovation.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database)
    server = ThreadingHTTPServer(
        (args.host, args.port), make_handler(JsonApplication(RefurbService(connection))))
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
