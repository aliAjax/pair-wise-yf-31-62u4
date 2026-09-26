"""Rolling-adjustment business service: records, preview and confirm.

This module owns persistence for dispatcher rolling adjustments and bridges
the HTTP layer and the pure propagation engine in :mod:`rolling`.

Workflow:
1. ``preview_rolling`` calculates the downstream chain and returns every
   proposed leg plus explicit conflicts (flight number + constraint) without
   writing anything.
2. ``confirm_rolling`` re-runs the calculation inside one transaction. If any
   conflict remains the whole call is rejected; otherwise only the affected
   legs are written into a recovery plan (created on the fly or updated),
   other flights/assignments stay untouched, and a rolling_adjustments record
   plus an audit entry are written.
"""
from __future__ import annotations

import json
from typing import Any

import rolling

ROLLING_DDL = """
CREATE TABLE IF NOT EXISTS rolling_adjustments(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER REFERENCES recovery_plans(id),
    trigger_flight_id INTEGER NOT NULL REFERENCES flights(id),
    trigger_flight_no TEXT NOT NULL,
    action TEXT NOT NULL,
    aircraft_id TEXT NOT NULL,
    crew_id TEXT NOT NULL,
    shift_minutes INTEGER NOT NULL DEFAULT 0,
    leg_count INTEGER NOT NULL DEFAULT 0,
    shifted_count INTEGER NOT NULL DEFAULT 0,
    canceled_count INTEGER NOT NULL DEFAULT 0,
    total_delay_minutes INTEGER NOT NULL DEFAULT 0,
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);"""


def ensure_rolling_tables(conn: Any) -> None:
    conn.executescript(ROLLING_DDL)


class RollingAdjustmentService:
    """Mixin for :class:`app.AirlineRecoveryService`; needs ``self.repo``."""

    # ---- snapshots & input -------------------------------------------------

    def _rolling_snapshot(self, conn: Any) -> tuple[list[dict[str, Any]], dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        flights = [dict(r) for r in conn.execute("SELECT * FROM flights")]
        aircraft = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM aircraft")}
        crew = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM crew")}
        airports = {r["code"]: dict(r) for r in conn.execute("SELECT * FROM airports")}
        permits = [dict(r) for r in conn.execute("SELECT * FROM permits")]
        return flights, aircraft, crew, airports, permits

    @staticmethod
    def _rolling_kwargs(body: dict[str, Any]) -> dict[str, Any]:
        from rolling import ApiError, iso, parse_time

        flight_id = body.get("flight_id")
        if not isinstance(flight_id, int):
            raise ApiError(400, "flight_id_required", "flight_id 必须是整数")
        action = body.get("action", "shift")
        if action not in {"shift", "cancel"}:
            raise ApiError(400, "invalid_action", "action 只能是 shift（顺延/换机）或 cancel（保留取消）")
        kwargs: dict[str, Any] = {"trigger_flight_id": flight_id, "action": action}
        if action == "shift":
            if not body.get("new_std") or not body.get("new_sta"):
                raise ApiError(400, "missing_fields", "顺延调整必须提供 new_std 和 new_sta")
            new_std, new_sta = parse_time(body["new_std"]), parse_time(body["new_sta"])
            if new_sta <= new_std:
                raise ApiError(400, "invalid_times", "新到达时间必须晚于新起飞时间")
            kwargs["new_std"], kwargs["new_sta"] = iso(new_std), iso(new_sta)
        if body.get("aircraft_id"):
            kwargs["aircraft_id"] = str(body["aircraft_id"]).strip()
        if body.get("crew_id"):
            kwargs["crew_id"] = str(body["crew_id"]).strip()
        return kwargs

    def _run_propagation(self, conn: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
        from rolling import ApiError

        flights, aircraft, crew, airports, permits = self._rolling_snapshot(conn)
        try:
            return rolling.propagate(trigger_flight_id=kwargs["trigger_flight_id"], flights=flights,
                                     aircraft=aircraft, crew=crew, airports=airports, permits=permits,
                                     action=kwargs["action"], new_std=kwargs.get("new_std"),
                                     new_sta=kwargs.get("new_sta"), aircraft_id=kwargs.get("aircraft_id"),
                                     crew_id=kwargs.get("crew_id"))
        except LookupError as exc:
            raise ApiError(404, "flight_not_found", str(exc)) from exc
        except ValueError as exc:
            raise ApiError(400, "invalid_rolling", str(exc)) from exc

    # ---- preview (no writes) ----------------------------------------------

    def preview_rolling(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        from rolling import ApiError

        if role not in {"scheduler", "ops_manager", "auditor"}:
            raise ApiError(403, "rolling_forbidden", "当前角色不能预览滚动恢复")
        kwargs = self._rolling_kwargs(body)
        with self.repo.tx() as conn:
            if not conn.execute("SELECT 1 FROM flights WHERE id=?", (kwargs["trigger_flight_id"],)).fetchone():
                raise ApiError(404, "flight_not_found", "触发滚动的航班不存在")
            return self._run_propagation(conn, kwargs)
    # ---- confirm (atomic write) -------------------------------------------

    def confirm_rolling(self, actor: str, role: str, body: dict[str, Any]) -> dict[str, Any]:
        from rolling import ApiError, iso
        from app import Repository

        if role not in {"scheduler", "ops_manager"}:
            raise ApiError(403, "rolling_forbidden", "当前角色不能确认滚动调整")
        kwargs = self._rolling_kwargs(body)
        plan_id, disruption_id = body.get("plan_id"), body.get("disruption_id")
        plan_name = str(body.get("name", "滚动恢复调整")).strip() or "滚动恢复调整"
        expected = body.get("expected_revision")
        if plan_id is not None and not isinstance(plan_id, int):
            raise ApiError(400, "invalid_plan", "plan_id 必须是整数")
        if plan_id is None and not isinstance(disruption_id, int):
            raise ApiError(400, "disruption_required", "写入新方案时 disruption_id 必填；写入已有方案请提供 plan_id")
        if expected is not None and not isinstance(expected, int):
            raise ApiError(400, "revision_required", "expected_revision 必须是整数")

        with self.repo.tx() as conn:
            trigger = conn.execute("SELECT * FROM flights WHERE id=?", (kwargs["trigger_flight_id"],)).fetchone()
            if not trigger:
                raise ApiError(404, "flight_not_found", "触发滚动的航班不存在")
            if trigger["status"] == "canceled":
                raise ApiError(409, "flight_canceled", f"航班 {trigger['flight_no']} 已取消，不能作为滚动调整起点")

            created_plan = False
            if plan_id is None:
                if not conn.execute("SELECT 1 FROM disruptions WHERE id=?", (disruption_id,)).fetchone():
                    raise ApiError(404, "disruption_not_found", "中断事件不存在")
                cur = conn.execute("INSERT INTO recovery_plans(disruption_id,name,created_by,created_at) VALUES(?,?,?,?)",
                                   (disruption_id, plan_name, actor, iso()))
                plan_id = cur.lastrowid
                created_plan = True
            else:
                plan = conn.execute("SELECT * FROM recovery_plans WHERE id=?", (plan_id,)).fetchone()
                if not plan:
                    raise ApiError(404, "plan_not_found", "执行方案不存在")
                if plan["status"] != "draft":
                    raise ApiError(409, "plan_locked", "已锁定方案不能再追加滚动调整")
                if expected is not None and plan["revision"] != expected:
                    raise ApiError(409, "revision_conflict", "方案版本已变化，请刷新后重试")

            result = self._run_propagation(conn, kwargs)
            if result["conflicts"]:
                # Nothing is written: rollback the plan we may have created too.
                raise ApiError(409, "rolling_conflict",
                               f"滚动到 {len(result['conflicts'])} 处约束冲突，未写入执行方案", result)

            affected = [leg for leg in result["legs"] if leg["action"] in ("shift", "cancel")]
            for leg in affected:
                self._upsert_rolling_assignment(conn, plan_id, leg)

            conn.execute("UPDATE recovery_plans SET revision=revision+1 WHERE id=?", (plan_id,))
            trigger_leg = result["legs"][0]
            cur = conn.execute(
                """INSERT INTO rolling_adjustments(plan_id,trigger_flight_id,trigger_flight_no,action,aircraft_id,crew_id,
                   shift_minutes,leg_count,shifted_count,canceled_count,total_delay_minutes,result_json,created_by,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (plan_id, trigger["id"], trigger["flight_no"], kwargs["action"],
                 trigger_leg["aircraft_id"], trigger_leg["crew_id"], max(0, trigger_leg["delay_minutes"]),
                 result["summary"]["leg_count"], result["summary"]["shifted_count"],
                 result["summary"]["canceled_count"], result["summary"]["total_delay_minutes"],
                 json.dumps(result, ensure_ascii=False, sort_keys=True), actor, iso()))
            record_id = cur.lastrowid
            Repository.audit(conn, plan_id, actor, role, "rolling_confirmed",
                             {"record_id": record_id, "trigger_flight_no": trigger["flight_no"],
                              "action": kwargs["action"], "affected_flights": [leg["flight_no"] for leg in affected],
                              "created_plan": created_plan})
            record = dict(conn.execute("SELECT * FROM rolling_adjustments WHERE id=?", (record_id,)).fetchone())
            return {"record": record, "plan": self.get_plan(plan_id), "created_plan": created_plan}

    @staticmethod
    def _upsert_rolling_assignment(conn: Any, plan_id: int, leg: dict[str, Any]) -> None:
        """Write one affected leg; canceled legs keep original times as placeholders."""
        if leg["action"] == "cancel":
            new_std, new_sta, status = leg["old_std"], leg["old_sta"], "canceled"
        else:
            new_std, new_sta, status = leg["new_std"], leg["new_sta"], "planned"
        conn.execute(
            """INSERT INTO assignments(plan_id,flight_id,aircraft_id,crew_id,new_std,new_sta,status,delay_minutes,missed_connections)
               VALUES(?,?,?,?,?,?,?,?,0)
               ON CONFLICT(plan_id,flight_id) DO UPDATE SET
                 aircraft_id=excluded.aircraft_id, crew_id=excluded.crew_id,
                 new_std=excluded.new_std, new_sta=excluded.new_sta, status=excluded.status,
                 delay_minutes=excluded.delay_minutes""",
            (plan_id, leg["flight_id"], leg["aircraft_id"], leg["crew_id"],
             new_std, new_sta, status, max(0, leg["delay_minutes"])))

    # ---- adjustment history ------------------------------------------------

    def list_rolling_records(self, actor: str, role: str, plan_id: int | None = None) -> dict[str, Any]:
        if plan_id is not None:
            if not isinstance(plan_id, int):
                raise ApiError(400, "invalid_plan", "plan_id 必须是整数")
            rows = self.repo.conn.execute(
                "SELECT * FROM rolling_adjustments WHERE plan_id=? ORDER BY id DESC", (plan_id,)).fetchall()
        else:
            rows = self.repo.conn.execute(
                "SELECT * FROM rolling_adjustments ORDER BY id DESC LIMIT 50").fetchall()
        records = []
        for row in rows:
            item = dict(row)
            item["result"] = json.loads(item.pop("result_json"))
            records.append(item)
        return {"records": records}
