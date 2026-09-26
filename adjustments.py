"""滚动调整记录:预览落库、确认后写入执行方案、历史查询。

确认只在记录无冲突且涉及航班版本未变时生效;写入只触及预览中的航段,其他航班保持原样。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any

from common import ApiError, iso, parse_time

SCHEMA = """
CREATE TABLE IF NOT EXISTS rolling_adjustments(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    flight_id INTEGER NOT NULL REFERENCES flights(id),
    action TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'preview',
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    confirmed_by TEXT,
    confirmed_at TEXT
);
"""

# 确认时真正写入执行方案的航段动作;kept/kept_canceled 只展示不落库
WRITTEN_ACTIONS = ("adjusted", "canceled", "shifted")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _hydrate(row: sqlite3.Row) -> dict[str, Any]:
    record = dict(row)
    record["result"] = json.loads(record.pop("result_json"))
    return record


def create_record(conn: sqlite3.Connection, *, flight_id: int, action: str, actor: str, result: dict[str, Any]) -> dict[str, Any]:
    cur = conn.execute("INSERT INTO rolling_adjustments(flight_id,action,status,result_json,created_by,created_at) VALUES(?,?,?,?,?,?)",
                       (flight_id, action, "preview", json.dumps(result, ensure_ascii=False, sort_keys=True), actor, iso()))
    return get_record(conn, cur.lastrowid)


def get_record(conn: sqlite3.Connection, record_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM rolling_adjustments WHERE id=?", (record_id,)).fetchone()
    if not row:
        raise ApiError(404, "rolling_not_found", "滚动调整记录不存在")
    return _hydrate(row)


def list_records(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    return [_hydrate(row) for row in conn.execute("SELECT * FROM rolling_adjustments ORDER BY id DESC LIMIT ?", (limit,))]


def confirm_record(conn: sqlite3.Connection, record_id: int, actor: str) -> dict[str, Any]:
    """确认一条预览记录,把其中的航段变更写入执行方案(flights 表)。"""
    record = get_record(conn, record_id)
    if record["status"] == "confirmed":
        return {"record": record, "flights": [], "idempotent": True}
    result = record["result"]
    if result["conflicts"]:
        raise ApiError(409, "rolling_has_conflicts", "滚动预览存在冲突,不能写入执行方案", result["conflicts"])
    writes = [segment for segment in result["segments"] if segment["action"] in WRITTEN_ACTIONS]
    stale = []
    for segment in writes:
        row = conn.execute("SELECT revision FROM flights WHERE id=?", (segment["flight_id"],)).fetchone()
        if not row or row["revision"] != segment["revision"]:
            stale.append({"flight_id": segment["flight_id"], "flight_no": segment["flight_no"]})
    if stale:
        raise ApiError(409, "stale_preview", "预览之后航班已被变更,请重新滚动预览", stale)
    now = iso()
    flights = []
    for segment in writes:
        if segment["action"] == "canceled":
            conn.execute("UPDATE flights SET status='canceled',cancel_reason=?,revision=revision+1,updated_at=? WHERE id=?",
                         (result["adjustment"].get("reason") or "滚动调整取消", now, segment["flight_id"]))
        else:
            delay = max(0, int((parse_time(segment["new_std"]) - parse_time(segment["old_std"])).total_seconds() // 60))
            if segment["action"] == "adjusted":
                conn.execute("UPDATE flights SET std=?,sta=?,aircraft_id=?,crew_id=?,delay_minutes=?,revision=revision+1,updated_at=? WHERE id=?",
                             (segment["new_std"], segment["new_sta"], result["adjustment"]["aircraft_id"], result["adjustment"]["crew_id"],
                              delay, now, segment["flight_id"]))
            else:
                conn.execute("UPDATE flights SET std=?,sta=?,delay_minutes=?,revision=revision+1,updated_at=? WHERE id=?",
                             (segment["new_std"], segment["new_sta"], delay, now, segment["flight_id"]))
        flights.append(dict(conn.execute("SELECT * FROM flights WHERE id=?", (segment["flight_id"],)).fetchone()))
    conn.execute("UPDATE rolling_adjustments SET status='confirmed',confirmed_by=?,confirmed_at=? WHERE id=?", (actor, now, record_id))
    return {"record": get_record(conn, record_id), "flights": flights, "idempotent": False}
