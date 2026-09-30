"""维修与翻新谱系服务的 SQLite 模式和事务辅助。

完整性要点：

* ``assembly_memberships`` 以部分唯一索引同时保证「一个序列件至多属于一条
  生效装配」与「一个父件仓位至多挂载一个生效子件」，从数据库层杜绝一物多装；
* 装配关系只追加、不改写：撤销、返工、换件都是把旧关系置为 ``removed`` 再写
  新行，已经出场（delivered）的配置连置位都不允许，历史配置永久可还原；
* 工单、组件、检测、维修全部保留版本号与内容摘要，组包只能引用确定版本。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA_VERSION = 1

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS refurb_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN
        ('planner','technician','engineer','quality','auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0,1)),
    created_at TEXT NOT NULL
);

-- 序列件：整包、模组、电芯、BMS 等任何需要单件追溯的对象
CREATE TABLE IF NOT EXISTS items (
    item_id TEXT PRIMARY KEY,
    item_kind TEXT NOT NULL CHECK (item_kind IN
        ('pack','module','cell','bms','component')),
    model_name TEXT NOT NULL,
    vendor TEXT NOT NULL,
    -- in_service  在场服役的原始配置
    -- available  复用库，可被新装配引用
    -- repair     维修中
    -- quarantined 隔离，禁止装配
    -- scrapped   已报废（终态）
    -- retired    原整包拆解完毕后退役（终态）
    -- building   新整包组包中
    -- released   技术确认与质量放行完成、待出场
    -- delivered  已出场（终态，配置冻结）
    -- cancelled  组包撤销（整包构建记录终态，不影响已拆组件）
    state TEXT NOT NULL CHECK (state IN
        ('in_service','available','repair','quarantined','scrapped',
         'retired','building','released','delivered','cancelled')),
    state_reason TEXT,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_by TEXT NOT NULL REFERENCES refurb_users(user_id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_items_state ON items(state, item_kind);

-- 故障证据：进场后、拆解前必须冻结
CREATE TABLE IF NOT EXISTS fault_evidences (
    evidence_id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES items(item_id),
    evidence_kind TEXT NOT NULL,
    summary TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256)=64),
    recorded_by TEXT NOT NULL REFERENCES refurb_users(user_id),
    recorded_at TEXT NOT NULL,
    UNIQUE(item_id, content_sha256)
);

-- 工单：拆解或重新组包
CREATE TABLE IF NOT EXISTS work_orders (
    work_order_id TEXT PRIMARY KEY,
    order_type TEXT NOT NULL CHECK (order_type IN ('disassembly','reassembly')),
    target_item_id TEXT NOT NULL REFERENCES items(item_id),
    state TEXT NOT NULL CHECK (state IN
        ('draft','frozen','in_progress','released','completed','failed','cancelled')),
    -- 进场配置快照（拆解单）或组包清单（组包单），冻结后不可变
    intake_snapshot_json TEXT,
    intake_sha256 TEXT,
    frozen_at TEXT,
    closed_at TEXT,
    fail_reason TEXT,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_by TEXT NOT NULL REFERENCES refurb_users(user_id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_work_orders_target ON work_orders(target_item_id);

CREATE TABLE IF NOT EXISTS work_order_evidences (
    work_order_id TEXT NOT NULL REFERENCES work_orders(work_order_id),
    evidence_id TEXT NOT NULL REFERENCES fault_evidences(evidence_id),
    PRIMARY KEY(work_order_id, evidence_id)
);

-- 拆出组件的处置去向：每个组件分别决定 reuse / repair / scrap / quarantine
CREATE TABLE IF NOT EXISTS disassembly_dispositions (
    disposition_id INTEGER PRIMARY KEY AUTOINCREMENT,
    work_order_id TEXT NOT NULL REFERENCES work_orders(work_order_id),
    component_id TEXT NOT NULL REFERENCES items(item_id),
    disposition TEXT NOT NULL CHECK (disposition IN
        ('reuse','repair','scrap','quarantine')),
    note TEXT NOT NULL DEFAULT '',
    decided_by TEXT NOT NULL REFERENCES refurb_users(user_id),
    decided_at TEXT NOT NULL,
    UNIQUE(work_order_id, component_id)
);

-- 装配关系（谱系边）。只追加：换件/返工 = 旧行 removed + 新行 installed
CREATE TABLE IF NOT EXISTS assembly_memberships (
    membership_id INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_item_id TEXT NOT NULL REFERENCES items(item_id),
    child_item_id TEXT NOT NULL REFERENCES items(item_id),
    position TEXT NOT NULL,
    -- 平台接管前的既有配置没有工单，记为 NULL（进场快照会冻结它们）
    work_order_id TEXT REFERENCES work_orders(work_order_id),
    state TEXT NOT NULL CHECK (state IN ('installed','removed')),
    -- 撤销/返工/换件时指向执行动作的工单
    removed_by_work_order_id TEXT REFERENCES work_orders(work_order_id),
    removed_reason TEXT,
    installed_at TEXT NOT NULL,
    removed_at TEXT,
    CHECK (parent_item_id <> child_item_id)
);

-- 任一序列件同一时刻只能属于一条生效装配（也不能同时装在两个仓位）
CREATE UNIQUE INDEX IF NOT EXISTS one_active_membership_per_child
ON assembly_memberships(child_item_id)
WHERE state='installed';

-- 同一父件的同一仓位不能同时挂两个件
CREATE UNIQUE INDEX IF NOT EXISTS one_active_occupant_per_position
ON assembly_memberships(parent_item_id, position)
WHERE state='installed';

CREATE INDEX IF NOT EXISTS idx_memberships_parent
ON assembly_memberships(parent_item_id, state);

CREATE INDEX IF NOT EXISTS idx_memberships_child
ON assembly_memberships(child_item_id, state);

-- 组件检测版本：结论支持复用/送修/报废/隔离
CREATE TABLE IF NOT EXISTS inspections (
    inspection_id INTEGER PRIMARY KEY AUTOINCREMENT,
    component_id TEXT NOT NULL REFERENCES items(item_id),
    version INTEGER NOT NULL CHECK (version > 0),
    protocol_id TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK (verdict IN ('reuse','repair','scrap','quarantine')),
    metrics_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256)=64),
    inspected_by TEXT NOT NULL REFERENCES refurb_users(user_id),
    inspected_at TEXT NOT NULL,
    UNIQUE(component_id, version)
);

CREATE INDEX IF NOT EXISTS idx_inspections_component ON inspections(component_id, version);

-- 维修工单与确定版本的维修动作
CREATE TABLE IF NOT EXISTS repair_orders (
    repair_order_id TEXT PRIMARY KEY,
    component_id TEXT NOT NULL REFERENCES items(item_id),
    source_work_order_id TEXT REFERENCES work_orders(work_order_id),
    state TEXT NOT NULL CHECK (state IN ('open','completed','cancelled')),
    opened_by TEXT NOT NULL REFERENCES refurb_users(user_id),
    opened_at TEXT NOT NULL,
    closed_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS one_open_repair_per_component
ON repair_orders(component_id)
WHERE state='open';

CREATE TABLE IF NOT EXISTS repair_actions (
    action_id INTEGER PRIMARY KEY AUTOINCREMENT,
    repair_order_id TEXT NOT NULL REFERENCES repair_orders(repair_order_id),
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    action_code TEXT NOT NULL,
    detail TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256)=64),
    performed_by TEXT NOT NULL REFERENCES refurb_users(user_id),
    performed_at TEXT NOT NULL,
    UNIQUE(repair_order_id, sequence)
);

-- 组包清单：组包工单冻结的 BOM 行，逐行引用确定版本检测/维修动作。
-- 换件保留原行（superseded），避免物理删除造成来源断裂；唯一性只对生效行。
CREATE TABLE IF NOT EXISTS reassembly_lines (
    line_id INTEGER PRIMARY KEY AUTOINCREMENT,
    work_order_id TEXT NOT NULL REFERENCES work_orders(work_order_id),
    component_id TEXT NOT NULL REFERENCES items(item_id),
    position TEXT NOT NULL,
    inspection_id INTEGER NOT NULL REFERENCES inspections(inspection_id),
    repair_action_id INTEGER REFERENCES repair_actions(action_id),
    state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active','superseded')),
    replaced_reason TEXT,
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_line_per_component
ON reassembly_lines(work_order_id, component_id)
WHERE state='active';

CREATE UNIQUE INDEX IF NOT EXISTS one_active_line_per_position
ON reassembly_lines(work_order_id, position)
WHERE state='active';

-- 双签：技术确认与质量放行必须由不同职责人员完成，分两步签署
CREATE TABLE IF NOT EXISTS release_approvals (
    work_order_id TEXT PRIMARY KEY REFERENCES work_orders(work_order_id),
    technical_by TEXT REFERENCES refurb_users(user_id),
    technical_at TEXT,
    quality_by TEXT REFERENCES refurb_users(user_id),
    quality_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS release_signers_must_differ
ON release_approvals(work_order_id)
WHERE technical_by IS NOT NULL AND quality_by IS NOT NULL
  AND technical_by = quality_by;

-- 谱系事件：审计链，append-only
CREATE TABLE IF NOT EXISTS refurb_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_refurb_audit_entity
ON refurb_audit_events(entity_type, entity_id, event_id);
"""


REQUIRED_TABLES = frozenset({
    "schema_meta", "refurb_users", "items", "fault_evidences", "work_orders",
    "work_order_evidences", "disassembly_dispositions", "assembly_memberships",
    "inspections", "repair_orders", "repair_actions", "reassembly_lines",
    "release_approvals", "refurb_audit_events",
})


def connect(path: str | Path) -> sqlite3.Connection:
    # ThreadingHTTPServer 会在工作线程中复用该连接；写入统一走 BEGIN IMMEDIATE
    # 并由 busy_timeout 串行化，因此关闭同线程限制是安全的。
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10,
                                 check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA_SQL)
    with transaction(connection, immediate=True):
        connection.execute(
            "INSERT INTO schema_meta(key,value) VALUES('schema_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
