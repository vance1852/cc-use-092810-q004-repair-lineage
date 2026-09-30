"""维修与翻新谱系的领域用例。

核心不变量：
- 工单冻结进场配置与故障证据后才允许拆解；
- 拆出组件必须进入复用、维修、报废或隔离之一；
- 任一序列件同一时刻最多属于一个有效装配（数据库部分唯一索引兜底）；
- 组包装机必须引用组件最新合格检测版本，维修件必须同时引用已完工维修动作；
- 技术确认与质量放行由不同角色、不同人员完成；
- 撤销、返工、换件、部分失败都在事务内完成，组件不会失联或一物多装；
- 已出场装配的装机事实只增不改，历史配置保持可回放。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Iterable, Mapping

from .clock import SystemClock, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .jsonutil import canonical_json, content_digest
from .models import (
    ASSEMBLY_KINDS,
    ASSEMBLY_ORIGINS,
    COMPONENT_KINDS,
    DISPOSITIONS,
    INSPECTION_RESULTS,
    WORK_ORDER_KINDS,
    choice,
    fault_evidence,
    identifier,
    metrics_map,
    required_text,
)
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "intake": {
        "component.register", "assembly.register", "assembly.install", "assembly.swap",
        "assembly.ship", "assembly.void", "workorder.create", "workorder.freeze",
        "workorder.close", "workorder.cancel", "disassembly.record", "lineage.read",
    },
    "technician": {
        "inspection.record", "repair.open", "repair.close", "assembly.confirm",
        "assembly.rework", "lineage.read",
    },
    "quality": {"component.redisposition", "assembly.release", "lineage.read"},
    "auditor": {"lineage.read", "audit.read"},
}

MAX_TRACE_DEPTH = 16


class LineageService:
    """在单个 SQLite 连接上提供维修翻新谱系的全部业务操作。"""

    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    # ------------------------------------------------------------------
    # 用户、权限与审计
    # ------------------------------------------------------------------

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT user_id, display_name, role, active FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"用户不存在: {user_id}")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self, entity_type: str, entity_id: str, event_type: str, actor_id: str, payload: Mapping[str, Any]
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed(f"未知角色: {role}")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"用户已存在: {user_id}") from exc
        return {"user_id": user_id.strip(), "role": role}

    # ------------------------------------------------------------------
    # 组件与装配登记
    # ------------------------------------------------------------------

    def _component(self, component_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM components WHERE component_id=?", (component_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"组件不存在: {component_id}")
        return row

    def _assembly(self, assembly_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM assemblies WHERE assembly_id=?", (assembly_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"装配不存在: {assembly_id}")
        return row

    def _work_order(self, work_order_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM work_orders WHERE work_order_id=?", (work_order_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"工单不存在: {work_order_id}")
        return row

    def _set_component_state(self, component_id: str, state: str, reason: str) -> None:
        self.connection.execute(
            "UPDATE components SET state=?,state_reason=?,revision=revision+1,updated_at=? "
            "WHERE component_id=?",
            (state, reason, self._now(), component_id),
        )

    def register_component(
        self,
        actor_id: str,
        component_id: str,
        kind: str,
        model_name: str,
        state: str = "reuse",
        reason: str = "",
    ) -> dict[str, Any]:
        """登记散装序列件（备件等），进场包内组件由 register_assembly 一并登记。"""

        self._require(actor_id, "component.register")
        component_id = identifier(component_id, "component_id")
        kind = choice(kind, "kind", COMPONENT_KINDS)
        model_name = required_text(model_name, "model_name")
        if state not in {"reuse", "quarantine"}:
            raise ValidationFailed("散装组件初始状态只能是 reuse 或 quarantine")
        reason = required_text(reason, "reason", 512) if reason else "登记入库"
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO components(component_id,kind,model_name,state,state_reason,created_by,"
                    "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (component_id, kind, model_name, state, reason, actor_id, self._now(), self._now()),
                )
                self._audit(
                    "component", component_id, "component.registered", actor_id,
                    {"kind": kind, "model_name": model_name, "state": state},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"组件序列号已存在: {component_id}") from exc
        return {"component_id": component_id, "kind": kind, "state": state}

    def register_assembly(
        self,
        actor_id: str,
        assembly_id: str,
        label: str,
        kind: str,
        origin: str,
        members: Iterable[Mapping[str, Any]] = (),
    ) -> dict[str, Any]:
        """登记装配。external 必须带进场组件清单；refurb 创建空装配等待组包工单。"""

        self._require(actor_id, "assembly.register")
        assembly_id = identifier(assembly_id, "assembly_id")
        label = required_text(label, "label")
        kind = choice(kind, "kind", ASSEMBLY_KINDS)
        origin = choice(origin, "origin", ASSEMBLY_ORIGINS)
        member_list = [dict(item) for item in members]
        if origin == "external" and not member_list:
            raise ValidationFailed("退役进场装配必须登记进场组件清单")
        if origin == "refurb" and member_list:
            raise ValidationFailed("翻新装配创建时不携带组件，请通过组包工单装机")
        parsed_members: list[dict[str, str]] = []
        seen_components: set[str] = set()
        seen_positions: set[str] = set()
        for item in member_list:
            member = {
                "component_id": identifier(item.get("component_id"), "component_id"),
                "kind": choice(item.get("kind"), "kind", COMPONENT_KINDS),
                "model_name": required_text(item.get("model_name"), "model_name"),
                "position": required_text(item.get("position"), "position", 64),
            }
            if member["component_id"] in seen_components:
                raise ValidationFailed(f"组件在清单中重复: {member['component_id']}")
            if member["position"] in seen_positions:
                raise ValidationFailed(f"槽位在清单中重复: {member['position']}")
            seen_components.add(member["component_id"])
            seen_positions.add(member["position"])
            parsed_members.append(member)
        try:
            with transaction(self.connection, immediate=True):
                now = self._now()
                self.connection.execute(
                    "INSERT INTO assemblies(assembly_id,label,kind,origin,state,created_by,created_at) "
                    "VALUES(?,?,?,?,'open',?,?)",
                    (assembly_id, label, kind, origin, actor_id, now),
                )
                for member in parsed_members:
                    self.connection.execute(
                        "INSERT INTO components(component_id,kind,model_name,state,state_reason,created_by,"
                        "created_at,updated_at) VALUES(?,?,?,'installed','随退役装配进场',?,?,?)",
                        (member["component_id"], member["kind"], member["model_name"], actor_id, now, now),
                    )
                    self.connection.execute(
                        "INSERT INTO assembly_members(assembly_id,component_id,position,installed_by,installed_at) "
                        "VALUES(?,?,?,?,?)",
                        (assembly_id, member["component_id"], member["position"], actor_id, now),
                    )
                self._audit(
                    "assembly", assembly_id, "assembly.registered", actor_id,
                    {"label": label, "origin": origin, "members": [m["component_id"] for m in parsed_members]},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("装配编号、槽位或组件序列号冲突") from exc
        return {"assembly_id": assembly_id, "state": "open", "members": len(parsed_members)}

    def get_assembly(self, assembly_id: str) -> dict[str, Any]:
        assembly = dict(self._assembly(assembly_id))
        members = self.connection.execute(
            "SELECT m.member_id,m.component_id,m.position,m.inspection_id,m.repair_id,m.installed_by,"
            "m.installed_at,c.kind,c.model_name,c.state AS component_state "
            "FROM assembly_members m JOIN components c ON c.component_id=m.component_id "
            "WHERE m.assembly_id=? AND m.removed_at IS NULL ORDER BY m.position",
            (assembly_id,),
        ).fetchall()
        assembly["members"] = [dict(row) for row in members]
        return assembly

    # ------------------------------------------------------------------
    # 工单：创建、冻结、拆解处置、关闭
    # ------------------------------------------------------------------

    def create_work_order(self, actor_id: str, work_order_id: str, kind: str, assembly_id: str) -> dict[str, Any]:
        self._require(actor_id, "workorder.create")
        work_order_id = identifier(work_order_id, "work_order_id")
        kind = choice(kind, "kind", WORK_ORDER_KINDS)
        assembly = self._assembly(identifier(assembly_id, "assembly_id"))
        if kind == "teardown":
            external_intake = assembly["origin"] == "external" and assembly["state"] == "open"
            returned = assembly["state"] == "shipped"
            if not (external_intake or returned):
                raise InvalidState("只有外部进场或已出场返场的装配可以开立拆解工单")
            state, source, target = "draft", assembly_id, None
        else:
            if assembly["origin"] != "refurb" or assembly["state"] != "open":
                raise InvalidState("只有未确认的翻新装配可以开立组包工单")
            state, source, target = "in_progress", None, assembly_id
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO work_orders(work_order_id,kind,source_assembly_id,target_assembly_id,state,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (work_order_id, kind, source, target, state, actor_id, self._now()),
                )
                self._audit(
                    "work_order", work_order_id, "work_order.created", actor_id,
                    {"kind": kind, "assembly_id": assembly_id},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("工单编号冲突或该装配已有进行中的工单") from exc
        return {"work_order_id": work_order_id, "kind": kind, "state": state}

    def get_work_order(self, work_order_id: str) -> dict[str, Any]:
        work_order = dict(self._work_order(work_order_id))
        items = self.connection.execute(
            "SELECT i.component_id,i.position,i.disposition,i.disposition_reason,i.dispositioned_by,"
            "i.dispositioned_at,c.state AS component_state "
            "FROM work_order_intake_items i JOIN components c ON c.component_id=i.component_id "
            "WHERE i.work_order_id=? ORDER BY i.position",
            (work_order_id,),
        ).fetchall()
        work_order["intake_items"] = [dict(row) for row in items]
        if work_order["fault_evidence_json"] is not None:
            work_order["fault_evidence"] = json.loads(work_order["fault_evidence_json"])
        work_order.pop("fault_evidence_json", None)
        return work_order

    def freeze_work_order(
        self, actor_id: str, work_order_id: str, evidence: Mapping[str, Any], expected_revision: int
    ) -> dict[str, Any]:
        """冻结进场配置快照与故障证据；冻结后才允许拆解。"""

        self._require(actor_id, "workorder.freeze")
        evidence_dict = fault_evidence(evidence)
        work_order = self._work_order(work_order_id)
        if work_order["kind"] != "teardown":
            raise ValidationFailed("只有拆解工单需要冻结")
        members = self.connection.execute(
            "SELECT component_id,position FROM assembly_members "
            "WHERE assembly_id=? AND removed_at IS NULL ORDER BY position",
            (work_order["source_assembly_id"],),
        ).fetchall()
        if not members:
            raise InvalidState("进场装配没有可拆解组件")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE work_orders SET state='frozen',fault_evidence_json=?,frozen_at=?,revision=revision+1 "
                "WHERE work_order_id=? AND state='draft' AND revision=?",
                (canonical_json(evidence_dict), self._now(), work_order_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("工单不是当前草稿版本")
            for member in members:
                self.connection.execute(
                    "INSERT INTO work_order_intake_items(work_order_id,component_id,position) VALUES(?,?,?)",
                    (work_order_id, member["component_id"], member["position"]),
                )
            self._audit(
                "work_order", work_order_id, "work_order.frozen", actor_id,
                {"components": [m["component_id"] for m in members], "evidence": evidence_dict},
            )
        return self.get_work_order(work_order_id)

    def _idempotent_response(self, scope: str, key: str, request_digest: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT request_sha256,response_json FROM idempotency_keys WHERE scope=? AND key=?",
            (scope, key),
        ).fetchone()
        if row is None:
            return None
        if row["request_sha256"] != request_digest:
            raise Conflict("同一幂等键对应了不同请求内容")
        return json.loads(row["response_json"])

    def _store_idempotent(self, scope: str, key: str, request_digest: str, response: Mapping[str, Any]) -> None:
        self.connection.execute(
            "INSERT INTO idempotency_keys(scope,key,request_sha256,response_json,created_at) VALUES(?,?,?,?,?)",
            (scope, key, request_digest, canonical_json(response), self._now()),
        )

    @staticmethod
    def _idempotency_key(key: object) -> str:
        return identifier(key, "idempotency_key")

    def _latest_inspection(self, component_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM inspections WHERE component_id=? ORDER BY version DESC LIMIT 1",
            (component_id,),
        ).fetchone()

    def record_disassembly(
        self,
        actor_id: str,
        work_order_id: str,
        component_id: str,
        disposition: str,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """把冻结清单中的组件从源装配拆出并处置到复用/维修/报废/隔离。"""

        self._require(actor_id, "disassembly.record")
        disposition = choice(disposition, "disposition", DISPOSITIONS)
        reason = required_text(reason, "reason", 512)
        key = self._idempotency_key(idempotency_key)
        work_order = self._work_order(work_order_id)
        if work_order["kind"] != "teardown":
            raise ValidationFailed("只有拆解工单可以登记拆解处置")
        if work_order["state"] not in ("frozen", "in_progress"):
            raise InvalidState("工单未冻结或已关闭，不能拆解")
        item = self.connection.execute(
            "SELECT * FROM work_order_intake_items WHERE work_order_id=? AND component_id=?",
            (work_order_id, component_id),
        ).fetchone()
        if item is None:
            raise NotFound("组件不在冻结的进场配置中")
        request = {
            "work_order_id": work_order_id,
            "component_id": component_id,
            "disposition": disposition,
            "reason": reason,
        }
        request_digest = content_digest([request])
        scope = f"disassembly:{work_order_id}"
        existing = self._idempotent_response(scope, key, request_digest)
        if existing is not None:
            return existing
        if item["disposition"] is not None:
            raise Conflict("组件已处置，不能重复拆解")
        member = self.connection.execute(
            "SELECT member_id FROM assembly_members WHERE assembly_id=? AND component_id=? AND removed_at IS NULL",
            (work_order["source_assembly_id"], component_id),
        ).fetchone()
        if member is None:
            raise Conflict("组件已不在源装配中，进场配置与实物不一致")
        if disposition == "reuse":
            latest = self._latest_inspection(component_id)
            if latest is None or latest["result"] != "pass":
                raise ValidationFailed("复用处置需要最近一次检测合格")
        response = {
            "work_order_id": work_order_id,
            "component_id": component_id,
            "disposition": disposition,
        }
        try:
            with transaction(self.connection, immediate=True):
                now = self._now()
                cursor = self.connection.execute(
                    "UPDATE assembly_members SET removed_at=?,removed_by=?,removal_reason='disassembled' "
                    "WHERE member_id=? AND removed_at IS NULL",
                    (now, actor_id, member["member_id"]),
                )
                if cursor.rowcount != 1:
                    raise Conflict("组件在源装配中的状态已变化")
                self._set_component_state(component_id, disposition, reason)
                self.connection.execute(
                    "UPDATE work_order_intake_items SET disposition=?,disposition_reason=?,"
                    "dispositioned_by=?,dispositioned_at=? WHERE work_order_id=? AND component_id=?",
                    (disposition, reason, actor_id, now, work_order_id, component_id),
                )
                self.connection.execute(
                    "UPDATE work_orders SET state='in_progress' WHERE work_order_id=? AND state='frozen'",
                    (work_order_id,),
                )
                self._store_idempotent(scope, key, request_digest, response)
                self._audit(
                    "component", component_id, "component.dispositioned", actor_id,
                    {"work_order_id": work_order_id, "disposition": disposition, "reason": reason},
                )
                self._audit(
                    "work_order", work_order_id, "work_order.disassembly_recorded", actor_id,
                    {"component_id": component_id, "disposition": disposition},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("拆解处置并发冲突") from exc
        return response

    def complete_work_order(self, actor_id: str, work_order_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "workorder.close")
        work_order = self._work_order(work_order_id)
        with transaction(self.connection, immediate=True):
            if work_order["kind"] == "teardown":
                remaining = self.connection.execute(
                    "SELECT count(*) FROM work_order_intake_items WHERE work_order_id=? AND disposition IS NULL",
                    (work_order_id,),
                ).fetchone()[0]
                if remaining:
                    raise InvalidState(f"还有 {remaining} 个组件未处置，不能完工")
                cursor = self.connection.execute(
                    "UPDATE work_orders SET state='completed',revision=revision+1,closed_by=?,closed_at=? "
                    "WHERE work_order_id=? AND state IN ('frozen','in_progress') AND revision=?",
                    (actor_id, self._now(), work_order_id, expected_revision),
                )
                if cursor.rowcount != 1:
                    raise InvalidState("工单状态或版本已变化")
                left = self.connection.execute(
                    "SELECT count(*) FROM assembly_members WHERE assembly_id=? AND removed_at IS NULL",
                    (work_order["source_assembly_id"],),
                ).fetchone()[0]
                if left == 0:
                    self.connection.execute(
                        "UPDATE assemblies SET state='dismantled',closed_at=?,revision=revision+1 "
                        "WHERE assembly_id=? AND state IN ('open','shipped')",
                        (self._now(), work_order["source_assembly_id"]),
                    )
                    self._audit(
                        "assembly", work_order["source_assembly_id"], "assembly.dismantled", actor_id,
                        {"work_order_id": work_order_id},
                    )
            else:
                installed = self.connection.execute(
                    "SELECT count(*) FROM assembly_members WHERE assembly_id=? AND removed_at IS NULL",
                    (work_order["target_assembly_id"],),
                ).fetchone()[0]
                if installed == 0:
                    raise InvalidState("组包工单没有已装机组件，不能完工")
                cursor = self.connection.execute(
                    "UPDATE work_orders SET state='completed',revision=revision+1,closed_by=?,closed_at=? "
                    "WHERE work_order_id=? AND state='in_progress' AND revision=?",
                    (actor_id, self._now(), work_order_id, expected_revision),
                )
                if cursor.rowcount != 1:
                    raise InvalidState("工单状态或版本已变化")
            self._audit(
                "work_order", work_order_id, "work_order.completed", actor_id,
                {"kind": work_order["kind"]},
            )
        return self.get_work_order(work_order_id)

    def fail_work_order(self, actor_id: str, work_order_id: str, reason: str) -> dict[str, Any]:
        """部分失败关闭：未处置组件留在源装配，已装机组件留在目标装配，不失联。"""

        self._require(actor_id, "workorder.close")
        reason = required_text(reason, "reason", 512)
        work_order = self._work_order(work_order_id)
        allowed = ("frozen", "in_progress") if work_order["kind"] == "teardown" else ("in_progress",)
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                f"UPDATE work_orders SET state='failed',revision=revision+1,closed_by=?,closed_at=?,close_note=? "
                f"WHERE work_order_id=? AND state IN ({','.join('?' * len(allowed))})",
                (actor_id, self._now(), reason, work_order_id, *allowed),
            )
            if cursor.rowcount != 1:
                raise InvalidState("工单当前状态不能标记失败")
            remaining = None
            if work_order["kind"] == "teardown":
                remaining = self.connection.execute(
                    "SELECT count(*) FROM work_order_intake_items WHERE work_order_id=? AND disposition IS NULL",
                    (work_order_id,),
                ).fetchone()[0]
            self._audit(
                "work_order", work_order_id, "work_order.failed", actor_id,
                {"reason": reason, "undispositioned": remaining},
            )
        return self.get_work_order(work_order_id)

    def cancel_work_order(self, actor_id: str, work_order_id: str, reason: str) -> dict[str, Any]:
        """撤销工单：不改变任何组件状态；已装机组件留在装配内，可再撤销装配退回。"""

        self._require(actor_id, "workorder.cancel")
        reason = required_text(reason, "reason", 512)
        work_order = self._work_order(work_order_id)
        allowed = ("draft", "frozen", "in_progress") if work_order["kind"] == "teardown" else ("in_progress",)
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                f"UPDATE work_orders SET state='cancelled',revision=revision+1,closed_by=?,closed_at=?,close_note=? "
                f"WHERE work_order_id=? AND state IN ({','.join('?' * len(allowed))})",
                (actor_id, self._now(), reason, work_order_id, *allowed),
            )
            if cursor.rowcount != 1:
                raise InvalidState("工单当前状态不能撤销")
            self._audit("work_order", work_order_id, "work_order.cancelled", actor_id, {"reason": reason})
        return self.get_work_order(work_order_id)

    # ------------------------------------------------------------------
    # 检测与维修
    # ------------------------------------------------------------------

    def record_inspection(
        self,
        actor_id: str,
        component_id: str,
        result: str,
        metrics: Mapping[str, Any],
        idempotency_key: str,
        summary: str = "",
    ) -> dict[str, Any]:
        """记录组件检测的不可变版本；在库组件检测不合格会自动转入隔离。"""

        self._require(actor_id, "inspection.record")
        result = choice(result, "result", INSPECTION_RESULTS)
        metrics_dict = metrics_map(metrics)
        summary = required_text(summary, "summary", 512) if summary else ""
        key = self._idempotency_key(idempotency_key)
        component = self._component(component_id)
        if component["state"] == "scrap":
            raise InvalidState("报废组件不能记录检测")
        request = {
            "component_id": component_id,
            "result": result,
            "metrics": metrics_dict,
            "summary": summary,
        }
        request_digest = content_digest([request])
        scope = f"inspection:{component_id}"
        existing = self._idempotent_response(scope, key, request_digest)
        if existing is not None:
            return existing
        try:
            with transaction(self.connection, immediate=True):
                row = self.connection.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 AS next_version FROM inspections WHERE component_id=?",
                    (component_id,),
                ).fetchone()
                version = int(row["next_version"])
                content_sha256 = content_digest([request | {"version": version}])
                cursor = self.connection.execute(
                    "INSERT INTO inspections(component_id,version,result,summary,metrics_json,content_sha256,"
                    "recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        component_id, version, result, summary, canonical_json(metrics_dict),
                        content_sha256, actor_id, self._now(),
                    ),
                )
                inspection_id = int(cursor.lastrowid)
                quarantined = False
                if result == "fail" and component["state"] == "reuse":
                    self._set_component_state(component_id, "quarantine", "检测不合格自动隔离")
                    quarantined = True
                    self._audit(
                        "component", component_id, "component.quarantined", actor_id,
                        {"inspection_id": inspection_id},
                    )
                response = {
                    "inspection_id": inspection_id,
                    "component_id": component_id,
                    "version": version,
                    "result": result,
                    "component_state": "quarantine" if quarantined else component["state"],
                }
                self._store_idempotent(scope, key, request_digest, response)
                self._audit(
                    "component", component_id, "inspection.recorded", actor_id,
                    {"inspection_id": inspection_id, "version": version, "result": result},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("检测版本或内容摘要冲突") from exc
        return response

    def open_repair(self, actor_id: str, component_id: str, action: str) -> dict[str, Any]:
        self._require(actor_id, "repair.open")
        action = required_text(action, "action", 512)
        component = self._component(component_id)
        if component["state"] != "repair":
            raise InvalidState("只有维修状态的组件可以开立维修动作")
        try:
            with transaction(self.connection, immediate=True):
                row = self.connection.execute(
                    "SELECT COALESCE(MAX(version), 0) + 1 AS next_version FROM repair_actions WHERE component_id=?",
                    (component_id,),
                ).fetchone()
                version = int(row["next_version"])
                cursor = self.connection.execute(
                    "INSERT INTO repair_actions(component_id,version,action,state,opened_by,opened_at) "
                    "VALUES(?,?,?,'open',?,?)",
                    (component_id, version, action, actor_id, self._now()),
                )
                repair_id = int(cursor.lastrowid)
                self._audit(
                    "component", component_id, "repair.opened", actor_id,
                    {"repair_id": repair_id, "version": version, "action": action},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("该组件已存在进行中的维修动作") from exc
        return {"repair_id": repair_id, "component_id": component_id, "version": version, "state": "open"}

    def close_repair(
        self,
        actor_id: str,
        repair_id: int,
        outcome: str,
        note: str = "",
        inspection_id: int | None = None,
    ) -> dict[str, Any]:
        """完工需要维修开始后的合格检测版本；放弃则组件保持维修状态可再开立。"""

        self._require(actor_id, "repair.close")
        outcome = choice(outcome, "outcome", {"completed", "abandoned"})
        note = required_text(note, "note", 512) if note else ""
        repair = self.connection.execute(
            "SELECT * FROM repair_actions WHERE repair_id=?", (repair_id,)
        ).fetchone()
        if repair is None:
            raise NotFound("维修动作不存在")
        if repair["state"] != "open":
            raise InvalidState("维修动作已经关闭")
        component = self._component(repair["component_id"])
        if outcome == "completed":
            if inspection_id is None:
                raise ValidationFailed("维修完工必须引用维修后的合格检测版本")
            inspection = self.connection.execute(
                "SELECT * FROM inspections WHERE inspection_id=?", (inspection_id,)
            ).fetchone()
            if inspection is None or inspection["component_id"] != repair["component_id"]:
                raise ValidationFailed("检测版本不属于该组件")
            if inspection["result"] != "pass" or inspection["recorded_at"] < repair["opened_at"]:
                raise ValidationFailed("维修完工需要维修开始后的合格检测版本")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE repair_actions SET state=?,closed_by=?,closed_at=?,close_note=?,close_inspection_id=? "
                "WHERE repair_id=? AND state='open'",
                (
                    outcome, actor_id, self._now(), note,
                    inspection_id if outcome == "completed" else None, repair_id,
                ),
            )
            if cursor.rowcount != 1:
                raise InvalidState("维修动作状态已变化")
            if outcome == "completed":
                self._set_component_state(repair["component_id"], "reuse", "维修完工复检合格")
            self._audit(
                "component", repair["component_id"], f"repair.{outcome}", actor_id,
                {"repair_id": repair_id, "inspection_id": inspection_id, "note": note},
            )
        return {
            "repair_id": repair_id,
            "state": outcome,
            "component_state": "reuse" if outcome == "completed" else component["state"],
        }

    def redisposition_component(self, actor_id: str, component_id: str, new_state: str, reason: str) -> dict[str, Any]:
        """质量角色对隔离/维修中的组件重新判定去向。"""

        self._require(actor_id, "component.redisposition")
        new_state = choice(new_state, "state", DISPOSITIONS)
        reason = required_text(reason, "reason", 512)
        component = self._component(component_id)
        if component["state"] not in ("quarantine", "repair"):
            raise InvalidState("只有隔离或维修中的组件可以重新判定")
        if component["state"] == new_state:
            raise ValidationFailed("目标状态与当前状态相同")
        open_repair = self.connection.execute(
            "SELECT repair_id FROM repair_actions WHERE component_id=? AND state='open'",
            (component_id,),
        ).fetchone()
        if open_repair is not None:
            raise InvalidState("组件存在进行中的维修动作，不能重新判定")
        if new_state == "reuse":
            latest = self._latest_inspection(component_id)
            if latest is None or latest["result"] != "pass":
                raise ValidationFailed("判定复用需要最近一次检测合格")
        with transaction(self.connection, immediate=True):
            self._set_component_state(component_id, new_state, reason)
            self._audit(
                "component", component_id, "component.redispositioned", actor_id,
                {"from": component["state"], "to": new_state, "reason": reason},
            )
        return {"component_id": component_id, "state": new_state}

    # ------------------------------------------------------------------
    # 组包：装机、换件、确认、放行、出场、撤销、返工
    # ------------------------------------------------------------------

    def _rebuild_target(self, work_order_id: str) -> tuple[sqlite3.Row, sqlite3.Row]:
        work_order = self._work_order(work_order_id)
        if work_order["kind"] != "rebuild":
            raise ValidationFailed("只有组包工单可以执行装机操作")
        if work_order["state"] != "in_progress":
            raise InvalidState("组包工单不在进行中")
        assembly = self._assembly(work_order["target_assembly_id"])
        if assembly["state"] != "open":
            raise InvalidState("目标装配不在可装机状态")
        return work_order, assembly

    def _install_evidence(
        self, component: sqlite3.Row, inspection_id: object, repair_id: object
    ) -> tuple[int, int | None]:
        """校验装机引用的确定版本检测与维修动作。"""

        if isinstance(inspection_id, bool) or not isinstance(inspection_id, int):
            raise ValidationFailed("inspection_id 必须是整数")
        inspection = self.connection.execute(
            "SELECT * FROM inspections WHERE inspection_id=?", (inspection_id,)
        ).fetchone()
        if inspection is None or inspection["component_id"] != component["component_id"]:
            raise ValidationFailed("装机检测版本不属于该组件")
        if inspection["result"] != "pass":
            raise ValidationFailed("装机检测版本必须合格")
        latest = self._latest_inspection(component["component_id"])
        if latest is None or latest["inspection_id"] != inspection["inspection_id"]:
            raise ValidationFailed("装机检测必须是组件最新的检测版本")
        completed_repairs = self.connection.execute(
            "SELECT * FROM repair_actions WHERE component_id=? AND state='completed' "
            "ORDER BY repair_id DESC",
            (component["component_id"],),
        ).fetchall()
        pinned_repair: int | None = None
        if completed_repairs:
            if repair_id is None:
                raise ValidationFailed("经过维修的组件装机必须引用已完工的维修动作")
            if isinstance(repair_id, bool) or not isinstance(repair_id, int):
                raise ValidationFailed("repair_id 必须是整数")
            matched = any(row["repair_id"] == repair_id for row in completed_repairs)
            if not matched:
                raise ValidationFailed("引用的维修动作不存在或未完工")
            pinned_repair = repair_id
        elif repair_id is not None:
            raise ValidationFailed("组件没有已完工维修，不能引用维修动作")
        return inspection_id, pinned_repair

    def install_component(
        self,
        actor_id: str,
        work_order_id: str,
        component_id: str,
        position: str,
        inspection_id: int,
        repair_id: int | None,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "assembly.install")
        position = required_text(position, "position", 64)
        key = self._idempotency_key(idempotency_key)
        _, assembly = self._rebuild_target(work_order_id)
        component = self._component(component_id)
        request = {
            "work_order_id": work_order_id,
            "component_id": component_id,
            "position": position,
            "inspection_id": inspection_id,
            "repair_id": repair_id,
        }
        request_digest = content_digest([request])
        scope = f"install:{work_order_id}"
        existing = self._idempotent_response(scope, key, request_digest)
        if existing is not None:
            return existing
        if component["state"] != "reuse":
            raise InvalidState(f"组件当前状态 {component['state']} 不能装机")
        pinned_inspection, pinned_repair = self._install_evidence(component, inspection_id, repair_id)
        response = {
            "assembly_id": assembly["assembly_id"],
            "component_id": component_id,
            "position": position,
            "inspection_id": pinned_inspection,
            "repair_id": pinned_repair,
        }
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "UPDATE components SET state='installed',state_reason=?,revision=revision+1,updated_at=? "
                    "WHERE component_id=? AND state='reuse'",
                    (f"装入 {assembly['assembly_id']}", self._now(), component_id),
                )
                if cursor.rowcount != 1:
                    raise InvalidState("组件状态已变化，不能装机")
                self.connection.execute(
                    "INSERT INTO assembly_members(assembly_id,component_id,position,inspection_id,repair_id,"
                    "installed_by,installed_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        assembly["assembly_id"], component_id, position, pinned_inspection, pinned_repair,
                        actor_id, self._now(),
                    ),
                )
                self._store_idempotent(scope, key, request_digest, response)
                self._audit(
                    "assembly", assembly["assembly_id"], "assembly.member_installed", actor_id,
                    {"component_id": component_id, "position": position,
                     "inspection_id": pinned_inspection, "repair_id": pinned_repair},
                )
                self._audit(
                    "component", component_id, "component.installed", actor_id,
                    {"assembly_id": assembly["assembly_id"], "position": position},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("槽位已被占用或组件已在其他有效装配中") from exc
        return response

    def swap_component(
        self,
        actor_id: str,
        work_order_id: str,
        position: str,
        new_component_id: str,
        inspection_id: int,
        repair_id: int | None,
        reason: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        """换件：旧件回库与新件装机在同一事务内完成。"""

        self._require(actor_id, "assembly.swap")
        position = required_text(position, "position", 64)
        reason = required_text(reason, "reason", 512)
        key = self._idempotency_key(idempotency_key)
        _, assembly = self._rebuild_target(work_order_id)
        old_member = self.connection.execute(
            "SELECT * FROM assembly_members WHERE assembly_id=? AND position=? AND removed_at IS NULL",
            (assembly["assembly_id"], position),
        ).fetchone()
        if old_member is None:
            raise NotFound("槽位上没有在装组件")
        if old_member["component_id"] == new_component_id:
            raise ValidationFailed("换件前后组件不能相同")
        component = self._component(new_component_id)
        request = {
            "work_order_id": work_order_id,
            "position": position,
            "component_id": new_component_id,
            "inspection_id": inspection_id,
            "repair_id": repair_id,
            "reason": reason,
        }
        request_digest = content_digest([request])
        scope = f"swap:{work_order_id}"
        existing = self._idempotent_response(scope, key, request_digest)
        if existing is not None:
            return existing
        if component["state"] != "reuse":
            raise InvalidState(f"组件当前状态 {component['state']} 不能装机")
        pinned_inspection, pinned_repair = self._install_evidence(component, inspection_id, repair_id)
        response = {
            "assembly_id": assembly["assembly_id"],
            "position": position,
            "removed_component_id": old_member["component_id"],
            "installed_component_id": new_component_id,
            "inspection_id": pinned_inspection,
            "repair_id": pinned_repair,
        }
        try:
            with transaction(self.connection, immediate=True):
                now = self._now()
                cursor = self.connection.execute(
                    "UPDATE assembly_members SET removed_at=?,removed_by=?,removal_reason='swapped' "
                    "WHERE member_id=? AND removed_at IS NULL",
                    (now, actor_id, old_member["member_id"]),
                )
                if cursor.rowcount != 1:
                    raise Conflict("槽位组件状态已变化")
                self._set_component_state(old_member["component_id"], "reuse", "换件回库")
                cursor = self.connection.execute(
                    "UPDATE components SET state='installed',state_reason=?,revision=revision+1,updated_at=? "
                    "WHERE component_id=? AND state='reuse'",
                    (f"装入 {assembly['assembly_id']}", now, new_component_id),
                )
                if cursor.rowcount != 1:
                    raise InvalidState("组件状态已变化，不能装机")
                self.connection.execute(
                    "INSERT INTO assembly_members(assembly_id,component_id,position,inspection_id,repair_id,"
                    "installed_by,installed_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        assembly["assembly_id"], new_component_id, position, pinned_inspection,
                        pinned_repair, actor_id, now,
                    ),
                )
                self._store_idempotent(scope, key, request_digest, response)
                self._audit(
                    "assembly", assembly["assembly_id"], "assembly.member_swapped", actor_id,
                    {"position": position, "removed": old_member["component_id"],
                     "installed": new_component_id, "reason": reason},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("槽位已被占用或组件已在其他有效装配中") from exc
        return response

    def confirm_assembly(
        self, actor_id: str, assembly_id: str, expected_revision: int, note: str = ""
    ) -> dict[str, Any]:
        """技术确认：组包工单完工后由维修技师确认。"""

        self._require(actor_id, "assembly.confirm")
        note = required_text(note, "note", 512) if note else ""
        assembly = self._assembly(assembly_id)
        if assembly["origin"] != "refurb":
            raise ValidationFailed("外部进场装配不需要技术确认")
        open_wo = self.connection.execute(
            "SELECT work_order_id FROM work_orders WHERE target_assembly_id=? AND kind='rebuild' "
            "AND state IN ('draft','in_progress')",
            (assembly_id,),
        ).fetchone()
        if open_wo is not None:
            raise InvalidState("组包工单未完工，不能技术确认")
        members = self.connection.execute(
            "SELECT component_id,inspection_id FROM assembly_members WHERE assembly_id=? AND removed_at IS NULL",
            (assembly_id,),
        ).fetchall()
        if not members:
            raise InvalidState("装配内没有组件，不能技术确认")
        missing = [m["component_id"] for m in members if m["inspection_id"] is None]
        if missing:
            raise InvalidState(f"组件缺少装机检测证据: {','.join(sorted(missing))}")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE assemblies SET state='confirmed',confirmed_by=?,confirmed_at=?,revision=revision+1 "
                "WHERE assembly_id=? AND state='open' AND revision=?",
                (actor_id, self._now(), assembly_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("装配状态或版本已变化")
            self._audit(
                "assembly", assembly_id, "assembly.confirmed", actor_id,
                {"members": [m["component_id"] for m in members], "note": note},
            )
        return self.get_assembly(assembly_id)

    def rework_assembly(self, actor_id: str, assembly_id: str, reason: str) -> dict[str, Any]:
        """返工：已确认装配退回可装机状态，组件保持在装，历史不丢。"""

        self._require(actor_id, "assembly.rework")
        reason = required_text(reason, "reason", 512)
        self._assembly(assembly_id)
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE assemblies SET state='open',revision=revision+1 "
                "WHERE assembly_id=? AND state='confirmed'",
                (assembly_id,),
            )
            if cursor.rowcount != 1:
                raise InvalidState("只有已确认装配可以返工")
            self._audit("assembly", assembly_id, "assembly.reworked", actor_id, {"reason": reason})
        return self.get_assembly(assembly_id)

    def release_assembly(
        self, actor_id: str, assembly_id: str, expected_revision: int, note: str = ""
    ) -> dict[str, Any]:
        """质量放行：必须由质量角色完成，且不能是技术确认人本人。"""

        self._require(actor_id, "assembly.release")
        note = required_text(note, "note", 512) if note else ""
        assembly = self._assembly(assembly_id)
        if assembly["state"] != "confirmed":
            raise InvalidState("只有已技术确认的装配可以质量放行")
        if assembly["confirmed_by"] == actor_id:
            raise Forbidden("技术确认与质量放行必须由不同人员完成")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE assemblies SET state='released',released_by=?,released_at=?,revision=revision+1 "
                "WHERE assembly_id=? AND state='confirmed' AND revision=?",
                (actor_id, self._now(), assembly_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("装配状态或版本已变化")
            self._audit("assembly", assembly_id, "assembly.released", actor_id, {"note": note})
        return self.get_assembly(assembly_id)

    def ship_assembly(
        self, actor_id: str, assembly_id: str, expected_revision: int, destination: str
    ) -> dict[str, Any]:
        """出场：此后装配配置只读，历史装机事实不再改变。"""

        self._require(actor_id, "assembly.ship")
        destination = required_text(destination, "destination")
        self._assembly(assembly_id)
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE assemblies SET state='shipped',shipped_by=?,shipped_at=?,destination=?,"
                "revision=revision+1 WHERE assembly_id=? AND state='released' AND revision=?",
                (actor_id, self._now(), destination, assembly_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("只有已质量放行的装配可以出场")
            self._audit(
                "assembly", assembly_id, "assembly.shipped", actor_id, {"destination": destination}
            )
        return self.get_assembly(assembly_id)

    def void_assembly(self, actor_id: str, assembly_id: str, reason: str) -> dict[str, Any]:
        """撤销未确认的翻新装配：全部组件退回复用库存。"""

        self._require(actor_id, "assembly.void")
        reason = required_text(reason, "reason", 512)
        assembly = self._assembly(assembly_id)
        if assembly["origin"] != "refurb":
            raise ValidationFailed("外部进场装配不能撤销，请通过拆解工单处理")
        open_wo = self.connection.execute(
            "SELECT work_order_id FROM work_orders WHERE target_assembly_id=? AND kind='rebuild' "
            "AND state IN ('draft','in_progress')",
            (assembly_id,),
        ).fetchone()
        if open_wo is not None:
            raise InvalidState("组包工单进行中，请先取消工单")
        with transaction(self.connection, immediate=True):
            members = self.connection.execute(
                "SELECT member_id,component_id FROM assembly_members WHERE assembly_id=? AND removed_at IS NULL",
                (assembly_id,),
            ).fetchall()
            now = self._now()
            for member in members:
                self.connection.execute(
                    "UPDATE assembly_members SET removed_at=?,removed_by=?,removal_reason='void' "
                    "WHERE member_id=? AND removed_at IS NULL",
                    (now, actor_id, member["member_id"]),
                )
                self._set_component_state(member["component_id"], "reuse", "装配撤销回库")
            cursor = self.connection.execute(
                "UPDATE assemblies SET state='void',closed_at=?,revision=revision+1 "
                "WHERE assembly_id=? AND state='open'",
                (now, assembly_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("只有未确认的装配可以撤销")
            self._audit(
                "assembly", assembly_id, "assembly.voided", actor_id,
                {"reason": reason, "returned": [m["component_id"] for m in members]},
            )
        return self.get_assembly(assembly_id)

    # ------------------------------------------------------------------
    # 谱系追溯
    # ------------------------------------------------------------------

    def _origin_chain(self, member: sqlite3.Row, depth: int) -> list[dict[str, Any]]:
        """沿组件的上一次在装记录向上还原来源，最新一跳在前。"""

        if depth >= MAX_TRACE_DEPTH:
            return [{"via": "truncated", "reason": "谱系深度超限"}]
        prior = self.connection.execute(
            "SELECT * FROM assembly_members WHERE component_id=? AND removed_at IS NOT NULL "
            "AND removed_at<=? AND member_id<>? ORDER BY removed_at DESC, member_id DESC LIMIT 1",
            (member["component_id"], member["installed_at"], member["member_id"]),
        ).fetchone()
        if prior is None:
            return [{"via": "registered", "note": "系统内无更早来源（外部进场或散装登记）"}]
        if prior["removal_reason"] == "disassembled":
            link = self.connection.execute(
                "SELECT i.work_order_id,i.disposition,i.dispositioned_at,w.source_assembly_id,a.label "
                "FROM work_order_intake_items i "
                "JOIN work_orders w ON w.work_order_id=i.work_order_id "
                "JOIN assemblies a ON a.assembly_id=w.source_assembly_id "
                "WHERE i.component_id=? AND i.dispositioned_at=? AND w.source_assembly_id=?",
                (member["component_id"], prior["removed_at"], prior["assembly_id"]),
            ).fetchone()
            if link is not None:
                hop = {
                    "via": "teardown",
                    "work_order_id": link["work_order_id"],
                    "source_assembly_id": link["source_assembly_id"],
                    "source_label": link["label"],
                    "disposition": link["disposition"],
                    "dispositioned_at": link["dispositioned_at"],
                }
                return [hop] + self._origin_chain(prior, depth + 1)
        hop = {
            "via": "returned",
            "reason": prior["removal_reason"],
            "assembly_id": prior["assembly_id"],
            "removed_at": prior["removed_at"],
        }
        return [hop] + self._origin_chain(prior, depth + 1)

    def assembly_origins(self, actor_id: str, assembly_id: str) -> dict[str, Any]:
        """从装配向上还原全部当前组件的来源链。"""

        self._require(actor_id, "lineage.read")
        assembly = self._assembly(assembly_id)
        members = self.connection.execute(
            "SELECT m.*,i.version AS inspection_version,i.result AS inspection_result,"
            "r.version AS repair_version,r.state AS repair_state "
            "FROM assembly_members m "
            "LEFT JOIN inspections i ON i.inspection_id=m.inspection_id "
            "LEFT JOIN repair_actions r ON r.repair_id=m.repair_id "
            "WHERE m.assembly_id=? AND m.removed_at IS NULL ORDER BY m.position",
            (assembly_id,),
        ).fetchall()
        entries: list[dict[str, Any]] = []
        sources: set[str] = set()
        for member in members:
            chain = self._origin_chain(member, 0)
            for hop in chain:
                if hop.get("via") == "teardown":
                    sources.add(hop["source_assembly_id"])
            entries.append({
                "component_id": member["component_id"],
                "position": member["position"],
                "installed_at": member["installed_at"],
                "evidence": {
                    "inspection_id": member["inspection_id"],
                    "inspection_version": member["inspection_version"],
                    "inspection_result": member["inspection_result"],
                    "repair_id": member["repair_id"],
                    "repair_version": member["repair_version"],
                    "repair_state": member["repair_state"],
                },
                "chain": chain,
            })
        return {
            "assembly_id": assembly["assembly_id"],
            "label": assembly["label"],
            "state": assembly["state"],
            "origin": assembly["origin"],
            "members": entries,
            "source_assemblies": sorted(sources),
        }

    def component_trace(self, actor_id: str, component_id: str) -> dict[str, Any]:
        """从组件向下给出当前位置、最终去向、未决动作与阻止交付的证据缺口。"""

        self._require(actor_id, "lineage.read")
        component = self._component(component_id)
        active = self.connection.execute(
            "SELECT m.*,a.label,a.state AS assembly_state,a.origin AS assembly_origin,a.destination,"
            "a.shipped_at FROM assembly_members m JOIN assemblies a ON a.assembly_id=m.assembly_id "
            "WHERE m.component_id=? AND m.removed_at IS NULL",
            (component_id,),
        ).fetchone()
        memberships = self.connection.execute(
            "SELECT m.assembly_id,a.label,m.position,m.inspection_id,m.repair_id,m.installed_by,"
            "m.installed_at,m.removed_by,m.removed_at,m.removal_reason "
            "FROM assembly_members m JOIN assemblies a ON a.assembly_id=m.assembly_id "
            "WHERE m.component_id=? ORDER BY m.member_id",
            (component_id,),
        ).fetchall()
        inspections = self.connection.execute(
            "SELECT inspection_id,version,result,summary,recorded_by,recorded_at FROM inspections "
            "WHERE component_id=? ORDER BY version",
            (component_id,),
        ).fetchall()
        repairs = self.connection.execute(
            "SELECT repair_id,version,action,state,opened_by,opened_at,closed_by,closed_at,"
            "close_inspection_id FROM repair_actions WHERE component_id=? ORDER BY version",
            (component_id,),
        ).fetchall()
        dispositions = self.connection.execute(
            "SELECT i.work_order_id,i.disposition,i.disposition_reason,i.dispositioned_at,"
            "w.source_assembly_id,w.state AS work_order_state "
            "FROM work_order_intake_items i JOIN work_orders w ON w.work_order_id=i.work_order_id "
            "WHERE i.component_id=? ORDER BY i.dispositioned_at",
            (component_id,),
        ).fetchall()

        pending_actions: list[dict[str, Any]] = []
        evidence_gaps: list[dict[str, Any]] = []
        if active is not None:
            location: dict[str, Any] = {
                "type": "assembly",
                "assembly_id": active["assembly_id"],
                "label": active["label"],
                "position": active["position"],
                "assembly_state": active["assembly_state"],
            }
            if active["assembly_state"] == "shipped":
                final_destination: dict[str, Any] = {
                    "type": "shipped",
                    "assembly_id": active["assembly_id"],
                    "label": active["label"],
                    "shipped_at": active["shipped_at"],
                    "destination": active["destination"],
                }
            else:
                final_destination = {
                    "type": "in_progress",
                    "assembly_id": active["assembly_id"],
                    "label": active["label"],
                    "assembly_state": active["assembly_state"],
                }
                if active["assembly_origin"] == "refurb":
                    if active["assembly_state"] == "open":
                        evidence_gaps.append({
                            "code": "assembly_not_confirmed",
                            "detail": f"装配 {active['assembly_id']} 未完成技术确认",
                        })
                    if active["assembly_state"] in ("open", "confirmed"):
                        evidence_gaps.append({
                            "code": "assembly_not_released",
                            "detail": f"装配 {active['assembly_id']} 未完成质量放行",
                        })
                open_wo = self.connection.execute(
                    "SELECT work_order_id FROM work_orders WHERE target_assembly_id=? AND kind='rebuild' "
                    "AND state IN ('draft','in_progress')",
                    (active["assembly_id"],),
                ).fetchone()
                if open_wo is not None:
                    pending_actions.append({
                        "type": "rebuild_work_order_open",
                        "work_order_id": open_wo["work_order_id"],
                        "detail": "组包工单未完工",
                    })
            if active["inspection_id"] is None and active["assembly_origin"] == "refurb":
                evidence_gaps.append({
                    "code": "missing_installation_inspection",
                    "detail": "装机记录缺少检测证据版本",
                })
        else:
            location = {"type": "stock", "state": component["state"]}
            final_destination = {"type": "stock", "state": component["state"]}
            if component["state"] == "repair":
                open_repairs = [r for r in repairs if r["state"] == "open"]
                for repair in open_repairs:
                    pending_actions.append({
                        "type": "repair_open",
                        "repair_id": repair["repair_id"],
                        "detail": f"维修动作未完工: {repair['action']}",
                    })
                evidence_gaps.append({
                    "code": "repair_not_completed",
                    "detail": "组件处于维修状态，未完工复检前不能交付",
                })
            elif component["state"] == "quarantine":
                evidence_gaps.append({
                    "code": "quarantine_unresolved",
                    "detail": "组件处于隔离状态，等待质量判定",
                })
            elif component["state"] == "scrap":
                final_destination = {"type": "scrapped", "state_reason": component["state_reason"]}
            elif component["state"] == "reuse":
                latest = self._latest_inspection(component_id)
                if latest is None or latest["result"] != "pass":
                    evidence_gaps.append({
                        "code": "missing_passing_inspection",
                        "detail": "复用库存缺少最新合格检测版本",
                    })
        for item in dispositions:
            if item["disposition"] is None and item["work_order_state"] in ("frozen", "in_progress"):
                pending_actions.append({
                    "type": "pending_disposition",
                    "work_order_id": item["work_order_id"],
                    "detail": "组件在冻结的进场配置中等待拆解处置",
                })
        return {
            "component": dict(component),
            "current_location": location,
            "final_destination": final_destination,
            "pending_actions": pending_actions,
            "evidence_gaps": evidence_gaps,
            "memberships": [dict(row) for row in memberships],
            "inspections": [dict(row) for row in inspections],
            "repairs": [dict(row) for row in repairs],
            "dispositions": [dict(row) for row in dispositions],
        }

    # ------------------------------------------------------------------
    # 审计链
    # ------------------------------------------------------------------

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM audit_events ORDER BY event_id").fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
