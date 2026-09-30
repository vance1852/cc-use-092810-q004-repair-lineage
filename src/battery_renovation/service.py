"""维修与翻新谱系领域用例。

事务边界与状态迁移规则：

* 拆解工单必须先冻结进场配置（装配快照 + 摘要）并挂接故障证据，才允许开工；
* 拆出的每个组件单独落子：复用 / 维修 / 报废 / 隔离，状态迁移与装配关系拆除
  在同一个立即事务内完成，部分失败也不会让组件失去明确状态；
* 组包工单逐行引用确定版本的检测和维修动作，冻结后按快照装配；
* 技术确认（engineer）与质量放行（quality）必须由不同人员完成，出场后整包
  与全部组件配置不可变；
* 撤销、返工、换件全部以「旧关系 removed + 新关系 installed」的追加方式记录。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Mapping

from .clock import SystemClock, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .jsonio import canonical_json, content_digest
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "planner": {"catalog.write", "workorder.create", "workorder.cancel", "lineage.read"},
    "technician": {
        "fault.record", "inspection.write", "repair.write",
        "disassembly.execute", "reassembly.execute", "rework.write", "lineage.read",
    },
    "engineer": {"inspection.write", "reassembly.confirm", "lineage.read"},
    "quality": {"release.write", "lineage.read"},
    "auditor": {"lineage.read", "audit.read"},
}

ITEM_KINDS = {"pack", "module", "cell", "bms", "component"}
DISPOSITIONS = {"reuse", "repair", "scrap", "quarantine"}
INSPECTION_VERDICTS = DISPOSITIONS
TERMINAL_ITEM_STATES = {"scrapped", "retired", "delivered", "cancelled"}
OPEN_ORDER_STATES = ("draft", "frozen", "in_progress", "released")


class RefurbService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    # ----- 基础 -----------------------------------------------------------

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM refurb_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM refurb_audit_events ORDER BY event_id DESC LIMIT 1"
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
            "INSERT INTO refurb_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type, entity_id, event_type, actor_id,
                canonical_json(payload), previous_hash, event_hash, body["created_at"],
            ),
        )

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO refurb_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    # ----- 序列件与进场既有配置 -------------------------------------------

    def register_item(
        self,
        actor_id: str,
        item_id: str,
        item_kind: str,
        model_name: str,
        vendor: str,
        state: str = "in_service",
    ) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        if item_kind not in ITEM_KINDS:
            raise ValidationFailed("item_kind 不受支持")
        if state not in {"in_service", "available"}:
            raise ValidationFailed("登记序列件只能以 in_service 或 available 状态入场")
        if not item_id.strip() or not model_name.strip() or not vendor.strip():
            raise ValidationFailed("序列件编号、型号和厂商不能为空")
        now = self._now()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO items(item_id,item_kind,model_name,vendor,state,created_by,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (item_id.strip(), item_kind, model_name.strip(), vendor.strip(), state, actor_id, now, now),
                )
                self._audit("item", item_id.strip(), "item.registered", actor_id,
                            {"item_kind": item_kind, "state": state})
        except sqlite3.IntegrityError as exc:
            raise Conflict("序列件编号已经存在") from exc
        return self.get_item(item_id.strip())

    def record_as_found_membership(
        self, actor_id: str, parent_item_id: str, child_item_id: str, position: str
    ) -> dict[str, Any]:
        """登记平台接管前已存在的装配关系（无工单，进场配置的一部分）。"""
        self._require(actor_id, "catalog.write")
        if not position.strip():
            raise ValidationFailed("仓位不能为空")
        with transaction(self.connection, immediate=True):
            parent = self._item_row(parent_item_id)
            child = self._item_row(child_item_id)
            if parent["state"] != "in_service" or child["state"] != "in_service":
                raise InvalidState("只有在场服役的序列件可以登记既有装配关系")
            try:
                cursor = self.connection.execute(
                    "INSERT INTO assembly_memberships(parent_item_id,child_item_id,position,work_order_id,"
                    "state,installed_at) VALUES(?,?,?,?, 'installed',?)",
                    (parent_item_id, child_item_id, position.strip(), None, self._now()),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("仓位已被占用或子件已在其它生效装配中") from exc
            self._audit("item", parent_item_id, "membership.as_found", actor_id,
                        {"child_item_id": child_item_id, "position": position.strip()})
            return self._membership(cursor.lastrowid)

    def get_item(self, item_id: str) -> dict[str, Any]:
        return dict(self._item_row(item_id))

    def authorize_read(self, user_id: str) -> None:
        self._require(user_id, "lineage.read")

    def _item_row(self, item_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM items WHERE item_id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFound("序列件不存在")
        return row

    def _membership(self, membership_id: int) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM assembly_memberships WHERE membership_id=?", (membership_id,)
        ).fetchone()
        return dict(row)

    def _set_item_state(self, item_id: str, new_state: str, reason: str | None = None) -> None:
        self.connection.execute(
            "UPDATE items SET state=?,state_reason=?,revision=revision+1,updated_at=? WHERE item_id=?",
            (new_state, reason, self._now(), item_id),
        )

    def _guard_no_open_order(self, item_id: str) -> None:
        row = self.connection.execute(
            "SELECT work_order_id FROM work_orders WHERE target_item_id=? AND state IN (?,?,?,?)",
            (item_id, *OPEN_ORDER_STATES),
        ).fetchone()
        if row is not None:
            raise Conflict(f"序列件存在未终结工单 {row['work_order_id']}")

    # ----- 故障证据 -------------------------------------------------------

    def record_fault_evidence(
        self,
        actor_id: str,
        evidence_id: str,
        item_id: str,
        evidence_kind: str,
        summary: str,
        content_sha256: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "fault.record")
        if not evidence_id.strip() or not evidence_kind.strip() or not summary.strip():
            raise ValidationFailed("证据编号、类型和摘要不能为空")
        if len(content_sha256) != 64:
            raise ValidationFailed("证据摘要必须是 64 位 SHA-256")
        self._item_row(item_id)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO fault_evidences(evidence_id,item_id,evidence_kind,summary,content_sha256,"
                    "recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?)",
                    (evidence_id.strip(), item_id, evidence_kind.strip(), summary.strip(),
                     content_sha256.lower(), actor_id, self._now()),
                )
                self._audit("fault_evidence", evidence_id.strip(), "fault_evidence.recorded", actor_id,
                            {"item_id": item_id, "evidence_kind": evidence_kind.strip()})
        except sqlite3.IntegrityError as exc:
            raise Conflict("证据编号或内容摘要冲突") from exc
        return {"evidence_id": evidence_id.strip(), "item_id": item_id}

    # ----- 组件检测（确定版本） -------------------------------------------

    def record_inspection(
        self,
        actor_id: str,
        component_id: str,
        protocol_id: str,
        verdict: str,
        metrics: Mapping[str, Any],
    ) -> dict[str, Any]:
        self._require(actor_id, "inspection.write")
        if verdict not in INSPECTION_VERDICTS:
            raise ValidationFailed("检测结论必须是 reuse、repair、scrap 或 quarantine")
        if not protocol_id.strip() or not isinstance(metrics, Mapping) or not metrics:
            raise ValidationFailed("协议编号和检测指标不能为空")
        item = self._item_row(component_id)
        if item["state"] in TERMINAL_ITEM_STATES:
            raise InvalidState("终态序列件不能再登记检测")
        body = {"component_id": component_id, "protocol_id": protocol_id.strip(),
                "verdict": verdict, "metrics": dict(metrics)}
        sha = content_digest(body)
        with transaction(self.connection, immediate=True):
            latest = self.connection.execute(
                "SELECT max(version) AS version FROM inspections WHERE component_id=?", (component_id,)
            ).fetchone()
            version = (latest["version"] or 0) + 1
            cursor = self.connection.execute(
                "INSERT INTO inspections(component_id,version,protocol_id,verdict,metrics_json,content_sha256,"
                "inspected_by,inspected_at) VALUES(?,?,?,?,?,?,?,?)",
                (component_id, version, protocol_id.strip(), verdict, canonical_json(dict(metrics)),
                 sha, actor_id, self._now()),
            )
            inspection_id = cursor.lastrowid
            self._audit("inspection", str(inspection_id), "inspection.recorded", actor_id,
                        {"component_id": component_id, "version": version, "verdict": verdict})
        return {"inspection_id": inspection_id, "component_id": component_id,
                "version": version, "verdict": verdict, "content_sha256": sha}

    def _inspection(self, inspection_id: int) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM inspections WHERE inspection_id=?", (inspection_id,)
        ).fetchone()
        if row is None:
            raise NotFound("检测版本不存在")
        return row

    # ----- 维修工单与维修动作 ---------------------------------------------

    def open_repair(self, actor_id: str, repair_order_id: str, component_id: str) -> dict[str, Any]:
        self._require(actor_id, "repair.write")
        item = self._item_row(component_id)
        if item["state"] != "repair":
            raise InvalidState("只有处置为维修的组件可以开维修工单")
        source = self.connection.execute(
            "SELECT work_order_id FROM disassembly_dispositions WHERE component_id=? AND disposition='repair' "
            "ORDER BY decided_at DESC, disposition_id DESC LIMIT 1",
            (component_id,),
        ).fetchone()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO repair_orders(repair_order_id,component_id,source_work_order_id,state,"
                    "opened_by,opened_at) VALUES(?,?,?, 'open',?,?)",
                    (repair_order_id.strip(), component_id,
                     None if source is None else source["work_order_id"], actor_id, self._now()),
                )
                self._audit("repair_order", repair_order_id.strip(), "repair.opened", actor_id,
                            {"component_id": component_id})
        except sqlite3.IntegrityError as exc:
            raise Conflict("维修工单编号冲突或组件已有未结维修工单") from exc
        return {"repair_order_id": repair_order_id.strip(), "component_id": component_id, "state": "open"}

    def add_repair_action(
        self, actor_id: str, repair_order_id: str, action_code: str, detail: Mapping[str, Any]
    ) -> dict[str, Any]:
        self._require(actor_id, "repair.write")
        if not action_code.strip() or not isinstance(detail, Mapping) or not detail:
            raise ValidationFailed("维修动作代码和明细不能为空")
        order = self._repair_order(repair_order_id)
        if order["state"] != "open":
            raise InvalidState("维修工单不是打开状态")
        body = {"repair_order_id": repair_order_id, "action_code": action_code.strip(),
                "detail": dict(detail)}
        sha = content_digest(body)
        with transaction(self.connection, immediate=True):
            latest = self.connection.execute(
                "SELECT max(sequence) AS sequence FROM repair_actions WHERE repair_order_id=?",
                (repair_order_id,),
            ).fetchone()
            sequence = (latest["sequence"] or 0) + 1
            cursor = self.connection.execute(
                "INSERT INTO repair_actions(repair_order_id,sequence,action_code,detail,content_sha256,"
                "performed_by,performed_at) VALUES(?,?,?,?,?,?,?)",
                (repair_order_id, sequence, action_code.strip(), canonical_json(dict(detail)),
                 sha, actor_id, self._now()),
            )
            action_id = cursor.lastrowid
            self._audit("repair_action", str(action_id), "repair_action.recorded", actor_id,
                        {"repair_order_id": repair_order_id, "sequence": sequence})
        return {"action_id": action_id, "repair_order_id": repair_order_id,
                "sequence": sequence, "content_sha256": sha}

    def complete_repair(
        self, actor_id: str, repair_order_id: str, recheck_inspection_id: int
    ) -> dict[str, Any]:
        self._require(actor_id, "repair.write")
        order = self._repair_order(repair_order_id)
        if order["state"] != "open":
            raise InvalidState("维修工单不是打开状态")
        actions = self.connection.execute(
            "SELECT count(*) AS c FROM repair_actions WHERE repair_order_id=?", (repair_order_id,)
        ).fetchone()["c"]
        if not actions:
            raise InvalidState("维修工单至少需要一个确定版本的维修动作")
        recheck = self._inspection(recheck_inspection_id)
        if recheck["component_id"] != order["component_id"]:
            raise ValidationFailed("复检检测不属于该组件")
        if recheck["verdict"] != "reuse":
            raise InvalidState("复检结论不是 reuse，组件不能回到复用库")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE repair_orders SET state='completed',closed_at=? WHERE repair_order_id=? AND state='open'",
                (self._now(), repair_order_id),
            )
            self._set_item_state(order["component_id"], "available",
                                 f"repair {repair_order_id} completed")
            self._audit("repair_order", repair_order_id, "repair.completed", actor_id,
                        {"recheck_inspection_id": recheck_inspection_id})
        return {"repair_order_id": repair_order_id, "component_id": order["component_id"],
                "state": "completed", "item_state": "available"}

    def cancel_repair(self, actor_id: str, repair_order_id: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "repair.write")
        order = self._repair_order(repair_order_id)
        if order["state"] != "open":
            raise InvalidState("维修工单不是打开状态")
        if not reason.strip():
            raise ValidationFailed("撤销原因不能为空")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE repair_orders SET state='cancelled',closed_at=? WHERE repair_order_id=? AND state='open'",
                (self._now(), repair_order_id),
            )
            # 维修撤销不等于组件消失：转入隔离等待新决定
            self._set_item_state(order["component_id"], "quarantined", reason.strip())
            self._audit("repair_order", repair_order_id, "repair.cancelled", actor_id, {"reason": reason.strip()})
        return {"repair_order_id": repair_order_id, "state": "cancelled", "item_state": "quarantined"}

    def _repair_order(self, repair_order_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM repair_orders WHERE repair_order_id=?", (repair_order_id,)
        ).fetchone()
        if row is None:
            raise NotFound("维修工单不存在")
        return row

    # ----- 拆解工单 -------------------------------------------------------

    def create_disassembly(self, actor_id: str, work_order_id: str, pack_id: str) -> dict[str, Any]:
        self._require(actor_id, "workorder.create")
        pack = self._item_row(pack_id)
        if pack["item_kind"] != "pack":
            raise ValidationFailed("拆解目标必须是整包")
        if pack["state"] != "in_service":
            raise InvalidState("只有在场服役的整包可以发起拆解")
        self._guard_no_open_order(pack_id)
        now = self._now()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO work_orders(work_order_id,order_type,target_item_id,state,"
                    "created_by,created_at,updated_at) VALUES(?, 'disassembly',?, 'draft',?,?,?)",
                    (work_order_id.strip(), pack_id, actor_id, now, now),
                )
                self._audit("work_order", work_order_id.strip(), "disassembly.created", actor_id,
                            {"target_item_id": pack_id})
        except sqlite3.IntegrityError as exc:
            raise Conflict("工单编号冲突") from exc
        return self.get_work_order(work_order_id.strip())

    def attach_evidence(self, actor_id: str, work_order_id: str, evidence_id: str) -> dict[str, Any]:
        self._require(actor_id, "fault.record")
        order = self._work_order(work_order_id)
        if order["order_type"] != "disassembly" or order["state"] != "draft":
            raise InvalidState("只有草稿拆解工单可以挂接故障证据")
        evidence = self.connection.execute(
            "SELECT * FROM fault_evidences WHERE evidence_id=?", (evidence_id,)
        ).fetchone()
        if evidence is None:
            raise NotFound("故障证据不存在")
        target = order["target_item_id"]
        if evidence["item_id"] != target:
            mounted = self.connection.execute(
                "SELECT 1 FROM assembly_memberships WHERE parent_item_id=? AND child_item_id=? AND state='installed'",
                (target, evidence["item_id"]),
            ).fetchone()
            if mounted is None:
                raise ValidationFailed("证据不属于整包或其当前装配件")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO work_order_evidences(work_order_id,evidence_id) VALUES(?,?)",
                    (work_order_id, evidence_id),
                )
                self.connection.execute(
                    "UPDATE work_orders SET revision=revision+1,updated_at=? WHERE work_order_id=?",
                    (self._now(), work_order_id),
                )
                self._audit("work_order", work_order_id, "evidence.attached", actor_id,
                            {"evidence_id": evidence_id})
        except sqlite3.IntegrityError as exc:
            raise Conflict("证据已经挂接") from exc
        return {"work_order_id": work_order_id, "evidence_id": evidence_id}

    def freeze_disassembly(self, actor_id: str, work_order_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "workorder.create")
        order = self._work_order(work_order_id)
        if order["order_type"] != "disassembly":
            raise ValidationFailed("该接口只适用于拆解工单")
        if order["state"] != "draft" or order["revision"] != expected_revision:
            raise InvalidState("工单不是当前草稿版本")
        children = self.connection.execute(
            "SELECT child_item_id,position FROM assembly_memberships "
            "WHERE parent_item_id=? AND state='installed' ORDER BY position,membership_id",
            (order["target_item_id"],),
        ).fetchall()
        if not children:
            raise InvalidState("进场配置为空，无可冻结装配")
        evidence_ids = [row[0] for row in self.connection.execute(
            "SELECT evidence_id FROM work_order_evidences WHERE work_order_id=? ORDER BY evidence_id",
            (work_order_id,),
        ).fetchall()]
        if not evidence_ids:
            raise InvalidState("故障证据未冻结，不允许拆解")
        snapshot = {
            "work_order_id": work_order_id,
            "pack_id": order["target_item_id"],
            "children": [{"component_id": row["child_item_id"], "position": row["position"]} for row in children],
            "evidence_ids": evidence_ids,
        }
        sha = content_digest(snapshot)
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE work_orders SET state='frozen',intake_snapshot_json=?,intake_sha256=?,frozen_at=?,"
                "revision=revision+1,updated_at=? WHERE work_order_id=? AND state='draft' AND revision=?",
                (canonical_json(snapshot), sha, self._now(), self._now(), work_order_id, expected_revision),
            )
            self._audit("work_order", work_order_id, "disassembly.frozen", actor_id,
                        {"intake_sha256": sha, "components": len(children), "evidences": len(evidence_ids)})
        return self.get_work_order(work_order_id)

    def start_disassembly(self, actor_id: str, work_order_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "disassembly.execute")
        order = self._work_order(work_order_id)
        if order["order_type"] != "disassembly":
            raise ValidationFailed("该接口只适用于拆解工单")
        return self._start_order(work_order_id, expected_revision, "disassembly.started", actor_id, "frozen")

    def _start_order(self, work_order_id: str, expected_revision: int, event: str,
                     actor_id: str, expected_state: str) -> dict[str, Any]:
        order = self._work_order(work_order_id)
        if order["state"] != expected_state or order["revision"] != expected_revision:
            raise InvalidState("工单不是可开工的当前版本")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE work_orders SET state='in_progress',revision=revision+1,updated_at=? "
                "WHERE work_order_id=? AND state=? AND revision=?",
                (self._now(), work_order_id, expected_state, expected_revision),
            )
            self._audit("work_order", work_order_id, event, actor_id, {})
        return self.get_work_order(work_order_id)

    def execute_disposition(
        self, actor_id: str, work_order_id: str, component_id: str, disposition: str, note: str = ""
    ) -> dict[str, Any]:
        self._require(actor_id, "disassembly.execute")
        if disposition not in DISPOSITIONS:
            raise ValidationFailed("处置必须是 reuse、repair、scrap 或 quarantine")
        order = self._work_order(work_order_id)
        if order["order_type"] != "disassembly" or order["state"] != "in_progress":
            raise InvalidState("拆解工单不在执行中")
        snapshot = json.loads(order["intake_snapshot_json"])
        if not any(line["component_id"] == component_id for line in snapshot["children"]):
            raise ValidationFailed("组件不在冻结进场配置中")
        latest_verdict = self.connection.execute(
            "SELECT verdict FROM inspections WHERE component_id=? ORDER BY version DESC LIMIT 1",
            (component_id,),
        ).fetchone()
        if disposition == "reuse" and (latest_verdict is None or latest_verdict["verdict"] != "reuse"):
            raise InvalidState("复用处置需要最新版本检测结论为 reuse")
        if latest_verdict is not None and latest_verdict["verdict"] != disposition:
            raise InvalidState(
                f"处置 {disposition} 与最新检测结论 {latest_verdict['verdict']} 不一致，"
                "请先补检或按最新结论处置")
        membership = self.connection.execute(
            "SELECT * FROM assembly_memberships WHERE parent_item_id=? AND child_item_id=? AND state='installed'",
            (order["target_item_id"], component_id),
        ).fetchone()
        if membership is None:
            raise InvalidState("组件已经拆出，不能重复处置")
        target_state = {
            "reuse": "available",
            "repair": "repair",
            "scrap": "scrapped",
            "quarantine": "quarantined",
        }[disposition]
        with transaction(self.connection, immediate=True):
            try:
                self.connection.execute(
                    "INSERT INTO disassembly_dispositions(work_order_id,component_id,disposition,note,"
                    "decided_by,decided_at) VALUES(?,?,?,?,?,?)",
                    (work_order_id, component_id, disposition, note.strip(), actor_id, self._now()),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("该组件在本工单已有处置") from exc
            self.connection.execute(
                "UPDATE assembly_memberships SET state='removed',removed_by_work_order_id=?,"
                "removed_reason=?,removed_at=? WHERE membership_id=? AND state='installed'",
                (work_order_id, f"disposition:{disposition}", self._now(), membership["membership_id"]),
            )
            self._set_item_state(component_id, target_state, f"disassembly {work_order_id}:{disposition}")
            self._audit("component", component_id, "component.dispositioned", actor_id,
                        {"work_order_id": work_order_id, "disposition": disposition})
        return {"work_order_id": work_order_id, "component_id": component_id,
                "disposition": disposition, "item_state": target_state}

    def complete_disassembly(self, actor_id: str, work_order_id: str) -> dict[str, Any]:
        self._require(actor_id, "disassembly.execute")
        order = self._work_order(work_order_id)
        if order["order_type"] != "disassembly":
            raise ValidationFailed("该接口只适用于拆解工单")
        if order["state"] != "in_progress":
            raise InvalidState("拆解工单不在执行中")
        snapshot = json.loads(order["intake_snapshot_json"])
        expected = {line["component_id"] for line in snapshot["children"]}
        with transaction(self.connection, immediate=True):
            decided = {row[0] for row in self.connection.execute(
                "SELECT component_id FROM disassembly_dispositions WHERE work_order_id=?", (work_order_id,)
            ).fetchall()}
            missing = sorted(expected - decided)
            if missing:
                raise InvalidState(f"仍有组件未分别处置: {', '.join(missing)}")
            remaining = self.connection.execute(
                "SELECT count(*) AS c FROM assembly_memberships WHERE parent_item_id=? AND state='installed'",
                (order["target_item_id"],),
            ).fetchone()["c"]
            if remaining:
                raise InvalidState("仍有装配关系未拆除")
            self.connection.execute(
                "UPDATE work_orders SET state='completed',closed_at=?,updated_at=? WHERE work_order_id=?",
                (self._now(), self._now(), work_order_id),
            )
            self._set_item_state(order["target_item_id"], "retired", f"disassembled by {work_order_id}")
            self._audit("work_order", work_order_id, "disassembly.completed", actor_id,
                        {"components": len(expected)})
        return self.get_work_order(work_order_id)

    def fail_disassembly(self, actor_id: str, work_order_id: str, reason: str) -> dict[str, Any]:
        """部分失败：已拆组件保留各自处置状态，未拆组件继续挂在原包上。"""
        self._require(actor_id, "disassembly.execute")
        order = self._work_order(work_order_id)
        if order["order_type"] != "disassembly":
            raise ValidationFailed("该接口只适用于拆解工单")
        if order["state"] != "in_progress":
            raise InvalidState("拆解工单不在执行中")
        if not reason.strip():
            raise ValidationFailed("失败原因不能为空")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE work_orders SET state='failed',fail_reason=?,closed_at=?,updated_at=? WHERE work_order_id=?",
                (reason.strip(), self._now(), self._now(), work_order_id),
            )
            self._audit("work_order", work_order_id, "disassembly.failed", actor_id, {"reason": reason.strip()})
        return self.get_work_order(work_order_id)

    def cancel_disassembly(self, actor_id: str, work_order_id: str, reason: str) -> dict[str, Any]:
        """开工前撤销：进场配置与证据保持原样，不触碰任何组件状态。"""
        self._require(actor_id, "workorder.cancel")
        order = self._work_order(work_order_id)
        if order["order_type"] != "disassembly" or order["state"] not in {"draft", "frozen"}:
            raise InvalidState("只有草稿或已冻结、未开工的拆解工单可以撤销")
        if not reason.strip():
            raise ValidationFailed("撤销原因不能为空")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE work_orders SET state='cancelled',closed_at=?,fail_reason=?,updated_at=? "
                "WHERE work_order_id=?",
                (self._now(), reason.strip(), self._now(), work_order_id),
            )
            self._audit("work_order", work_order_id, "disassembly.cancelled", actor_id,
                        {"reason": reason.strip()})
        return self.get_work_order(work_order_id)

    def resolve_component(
        self, actor_id: str, component_id: str, inspection_id: int
    ) -> dict[str, Any]:
        """依据新的确定版本检测重新决定游离组件去向（复用库或隔离件）。

        覆盖维修撤销后隔离、复用库复检出异常等场景，避免组件长期停留在无主状态。
        """
        self._require(actor_id, "rework.write")
        item = self._item_row(component_id)
        if item["state"] not in {"available", "quarantined"}:
            raise InvalidState("只有复用库或隔离中的组件可以依据新检测重新决定去向")
        inspection = self._inspection(inspection_id)
        if inspection["component_id"] != component_id:
            raise ValidationFailed("检测不属于该组件")
        mounted = self.connection.execute(
            "SELECT 1 FROM assembly_memberships WHERE child_item_id=? AND state='installed'",
            (component_id,),
        ).fetchone()
        if mounted is not None:
            raise InvalidState("组件仍挂在生效装配中，不能直接重新决定去向")
        target = {
            "reuse": "available",
            "repair": "repair",
            "scrap": "scrapped",
            "quarantine": "quarantined",
        }[inspection["verdict"]]
        with transaction(self.connection, immediate=True):
            if target != item["state"]:
                self._set_item_state(component_id, target,
                                     f"decision resolved by inspection {inspection_id}")
            self._audit("component", component_id, "component.resolved", actor_id,
                        {"inspection_id": inspection_id, "verdict": inspection["verdict"],
                         "from_state": item["state"], "to_state": target})
        return {"component_id": component_id, "state": self.get_item(component_id)["state"],
                "verdict": inspection["verdict"]}

    # ----- 重新组包工单 ---------------------------------------------------

    def create_reassembly(
        self, actor_id: str, work_order_id: str, new_pack_id: str, model_name: str, vendor: str
    ) -> dict[str, Any]:
        self._require(actor_id, "workorder.create")
        if not new_pack_id.strip() or not model_name.strip() or not vendor.strip():
            raise ValidationFailed("新整包编号、型号和厂商不能为空")
        now = self._now()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO items(item_id,item_kind,model_name,vendor,state,created_by,created_at,updated_at) "
                    "VALUES(?, 'pack',?,?, 'building',?,?,?)",
                    (new_pack_id.strip(), model_name.strip(), vendor.strip(), actor_id, now, now),
                )
                self.connection.execute(
                    "INSERT INTO work_orders(work_order_id,order_type,target_item_id,state,"
                    "created_by,created_at,updated_at) VALUES(?, 'reassembly',?, 'draft',?,?,?)",
                    (work_order_id.strip(), new_pack_id.strip(), actor_id, now, now),
                )
                self._audit("work_order", work_order_id.strip(), "reassembly.created", actor_id,
                            {"target_item_id": new_pack_id.strip()})
        except sqlite3.IntegrityError as exc:
            raise Conflict("工单或新整包编号冲突") from exc
        return self.get_work_order(work_order_id.strip())

    def add_reassembly_line(
        self,
        actor_id: str,
        work_order_id: str,
        component_id: str,
        position: str,
        inspection_id: int,
        repair_action_id: int | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "reassembly.execute")
        if not position.strip():
            raise ValidationFailed("仓位不能为空")
        order = self._work_order(work_order_id)
        if order["order_type"] != "reassembly" or order["state"] != "draft":
            raise InvalidState("只有草稿组包工单可以维护清单")
        component = self._item_row(component_id)
        if component["state"] != "available":
            raise InvalidState("只有复用库（available）组件可以进入组包清单")
        inspection = self._inspection(inspection_id)
        if inspection["component_id"] != component_id or inspection["verdict"] != "reuse":
            raise ValidationFailed("必须引用属于该组件、结论为 reuse 的确定版本检测")
        completed_repairs = self.connection.execute(
            "SELECT count(*) AS c FROM repair_orders WHERE component_id=? AND state='completed'",
            (component_id,),
        ).fetchone()["c"]
        if completed_repairs and repair_action_id is None:
            raise InvalidState("该组件有已完成维修，组包清单必须引用确定版本的维修动作")
        action_sha = None
        if repair_action_id is not None:
            action = self.connection.execute(
                "SELECT a.*,r.state AS repair_state,r.component_id AS action_component "
                "FROM repair_actions a JOIN repair_orders r ON r.repair_order_id=a.repair_order_id "
                "WHERE a.action_id=?",
                (repair_action_id,),
            ).fetchone()
            if action is None:
                raise NotFound("维修动作不存在")
            if action["action_component"] != component_id:
                raise ValidationFailed("维修动作不属于该组件")
            if action["repair_state"] != "completed":
                raise InvalidState("引用的维修动作所在维修工单尚未完成")
            action_sha = action["content_sha256"]
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO reassembly_lines(work_order_id,component_id,position,inspection_id,"
                    "repair_action_id,created_at) VALUES(?,?,?,?,?,?)",
                    (work_order_id, component_id, position.strip(), inspection_id,
                     repair_action_id, self._now()),
                )
                line_id = cursor.lastrowid
                self.connection.execute(
                    "UPDATE work_orders SET revision=revision+1,updated_at=? WHERE work_order_id=?",
                    (self._now(), work_order_id),
                )
                self._audit("work_order", work_order_id, "reassembly.line_added", actor_id,
                            {"line_id": line_id, "component_id": component_id, "position": position.strip(),
                             "inspection_id": inspection_id, "repair_action_id": repair_action_id,
                             "repair_action_sha256": action_sha})
        except sqlite3.IntegrityError as exc:
            raise Conflict("组件或仓位已在清单中") from exc
        return {"line_id": line_id, "work_order_id": work_order_id, "component_id": component_id,
                "position": position.strip(), "inspection_id": inspection_id,
                "repair_action_id": repair_action_id}

    def freeze_reassembly(self, actor_id: str, work_order_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "reassembly.execute")
        order = self._work_order(work_order_id)
        if order["order_type"] != "reassembly":
            raise ValidationFailed("该接口只适用于组包工单")
        if order["state"] != "draft" or order["revision"] != expected_revision:
            raise InvalidState("工单不是当前草稿版本")
        lines = self.connection.execute(
            "SELECT l.component_id,l.position,l.inspection_id,l.repair_action_id,i.version,i.content_sha256,"
            "a.content_sha256 AS action_sha256 "
            "FROM reassembly_lines l JOIN inspections i ON i.inspection_id=l.inspection_id "
            "LEFT JOIN repair_actions a ON a.action_id=l.repair_action_id "
            "WHERE l.work_order_id=? AND l.state='active' ORDER BY l.position,l.line_id",
            (work_order_id,),
        ).fetchall()
        if not lines:
            raise InvalidState("组包清单为空，不能冻结")
        snapshot = {
            "work_order_id": work_order_id,
            "pack_id": order["target_item_id"],
            "lines": [
                {"component_id": row["component_id"], "position": row["position"],
                 "inspection_id": row["inspection_id"], "inspection_version": row["version"],
                 "inspection_sha256": row["content_sha256"],
                 "repair_action_id": row["repair_action_id"], "repair_action_sha256": row["action_sha256"]}
                for row in lines
            ],
        }
        sha = content_digest(snapshot)
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE work_orders SET state='frozen',intake_snapshot_json=?,intake_sha256=?,frozen_at=?,"
                "revision=revision+1,updated_at=? WHERE work_order_id=? AND state='draft' AND revision=?",
                (canonical_json(snapshot), sha, self._now(), self._now(), work_order_id, expected_revision),
            )
            self._audit("work_order", work_order_id, "reassembly.frozen", actor_id,
                        {"intake_sha256": sha, "lines": len(lines)})
        return self.get_work_order(work_order_id)

    def start_reassembly(self, actor_id: str, work_order_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "reassembly.execute")
        order = self._work_order(work_order_id)
        if order["order_type"] != "reassembly":
            raise ValidationFailed("该接口只适用于组包工单")
        if order["state"] != "frozen" or order["revision"] != expected_revision:
            raise InvalidState("工单不是当前冻结版本")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE work_orders SET state='in_progress',revision=revision+1,updated_at=? "
                "WHERE work_order_id=? AND state='frozen' AND revision=?",
                (self._now(), work_order_id, expected_revision),
            )
            lines = self.connection.execute(
                "SELECT * FROM reassembly_lines WHERE work_order_id=? AND state='active'", (work_order_id,)
            ).fetchall()
            for line in lines:
                component = self._item_row(line["component_id"])
                if component["state"] != "available":
                    raise InvalidState(f"组件 {line['component_id']} 不在复用库，无法装配")
                try:
                    self.connection.execute(
                        "INSERT INTO assembly_memberships(parent_item_id,child_item_id,position,work_order_id,"
                        "state,installed_at) VALUES(?,?,?,?, 'installed',?)",
                        (order["target_item_id"], line["component_id"], line["position"],
                         work_order_id, self._now()),
                    )
                except sqlite3.IntegrityError as exc:
                    raise Conflict(f"组件 {line['component_id']} 已属于其它生效装配") from exc
                self._set_item_state(line["component_id"], "building", f"assembled into {order['target_item_id']}")
            self._audit("work_order", work_order_id, "reassembly.started", actor_id,
                        {"components": len(lines)})
        return self.get_work_order(work_order_id)

    def technical_confirm(self, actor_id: str, work_order_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "reassembly.confirm")
        order = self._work_order(work_order_id)
        if order["order_type"] != "reassembly":
            raise ValidationFailed("该接口只适用于组包工单")
        self._assert_mounting_consistent(order)
        if order["state"] != "in_progress" or order["revision"] != expected_revision:
            raise InvalidState("工单不是待技术确认的当前版本")
        approval = self.connection.execute(
            "SELECT * FROM release_approvals WHERE work_order_id=?", (work_order_id,)
        ).fetchone()
        if approval is not None and approval["quality_by"] == actor_id:
            raise Forbidden("技术确认与质量放行不能由同一人完成")
        with transaction(self.connection, immediate=True):
            if approval is None:
                self.connection.execute(
                    "INSERT INTO release_approvals(work_order_id,technical_by,technical_at) VALUES(?,?,?)",
                    (work_order_id, actor_id, self._now()),
                )
            else:
                self.connection.execute(
                    "UPDATE release_approvals SET technical_by=?,technical_at=? WHERE work_order_id=?",
                    (actor_id, self._now(), work_order_id),
                )
            self.connection.execute(
                "UPDATE work_orders SET revision=revision+1,updated_at=? WHERE work_order_id=?",
                (self._now(), work_order_id),
            )
            self._audit("work_order", work_order_id, "release.technical_confirmed", actor_id, {})
        return self.get_work_order(work_order_id)

    def quality_release(self, actor_id: str, work_order_id: str, expected_revision: int) -> dict[str, Any]:
        self._require(actor_id, "release.write")
        order = self._work_order(work_order_id)
        if order["order_type"] != "reassembly":
            raise ValidationFailed("该接口只适用于组包工单")
        if order["state"] != "in_progress" or order["revision"] != expected_revision:
            raise InvalidState("工单不是待质量放行的当前版本")
        approval = self.connection.execute(
            "SELECT * FROM release_approvals WHERE work_order_id=?", (work_order_id,)
        ).fetchone()
        if approval is None or approval["technical_by"] is None:
            raise InvalidState("缺少技术确认")
        if approval["technical_by"] == actor_id:
            raise Forbidden("技术确认与质量放行不能由同一人完成")
        gaps = self._evidence_gaps(order)
        blocking = [gap for gap in gaps if gap["code"] != "missing_quality_release"]
        if blocking:
            raise InvalidState(f"仍存在阻止放行的证据缺口: {blocking}")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE release_approvals SET quality_by=?,quality_at=? WHERE work_order_id=?",
                (actor_id, self._now(), work_order_id),
            )
            self.connection.execute(
                "UPDATE work_orders SET state='released',revision=revision+1,updated_at=? WHERE work_order_id=?",
                (self._now(), work_order_id),
            )
            self._set_item_state(order["target_item_id"], "released", "quality released")
            self.connection.execute(
                "UPDATE items SET state='released',revision=revision+1,updated_at=? "
                "WHERE item_id IN (SELECT child_item_id FROM assembly_memberships "
                "WHERE parent_item_id=? AND state='installed')",
                (self._now(), order["target_item_id"]),
            )
            self._audit("work_order", work_order_id, "release.quality_released", actor_id, {})
        return self.get_work_order(work_order_id)

    def deliver_reassembly(self, actor_id: str, work_order_id: str) -> dict[str, Any]:
        self._require(actor_id, "release.write")
        order = self._work_order(work_order_id)
        if order["order_type"] != "reassembly":
            raise ValidationFailed("该接口只适用于组包工单")
        if order["state"] != "released":
            raise InvalidState("只有双签放行的组包工单可以出场")
        gaps = self._evidence_gaps(order)
        if gaps:
            raise InvalidState(f"仍存在阻止交付的证据缺口: {gaps}")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE work_orders SET state='completed',closed_at=?,updated_at=? WHERE work_order_id=?",
                (self._now(), self._now(), work_order_id),
            )
            self._set_item_state(order["target_item_id"], "delivered", f"delivered from {work_order_id}")
            self.connection.execute(
                "UPDATE items SET state='delivered',revision=revision+1,updated_at=? "
                "WHERE item_id IN (SELECT child_item_id FROM assembly_memberships "
                "WHERE parent_item_id=? AND state='installed')",
                (self._now(), order["target_item_id"]),
            )
            self._audit("work_order", work_order_id, "reassembly.delivered", actor_id, {})
        return self.get_work_order(work_order_id)

    def cancel_reassembly(self, actor_id: str, work_order_id: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "workorder.cancel")
        order = self._work_order(work_order_id)
        if order["order_type"] != "reassembly":
            raise ValidationFailed("该接口只适用于组包工单")
        if order["state"] not in {"draft", "frozen", "in_progress"}:
            raise InvalidState("已放行或已终结的组包工单不能撤销")
        if not reason.strip():
            raise ValidationFailed("撤销原因不能为空")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE work_orders SET state='cancelled',closed_at=?,fail_reason=?,updated_at=? "
                "WHERE work_order_id=?",
                (self._now(), reason.strip(), self._now(), work_order_id),
            )
            if order["state"] == "in_progress":
                # 已装组件全部退回复用库，装配关系逐行拆除，组件不丢失、不多装
                mounted = self.connection.execute(
                    "SELECT child_item_id FROM assembly_memberships "
                    "WHERE parent_item_id=? AND state='installed'",
                    (order["target_item_id"],),
                ).fetchall()
                self.connection.execute(
                    "UPDATE assembly_memberships SET state='removed',removed_by_work_order_id=?,"
                    "removed_reason=?,removed_at=? WHERE parent_item_id=? AND state='installed'",
                    (work_order_id, f"cancelled:{reason.strip()}", self._now(), order["target_item_id"]),
                )
                for row in mounted:
                    self._set_item_state(row["child_item_id"], "available", f"order {work_order_id} cancelled")
            self._set_item_state(order["target_item_id"], "cancelled", reason.strip())
            self._audit("work_order", work_order_id, "reassembly.cancelled", actor_id,
                        {"reason": reason.strip()})
        return self.get_work_order(work_order_id)

    def replace_reassembly_component(
        self,
        actor_id: str,
        work_order_id: str,
        position: str,
        new_component_id: str,
        inspection_id: int,
        reason: str,
        repair_action_id: int | None = None,
    ) -> dict[str, Any]:
        """换件：旧件退回复用库、旧清单行作废、双签全部重置后重新签署。"""
        self._require(actor_id, "rework.write")
        if not reason.strip():
            raise ValidationFailed("换件原因不能为空")
        order = self._work_order(work_order_id)
        if order["order_type"] != "reassembly":
            raise ValidationFailed("该接口只适用于组包工单")
        if order["state"] != "in_progress":
            raise InvalidState("只有执行中的组包工单可以换件")
        line = self.connection.execute(
            "SELECT * FROM reassembly_lines WHERE work_order_id=? AND position=? AND state='active'",
            (work_order_id, position),
        ).fetchone()
        if line is None:
            raise NotFound("仓位上没有生效清单行")
        new_component = self._item_row(new_component_id)
        if new_component["state"] != "available":
            raise InvalidState("新组件必须在复用库中")
        inspection = self._inspection(inspection_id)
        if inspection["component_id"] != new_component_id or inspection["verdict"] != "reuse":
            raise ValidationFailed("必须引用属于新组件、结论为 reuse 的确定版本检测")
        if repair_action_id is not None:
            action = self.connection.execute(
                "SELECT r.state FROM repair_actions a JOIN repair_orders r ON r.repair_order_id=a.repair_order_id "
                "WHERE a.action_id=? AND r.component_id=?",
                (repair_action_id, new_component_id),
            ).fetchone()
            if action is None or action["state"] != "completed":
                raise InvalidState("引用的维修动作不存在、不属于该组件或未完成")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE reassembly_lines SET state='superseded',replaced_reason=? WHERE line_id=?",
                (reason.strip(), line["line_id"]),
            )
            self.connection.execute(
                "UPDATE assembly_memberships SET state='removed',removed_by_work_order_id=?,"
                "removed_reason=?,removed_at=? WHERE parent_item_id=? AND child_item_id=? AND state='installed'",
                (work_order_id, f"replaced:{reason.strip()}", self._now(),
                 order["target_item_id"], line["component_id"]),
            )
            self._set_item_state(line["component_id"], "available",
                                 f"removed from {work_order_id} during rework")
            try:
                cursor = self.connection.execute(
                    "INSERT INTO reassembly_lines(work_order_id,component_id,position,inspection_id,"
                    "repair_action_id,created_at) VALUES(?,?,?,?,?,?)",
                    (work_order_id, new_component_id, position, inspection_id,
                     repair_action_id, self._now()),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("新组件或仓位冲突") from exc
            try:
                self.connection.execute(
                    "INSERT INTO assembly_memberships(parent_item_id,child_item_id,position,work_order_id,"
                    "state,installed_at) VALUES(?,?,?,?, 'installed',?)",
                    (order["target_item_id"], new_component_id, position, work_order_id, self._now()),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("新组件已属于其它生效装配") from exc
            self._set_item_state(new_component_id, "building", f"assembled into {order['target_item_id']}")
            self.connection.execute(
                "UPDATE release_approvals SET technical_by=NULL,technical_at=NULL,"
                "quality_by=NULL,quality_at=NULL WHERE work_order_id=?",
                (work_order_id,),
            )
            self.connection.execute(
                "UPDATE work_orders SET revision=revision+1,updated_at=? WHERE work_order_id=?",
                (self._now(), work_order_id),
            )
            self._audit("work_order", work_order_id, "reassembly.component_replaced", actor_id,
                        {"position": position, "old_component_id": line["component_id"],
                         "new_component_id": new_component_id, "new_line_id": cursor.lastrowid,
                         "reason": reason.strip()})
        return {"work_order_id": work_order_id, "position": position,
                "old_component_id": line["component_id"], "new_component_id": new_component_id}

    # ----- 工单读取与证据缺口 ---------------------------------------------

    def get_work_order(self, work_order_id: str) -> dict[str, Any]:
        return dict(self._work_order(work_order_id))

    def _work_order(self, work_order_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM work_orders WHERE work_order_id=?", (work_order_id,)
        ).fetchone()
        if row is None:
            raise NotFound("工单不存在")
        return row

    def _active_lines(self, work_order_id: str) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT * FROM reassembly_lines WHERE work_order_id=? AND state='active' ORDER BY line_id",
            (work_order_id,),
        ))

    def _assert_mounting_consistent(self, order: sqlite3.Row) -> None:
        """技术确认前实际装配与证据必须齐全（放行签署缺口除外）。"""
        if order["order_type"] != "reassembly":
            return
        ignored = {"missing_technical_confirmation", "missing_quality_release"}
        gaps = [gap for gap in self._evidence_gaps(order) if gap["code"] not in ignored]
        if gaps:
            raise InvalidState(f"仍存在阻止技术确认的问题: {gaps}")

    def _evidence_gaps(self, order: sqlite3.Row) -> list[dict[str, str]]:
        """返回阻止交付的证据缺口；已完成/已出场工单返回空列表。"""
        if order["order_type"] != "reassembly" or order["state"] in {"completed", "cancelled", "failed"}:
            return []
        gaps: list[dict[str, str]] = []
        if order["intake_snapshot_json"] is None:
            gaps.append({"code": "snapshot_not_frozen", "message": "组包清单尚未冻结"})
        lines = self._active_lines(order["work_order_id"])
        mounted = {
            row["child_item_id"]: row
            for row in self.connection.execute(
                "SELECT * FROM assembly_memberships WHERE parent_item_id=? AND state='installed'",
                (order["target_item_id"],),
            ).fetchall()
        }
        for line in lines:
            membership = mounted.pop(line["component_id"], None)
            if membership is None:
                gaps.append({"code": "config_mismatch",
                             "message": f"组件 {line['component_id']} 在清单中但未实际装配"})
            elif membership["position"] != line["position"]:
                gaps.append({"code": "config_mismatch",
                             "message": f"组件 {line['component_id']} 仓位与冻结清单不一致"})
            inspection = self._inspection(line["inspection_id"])
            if inspection["component_id"] != line["component_id"] or inspection["verdict"] != "reuse":
                gaps.append({"code": "inspection_invalid",
                             "message": f"组件 {line['component_id']} 缺少 reuse 结论的确定版本检测"})
            else:
                newer = self.connection.execute(
                    "SELECT version,verdict FROM inspections WHERE component_id=? AND version>? "
                    "ORDER BY version DESC LIMIT 1",
                    (line["component_id"], inspection["version"]),
                ).fetchone()
                if newer is not None and newer["verdict"] != "reuse":
                    gaps.append({
                        "code": "inspection_superseded",
                        "message": f"组件 {line['component_id']} 引用的 reuse 检测已被版本 "
                                   f"{newer['version']}（{newer['verdict']}）推翻，需换件或复检",
                    })
            if line["repair_action_id"] is not None:
                action = self.connection.execute(
                    "SELECT r.state FROM repair_actions a JOIN repair_orders r "
                    "ON r.repair_order_id=a.repair_order_id WHERE a.action_id=? AND r.component_id=?",
                    (line["repair_action_id"], line["component_id"]),
                ).fetchone()
                if action is None or action["state"] != "completed":
                    gaps.append({"code": "repair_incomplete",
                                 "message": f"组件 {line['component_id']} 引用的维修动作未完成"})
            else:
                completed = self.connection.execute(
                    "SELECT count(*) AS c FROM repair_orders "
                    "WHERE component_id=? AND state='completed'",
                    (line["component_id"],),
                ).fetchone()["c"]
                if completed:
                    gaps.append({"code": "repair_action_missing",
                                 "message": f"组件 {line['component_id']} 有已完成维修但清单未引用维修动作"})
            component = self._item_row(line["component_id"])
            if component["state"] in {"quarantined", "scrapped", "repair", "available", "cancelled"}:
                gaps.append({"code": "mounted_component_blocked",
                             "message": f"组件 {line['component_id']} 状态为 {component['state']}，阻止交付"})
        for extra_id in mounted:
            gaps.append({"code": "config_mismatch", "message": f"未登记组件 {extra_id} 已实际装配"})
        approval = self.connection.execute(
            "SELECT * FROM release_approvals WHERE work_order_id=?", (order["work_order_id"],)
        ).fetchone()
        if approval is None or approval["technical_by"] is None:
            gaps.append({"code": "missing_technical_confirmation", "message": "缺少技术确认"})
        if approval is None or approval["quality_by"] is None:
            gaps.append({"code": "missing_quality_release", "message": "缺少质量放行"})
        if approval is not None and approval["technical_by"] is not None and approval["quality_by"] is not None:
            if approval["technical_by"] == approval["quality_by"]:
                gaps.append({"code": "invalid_signoff", "message": "技术确认与质量放行为同一人"})
        return gaps

    def delivery_blockers(self, actor_id: str, work_order_id: str) -> dict[str, Any]:
        self._require(actor_id, "lineage.read")
        order = self._work_order(work_order_id)
        return {
            "work_order_id": work_order_id,
            "order_type": order["order_type"],
            "state": order["state"],
            "blockers": self._evidence_gaps(order),
            "deliverable": not self._evidence_gaps(order),
        }

    # ----- 谱系查询 -------------------------------------------------------

    def lineage_up(self, actor_id: str, item_id: str) -> dict[str, Any]:
        """从新整包向上还原所有来源：装配树、检测版本、维修动作、拆解来源。"""
        self._require(actor_id, "lineage.read")
        root = self._item_row(item_id)
        return {"item": dict(root), "sources": self._source_tree(item_id, frozenset())}

    def _source_tree(self, item_id: str, seen: frozenset[str]) -> list[dict[str, Any]]:
        if item_id in seen:
            return []
        seen = seen | {item_id}
        children = self.connection.execute(
            "SELECT * FROM assembly_memberships WHERE parent_item_id=? AND state='installed' "
            "ORDER BY position,membership_id",
            (item_id,),
        ).fetchall()
        if not children:
            # 已拆解/已撤销的整包：用冻结的进场/组包快照还原来源
            children = self._snapshot_source_memberships(item_id)
        result: list[dict[str, Any]] = []
        for membership in children:
            component = self._item_row(membership["child_item_id"])
            node: dict[str, Any] = {
                "component_id": component["item_id"],
                "item_kind": component["item_kind"],
                "state": component["state"],
                "position": membership["position"],
                "inspections": [dict(row) for row in self.connection.execute(
                    "SELECT inspection_id,version,protocol_id,verdict,content_sha256,inspected_by,inspected_at "
                    "FROM inspections WHERE component_id=? ORDER BY version", (component["item_id"],)
                ).fetchall()],
                "repairs": self._repair_history(component["item_id"]),
                "origin": self._component_origin(component["item_id"]),
                "sources": [],
            }
            node["sources"] = self._source_tree(component["item_id"], seen)
            result.append(node)
        return result

    def _snapshot_source_memberships(self, pack_id: str) -> list[dict[str, str]]:
        """从冻结快照还原已拆解/已撤销整包的进场或组包配置。"""
        order = self.connection.execute(
            "SELECT * FROM work_orders WHERE target_item_id=? "
            "AND intake_snapshot_json IS NOT NULL ORDER BY frozen_at DESC,work_order_id DESC LIMIT 1",
            (pack_id,),
        ).fetchone()
        if order is None:
            return []
        snapshot = json.loads(order["intake_snapshot_json"])
        key = "lines" if order["order_type"] == "reassembly" else "children"
        return [
            {"child_item_id": line["component_id"], "position": line["position"]}
            for line in snapshot.get(key, [])
        ]

    def _repair_history(self, component_id: str) -> list[dict[str, Any]]:
        orders = self.connection.execute(
            "SELECT * FROM repair_orders WHERE component_id=? ORDER BY opened_at,repair_order_id",
            (component_id,),
        ).fetchall()
        history = []
        for order in orders:
            history.append({
                "repair_order_id": order["repair_order_id"],
                "state": order["state"],
                "source_work_order_id": order["source_work_order_id"],
                "opened_by": order["opened_by"],
                "opened_at": order["opened_at"],
                "closed_at": order["closed_at"],
                "actions": [dict(row) for row in self.connection.execute(
                    "SELECT action_id,sequence,action_code,content_sha256,performed_by,performed_at "
                    "FROM repair_actions WHERE repair_order_id=? ORDER BY sequence",
                    (order["repair_order_id"],),
                ).fetchall()],
            })
        return history

    def _component_origin(self, component_id: str) -> dict[str, Any] | None:
        removal = self.connection.execute(
            "SELECT m.*, w.order_type FROM assembly_memberships m "
            "JOIN work_orders w ON w.work_order_id=m.removed_by_work_order_id "
            "WHERE m.child_item_id=? AND m.state='removed' AND w.order_type='disassembly' "
            "ORDER BY m.removed_at DESC,m.membership_id DESC LIMIT 1",
            (component_id,),
        ).fetchone()
        if removal is None:
            return None
        old_pack = self._item_row(removal["parent_item_id"])
        disposition = self.connection.execute(
            "SELECT disposition,note,decided_by,decided_at FROM disassembly_dispositions "
            "WHERE work_order_id=? AND component_id=?",
            (removal["removed_by_work_order_id"], component_id),
        ).fetchone()
        order = self._work_order(removal["removed_by_work_order_id"])
        evidence_ids = [row[0] for row in self.connection.execute(
            "SELECT evidence_id FROM work_order_evidences WHERE work_order_id=? ORDER BY evidence_id",
            (removal["removed_by_work_order_id"],),
        ).fetchall()]
        snapshot = json.loads(order["intake_snapshot_json"]) if order["intake_snapshot_json"] else None
        return {
            "from_pack_id": old_pack["item_id"],
            "from_pack_state": old_pack["state"],
            "disassembly_work_order_id": removal["removed_by_work_order_id"],
            "disposition": None if disposition is None else disposition["disposition"],
            "disposition_by": None if disposition is None else disposition["decided_by"],
            "disposition_at": None if disposition is None else disposition["decided_at"],
            "intake_sha256": order["intake_sha256"],
            "intake_evidence_ids": evidence_ids,
            "intake_components": None if snapshot is None else snapshot.get("children", []),
        }

    def lineage_down(self, actor_id: str, item_id: str) -> dict[str, Any]:
        """从任一旧组件向下查到最终去向、未决动作和阻止交付的证据缺口。"""
        self._require(actor_id, "lineage.read")
        item = self._item_row(item_id)
        mounts = self.connection.execute(
            "SELECT m.membership_id,m.parent_item_id,m.position,m.state,m.work_order_id,"
            "m.removed_by_work_order_id,m.removed_reason,m.installed_at,m.removed_at,"
            "w.order_type AS via_order_type "
            "FROM assembly_memberships m LEFT JOIN work_orders w ON w.work_order_id=m.work_order_id "
            "WHERE m.child_item_id=? ORDER BY m.membership_id",
            (item_id,),
        ).fetchall()
        current_mount = self.connection.execute(
            "SELECT parent_item_id,position,work_order_id FROM assembly_memberships "
            "WHERE child_item_id=? AND state='installed'",
            (item_id,),
        ).fetchone()
        root_pack_id = None
        root_order_id = None
        if current_mount is not None:
            root_pack_id, root_order_id = self._root_pack(current_mount["parent_item_id"])
        pending = self._pending_actions(item_id, current_mount, root_order_id)
        blockers: list[dict[str, str]] = []
        if root_order_id is not None:
            root_order = self._work_order(root_order_id)
            if root_order["order_type"] == "reassembly":
                blockers = self._evidence_gaps(root_order)
        final_destination = self._final_destination(item, current_mount, root_pack_id)
        return {
            "item": dict(item),
            "mount_history": [dict(row) for row in mounts],
            "current_mount": None if current_mount is None else dict(current_mount),
            "root_pack_id": root_pack_id,
            "final_destination": final_destination,
            "pending_actions": pending,
            "delivery_blockers": blockers,
        }

    def _final_destination(
        self,
        item: sqlite3.Row,
        current_mount: sqlite3.Row | None,
        root_pack_id: str | None,
    ) -> dict[str, str] | None:
        if root_pack_id is not None:
            root = self._item_row(root_pack_id)
            if root["state"] == "delivered":
                return {"kind": "delivered_pack", "pack_id": root_pack_id}
        if item["state"] == "scrapped":
            return {"kind": "scrapped"}
        if item["state"] == "retired":
            return {"kind": "retired"}
        if item["state"] == "available" and current_mount is None:
            return {"kind": "reuse_pool"}
        return None

    def _root_pack(self, parent_id: str) -> tuple[str, str | None]:
        """沿生效装配向上找到整包根节点及其组包工单。"""
        current = parent_id
        order_id: str | None = None
        while True:
            mount = self.connection.execute(
                "SELECT parent_item_id,work_order_id FROM assembly_memberships "
                "WHERE child_item_id=? AND state='installed'",
                (current,),
            ).fetchone()
            if mount is None:
                break
            order_id = mount["work_order_id"]
            current = mount["parent_item_id"]
        order = None
        if order_id is not None:
            order = self.connection.execute(
                "SELECT order_type FROM work_orders WHERE work_order_id=?", (order_id,)
            ).fetchone()
        # 原始服役整包没有组包工单；根节点本身是 pack 即可
        item = self._item_row(current)
        if item["item_kind"] != "pack":
            return current, order_id
        return current, order_id

    def _pending_actions(
        self,
        component_id: str,
        current_mount: sqlite3.Row | None,
        root_order_id: str | None,
    ) -> list[dict[str, Any]]:
        pending: list[dict[str, Any]] = []
        repair = self.connection.execute(
            "SELECT repair_order_id,state,opened_at FROM repair_orders WHERE component_id=? AND state='open'",
            (component_id,),
        ).fetchall()
        for row in repair:
            pending.append({"type": "repair_open", "repair_order_id": row["repair_order_id"],
                            "since": row["opened_at"]})
        if current_mount is not None:
            order = self.connection.execute(
                "SELECT work_order_id,state FROM work_orders WHERE target_item_id=? AND state IN (?,?,?,?)",
                (current_mount["parent_item_id"], *OPEN_ORDER_STATES),
            ).fetchone()
            if order is not None and self.connection.execute(
                "SELECT 1 FROM work_orders WHERE work_order_id=? AND order_type='disassembly'",
                (order["work_order_id"],),
            ).fetchone():
                pending.append({"type": "awaiting_disposition",
                                "work_order_id": order["work_order_id"], "order_state": order["state"]})
        if root_order_id is not None:
            line = self.connection.execute(
                "SELECT line_id,position FROM reassembly_lines "
                "WHERE work_order_id=? AND component_id=? AND state='active'",
                (root_order_id, component_id),
            ).fetchone()
            if line is not None:
                order = self._work_order(root_order_id)
                if order["state"] in OPEN_ORDER_STATES:
                    pending.append({"type": "reassembly_in_flight",
                                    "work_order_id": root_order_id, "order_state": order["state"],
                                    "position": line["position"]})
        item = self._item_row(component_id)
        if item["state"] == "quarantined":
            pending.append({"type": "quarantine_decision_required"})
        return pending

    # ----- 审计 -----------------------------------------------------------

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute(
            "SELECT * FROM refurb_audit_events ORDER BY event_id"
        ).fetchall()
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
