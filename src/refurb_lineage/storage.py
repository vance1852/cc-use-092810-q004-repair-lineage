"""维修与翻新谱系服务的 SQLite 模式与事务辅助。"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path


SCHEMA_VERSION = 1

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('intake', 'technician', 'quality', 'auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS components (
    component_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('module', 'cell', 'bms', 'other')),
    model_name TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('installed', 'reuse', 'repair', 'quarantine', 'scrap')),
    state_reason TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assemblies (
    assembly_id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('pack', 'device')),
    origin TEXT NOT NULL CHECK (origin IN ('external', 'refurb')),
    state TEXT NOT NULL CHECK (state IN ('open', 'confirmed', 'released', 'shipped', 'dismantled', 'void')),
    destination TEXT,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    confirmed_by TEXT REFERENCES users(user_id),
    confirmed_at TEXT,
    released_by TEXT REFERENCES users(user_id),
    released_at TEXT,
    shipped_by TEXT REFERENCES users(user_id),
    shipped_at TEXT,
    closed_at TEXT
);

CREATE TABLE IF NOT EXISTS inspections (
    inspection_id INTEGER PRIMARY KEY AUTOINCREMENT,
    component_id TEXT NOT NULL REFERENCES components(component_id),
    version INTEGER NOT NULL CHECK (version > 0),
    result TEXT NOT NULL CHECK (result IN ('pass', 'fail')),
    summary TEXT NOT NULL DEFAULT '',
    metrics_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    recorded_by TEXT NOT NULL REFERENCES users(user_id),
    recorded_at TEXT NOT NULL,
    UNIQUE (component_id, version),
    UNIQUE (content_sha256)
);

CREATE TABLE IF NOT EXISTS repair_actions (
    repair_id INTEGER PRIMARY KEY AUTOINCREMENT,
    component_id TEXT NOT NULL REFERENCES components(component_id),
    version INTEGER NOT NULL CHECK (version > 0),
    action TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('open', 'completed', 'abandoned')),
    opened_by TEXT NOT NULL REFERENCES users(user_id),
    opened_at TEXT NOT NULL,
    closed_by TEXT REFERENCES users(user_id),
    closed_at TEXT,
    close_note TEXT,
    close_inspection_id INTEGER REFERENCES inspections(inspection_id),
    UNIQUE (component_id, version)
);

CREATE UNIQUE INDEX IF NOT EXISTS one_open_repair_per_component
ON repair_actions(component_id) WHERE state = 'open';

CREATE TABLE IF NOT EXISTS assembly_members (
    member_id INTEGER PRIMARY KEY AUTOINCREMENT,
    assembly_id TEXT NOT NULL REFERENCES assemblies(assembly_id),
    component_id TEXT NOT NULL REFERENCES components(component_id),
    position TEXT NOT NULL,
    inspection_id INTEGER REFERENCES inspections(inspection_id),
    repair_id INTEGER REFERENCES repair_actions(repair_id),
    installed_by TEXT NOT NULL REFERENCES users(user_id),
    installed_at TEXT NOT NULL,
    removed_by TEXT REFERENCES users(user_id),
    removed_at TEXT,
    removal_reason TEXT CHECK (removal_reason IN ('disassembled', 'swapped', 'void'))
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_membership_per_component
ON assembly_members(component_id) WHERE removed_at IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS one_active_component_per_position
ON assembly_members(assembly_id, position) WHERE removed_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_members_assembly
ON assembly_members(assembly_id, removed_at);

CREATE TABLE IF NOT EXISTS work_orders (
    work_order_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('teardown', 'rebuild')),
    source_assembly_id TEXT REFERENCES assemblies(assembly_id),
    target_assembly_id TEXT REFERENCES assemblies(assembly_id),
    state TEXT NOT NULL CHECK (state IN ('draft', 'frozen', 'in_progress', 'completed', 'failed', 'cancelled')),
    fault_evidence_json TEXT,
    frozen_at TEXT,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    closed_by TEXT REFERENCES users(user_id),
    closed_at TEXT,
    close_note TEXT,
    CHECK ((kind = 'teardown' AND source_assembly_id IS NOT NULL AND target_assembly_id IS NULL)
        OR (kind = 'rebuild' AND target_assembly_id IS NOT NULL AND source_assembly_id IS NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_teardown_per_assembly
ON work_orders(source_assembly_id) WHERE kind = 'teardown' AND state IN ('draft', 'frozen', 'in_progress');

CREATE UNIQUE INDEX IF NOT EXISTS one_active_rebuild_per_assembly
ON work_orders(target_assembly_id) WHERE kind = 'rebuild' AND state IN ('draft', 'in_progress');

CREATE TABLE IF NOT EXISTS work_order_intake_items (
    work_order_id TEXT NOT NULL REFERENCES work_orders(work_order_id),
    component_id TEXT NOT NULL REFERENCES components(component_id),
    position TEXT NOT NULL,
    disposition TEXT CHECK (disposition IN ('reuse', 'repair', 'scrap', 'quarantine')),
    disposition_reason TEXT,
    dispositioned_by TEXT REFERENCES users(user_id),
    dispositioned_at TEXT,
    PRIMARY KEY (work_order_id, component_id)
);

CREATE INDEX IF NOT EXISTS idx_intake_component
ON work_order_intake_items(component_id, dispositioned_at);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (scope, key)
);

CREATE TABLE IF NOT EXISTS audit_events (
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

CREATE INDEX IF NOT EXISTS idx_audit_entity
ON audit_events(entity_type, entity_id, event_id);
"""

REQUIRED_TABLES = frozenset({
    "schema_meta", "users", "components", "assemblies", "inspections", "repair_actions",
    "assembly_members", "work_orders", "work_order_intake_items", "idempotency_keys",
    "audit_events",
})


def connect(path: str | Path) -> sqlite3.Connection:
    """打开连接并启用严格的事务与外键设置。

    允许跨线程使用：多线程 HTTP 入口在应用层用锁串行化访问。
    """

    connection = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


@contextlib.contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    """显式事务；异常时保证回滚。"""

    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def initialize(connection: sqlite3.Connection) -> None:
    """初始化谱系表结构，重复执行不改变已有数据。"""

    connection.executescript(SCHEMA_SQL)
    with transaction(connection, immediate=True):
        connection.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )


def inspect_schema(connection: sqlite3.Connection) -> dict[str, object]:
    """返回适合机器检查的数据库结构摘要。"""

    table_rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    tables = tuple(row["name"] for row in table_rows)
    version_row = connection.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    missing = sorted(REQUIRED_TABLES - set(tables))
    foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    return {
        "tables": tables,
        "missing_tables": missing,
        "schema_version": None if version_row is None else version_row["value"],
        "foreign_keys": bool(foreign_keys),
    }
