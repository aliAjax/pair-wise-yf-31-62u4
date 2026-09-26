"""恢复滚动计算:调度员调整一趟航班后,沿同一架飞机的后续航段顺延或保留取消。

前一班尚未落地,后一班不能沿用原起飞时刻;逐段核对维护、执勤、宵禁和航线许可,
冲突写清航班号和约束。本模块只读数据库并返回预览结果,不写任何表。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Any

from common import ApiError, iso, parse_clock, parse_time

# 与方案校验一致的约束代码,便于调度台对照
MAINTENANCE = "maintenance_due"
DUTY_LIMIT = "duty_limit"
CURFEW = "airport_curfew"
PERMIT_MISSING = "route_permit_missing"
PERMIT_WINDOW = "route_permit_window"
AIRCRAFT_UNAVAILABLE = "aircraft_unavailable"
CREW_UNAVAILABLE = "crew_unavailable"
AIRPORT_UNKNOWN = "airport_unknown"
AIRCRAFT_OVERLAP = "aircraft_overlap"


def _conflict(flight_no: str, constraint: str, message: str, **extra: Any) -> dict[str, Any]:
    item = {"flight_no": flight_no, "constraint": constraint, "message": message}
    item.update(extra)
    return item


def check_segment(conn: sqlite3.Connection, *, flight_no: str, origin: str, destination: str,
                  aircraft_id: str, crew_id: str, std: datetime, sta: datetime) -> list[dict[str, Any]]:
    """逐段核对维护、执勤、宵禁和航线许可,返回带航班号和约束的冲突列表。"""
    problems: list[dict[str, Any]] = []
    aircraft = conn.execute("SELECT * FROM aircraft WHERE id=?", (aircraft_id,)).fetchone()
    if not aircraft or aircraft["status"] != "active":
        problems.append(_conflict(flight_no, AIRCRAFT_UNAVAILABLE, f"航班 {flight_no} 的飞机 {aircraft_id} 不可用", resource=aircraft_id))
    elif parse_time(aircraft["maintenance_due"]) < sta:
        problems.append(_conflict(flight_no, MAINTENANCE,
                                  f"航班 {flight_no} 到达 {iso(sta)} 晚于飞机 {aircraft_id} 维护到期 {aircraft['maintenance_due']}",
                                  resource=aircraft_id))
    crew = conn.execute("SELECT * FROM crew WHERE id=?", (crew_id,)).fetchone()
    if not crew or crew["status"] != "active":
        problems.append(_conflict(flight_no, CREW_UNAVAILABLE, f"航班 {flight_no} 的机组 {crew_id} 不可用", resource=crew_id))
    else:
        duty_minutes = (sta - parse_time(crew["duty_start"])).total_seconds() / 60
        if duty_minutes > crew["max_duty_minutes"]:
            problems.append(_conflict(flight_no, DUTY_LIMIT,
                                      f"航班 {flight_no} 落地后机组 {crew_id} 执勤 {int(duty_minutes)} 分钟,超过上限 {crew['max_duty_minutes']} 分钟",
                                      resource=crew_id))
    origin_row = conn.execute("SELECT * FROM airports WHERE code=?", (origin,)).fetchone()
    destination_row = conn.execute("SELECT * FROM airports WHERE code=?", (destination,)).fetchone()
    if not origin_row or not destination_row:
        problems.append(_conflict(flight_no, AIRPORT_UNKNOWN, f"航班 {flight_no} 的机场 {origin} 或 {destination} 未登记"))
    elif origin != destination:
        permit = conn.execute("SELECT * FROM permits WHERE origin=? AND destination=? AND valid_from<=? AND valid_to>=?",
                              (origin, destination, iso(sta), iso(sta))).fetchone()
        curfew_start, curfew_end = parse_clock(destination_row["curfew_start"]), parse_clock(destination_row["curfew_end"])
        arrival = sta.timetz().replace(tzinfo=None)
        inside = (arrival >= curfew_start or arrival < curfew_end) if curfew_start > curfew_end else curfew_start <= arrival < curfew_end
        if inside and not (permit and permit["curfew_exempt"]):
            problems.append(_conflict(flight_no, CURFEW,
                                      f"航班 {flight_no} 到达 {destination} 的时刻落入宵禁 {destination_row['curfew_start']}-{destination_row['curfew_end']}",
                                      resource=destination))
        if not permit:
            problems.append(_conflict(flight_no, PERMIT_MISSING, f"航班 {flight_no} 缺少 {origin}-{destination} 航线许可", resource=f"{origin}-{destination}"))
        elif not parse_time(permit["valid_from"]) <= std <= parse_time(permit["valid_to"]):
            problems.append(_conflict(flight_no, PERMIT_WINDOW,
                                      f"航班 {flight_no} 起飞 {iso(std)} 不在许可有效期 {permit['valid_from']} 至 {permit['valid_to']} 内",
                                      resource=f"{origin}-{destination}"))
    return problems


def _entry(row: sqlite3.Row) -> dict[str, Any]:
    std, sta = parse_time(row["std"]), parse_time(row["sta"])
    return {"flight_id": row["id"], "flight_no": row["flight_no"], "origin": row["origin"], "destination": row["destination"],
            "aircraft_id": row["aircraft_id"], "crew_id": row["crew_id"], "status": row["status"], "revision": row["revision"],
            "old_std": std, "old_sta": sta, "new_std": std, "new_sta": sta, "duration": sta - std}


def _segment_view(entry: dict[str, Any]) -> dict[str, Any]:
    delay = max(0, int((entry["new_std"] - entry["old_std"]).total_seconds() // 60))
    return {"flight_id": entry["flight_id"], "flight_no": entry["flight_no"], "origin": entry["origin"], "destination": entry["destination"],
            "aircraft_id": entry["aircraft_id"], "crew_id": entry["crew_id"], "old_std": iso(entry["old_std"]), "old_sta": iso(entry["old_sta"]),
            "new_std": iso(entry["new_std"]), "new_sta": iso(entry["new_sta"]), "delay_minutes": delay,
            "action": entry["action"], "revision": entry["revision"]}


def compute_rolling(conn: sqlite3.Connection, flight_id: int, *, action: str,
                    new_std: datetime | None = None, new_sta: datetime | None = None,
                    aircraft_id: str | None = None, crew_id: str | None = None, reason: str = "") -> dict[str, Any]:
    """以 flight_id 为起点沿同机后续航段滚动,返回 {adjustment, segments, conflicts, ok}。

    action 为 delay 时目标航班按新时刻执行(可换机/换机组),后续航段起飞不得早于前一班落地,
    需要则顺延,已取消的保留取消;action 为 cancel 时只取消目标航班,后续航段保持原时刻。
    """
    flight = conn.execute("SELECT * FROM flights WHERE id=?", (flight_id,)).fetchone()
    if not flight:
        raise ApiError(404, "flight_not_found", "航班不存在")
    if flight["status"] == "canceled":
        raise ApiError(409, "canceled_flight", "已取消航班不能作为滚动起点,请先恢复")
    target = _entry(flight)
    target["aircraft_id"] = aircraft_id or target["aircraft_id"]
    target["crew_id"] = crew_id or target["crew_id"]
    if action == "delay":
        if new_std is None or new_sta is None:
            raise ApiError(400, "invalid_times", "延误调整必须提供新的起飞与到达时间")
        if new_sta <= new_std:
            raise ApiError(400, "invalid_times", "新到达时间必须晚于新起飞时间")
        target["new_std"], target["new_sta"] = new_std, new_sta
    target["action"] = "adjusted" if action == "delay" else "canceled"

    # 同一架飞机的全部航段(含已取消),按原计划时刻排序;延误只向后顺延,不重排轮转顺序,
    # 换机时目标航班按新起飞时刻插入新飞机的航段序列
    swapped = target["aircraft_id"] != flight["aircraft_id"]
    target_key = target["new_std"] if swapped else target["old_std"]
    rows = conn.execute("SELECT * FROM flights WHERE aircraft_id=? AND id!=? ORDER BY std,id", (target["aircraft_id"], flight_id)).fetchall()
    chain = [_entry(row) for row in rows]
    chain.append(target)
    chain.sort(key=lambda entry: (target_key if entry is target else entry["old_std"], entry["flight_id"]))
    pos = next(i for i, entry in enumerate(chain) if entry["flight_id"] == flight_id)

    conflicts: list[dict[str, Any]] = []
    if action == "delay":
        previous = next((entry for entry in reversed(chain[:pos]) if entry["status"] != "canceled"), None)
        if previous and previous["new_sta"] > target["new_std"]:
            conflicts.append(_conflict(target["flight_no"], AIRCRAFT_OVERLAP,
                                       f"航班 {target['flight_no']} 起飞 {iso(target['new_std'])} 早于同机前一航段 {previous['flight_no']} 落地 {iso(previous['new_sta'])}",
                                       resource=target["aircraft_id"], other_flight_no=previous["flight_no"]))
        conflicts += check_segment(conn, flight_no=target["flight_no"], origin=target["origin"], destination=target["destination"],
                                   aircraft_id=target["aircraft_id"], crew_id=target["crew_id"], std=target["new_std"], sta=target["new_sta"])
        previous_sta = target["new_sta"]
        for entry in chain[pos + 1:]:
            if entry["status"] == "canceled":
                entry["action"] = "kept_canceled"
                continue
            if entry["new_std"] < previous_sta:
                entry["new_std"] = previous_sta
                entry["new_sta"] = previous_sta + entry["duration"]
                entry["action"] = "shifted"
            else:
                entry["action"] = "kept"
            conflicts += check_segment(conn, flight_no=entry["flight_no"], origin=entry["origin"], destination=entry["destination"],
                                       aircraft_id=entry["aircraft_id"], crew_id=entry["crew_id"], std=entry["new_std"], sta=entry["new_sta"])
            previous_sta = entry["new_sta"]
    else:
        for entry in chain[pos + 1:]:
            entry["action"] = "kept_canceled" if entry["status"] == "canceled" else "kept"

    adjustment = {"flight_id": target["flight_id"], "flight_no": target["flight_no"], "action": action,
                  "aircraft_id": target["aircraft_id"], "crew_id": target["crew_id"], "reason": reason,
                  "old_std": iso(target["old_std"]), "old_sta": iso(target["old_sta"]),
                  "new_std": iso(target["new_std"]), "new_sta": iso(target["new_sta"])}
    segments = [_segment_view(entry) for entry in chain[pos:]]
    return {"adjustment": adjustment, "segments": segments, "conflicts": conflicts, "ok": not conflicts}
