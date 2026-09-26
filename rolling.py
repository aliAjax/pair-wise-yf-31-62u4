"""Rolling recovery propagation engine.

Pure calculation module: given a snapshot of flights/resources and one
dispatcher adjustment, it walks the *downstream legs of the same aircraft* in
time order. Each downstream leg is either shifted so it cannot depart before
the previous leg lands, or its existing cancellation is kept (which breaks the
chain). Every leg is re-checked against maintenance, crew duty, airport
curfews, route permits, fleet position and the previous leg's landing time.

The module never writes anything: callers receive a preview with explicit,
flight-number-bearing conflict messages and decide whether to persist it.
Times are naive ISO-8601 UTC strings, matching the rest of the prototype.
"""
from __future__ import annotations

from datetime import datetime, time, timezone
from typing import Any

_UTC = timezone.utc


class ApiError(Exception):
    """Shared API error so app.py and adjustments.py compare one class even
    when app.py is executed as ``__main__`` (which loads a second copy)."""

    def __init__(self, status: int, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.status, self.code, self.message, self.details = status, code, message, details


def utcnow() -> datetime:
    return datetime.now(_UTC)


def iso(value: datetime | None = None) -> str:
    return (value or utcnow()).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_time(value: str | None) -> datetime:
    if not value:
        raise ApiError(400, "time_required", "必须提供 ISO 8601 时间")
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApiError(400, "invalid_time", f"时间格式错误: {value}") from exc
    return parsed.astimezone(_UTC) if parsed.tzinfo is None else parsed.astimezone(_UTC)


def parse_clock(value: str) -> time:
    try:
        return time.fromisoformat(str(value))
    except ValueError as exc:
        raise ApiError(400, "invalid_clock", f"时刻格式应为 HH:MM: {value}") from exc


def parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.astimezone(_UTC) if parsed.tzinfo is None else parsed.astimezone(_UTC)


def format_iso(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_clock(value: str) -> time:
    return time.fromisoformat(value)


def in_curfew(clock: time, curfew_start: time, curfew_end: time) -> bool:
    """Overnight window e.g. 23:00-05:00 wraps past midnight."""
    if curfew_start > curfew_end:
        return clock >= curfew_start or clock < curfew_end
    return curfew_start <= clock < curfew_end


def _conflict(leg: dict[str, Any], code: str, constraint: str, message: str, **detail: Any) -> dict[str, Any]:
    item = {"code": code, "constraint": constraint,
            "flight_id": leg["flight_id"], "flight_no": leg["flight_no"], "message": message}
    if detail:
        item["detail"] = detail
    return item


def _route_permit(permits: list[dict[str, Any]], origin: str, destination: str, std: datetime) -> tuple[dict[str, Any] | None, str | None]:
    route = [p for p in permits if p["origin"] == origin and p["destination"] == destination]
    for permit in route:
        if parse_iso(permit["valid_from"]) <= std <= parse_iso(permit["valid_to"]):
            return permit, None
    if not route:
        return None, "route_permit_missing"
    return None, "route_permit_window"


def check_leg(leg: dict[str, Any], std: datetime, sta: datetime, aircraft_id: str, crew_id: str,
              aircraft: dict[str, dict[str, Any]], crew: dict[str, dict[str, Any]],
              airports: dict[str, dict[str, Any]], permits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """All constraint checks for one proposed leg. Returns conflict dicts."""
    problems: list[dict[str, Any]] = []
    no = leg["flight_no"]

    plane = aircraft.get(aircraft_id)
    if not plane or plane.get("status") != "active":
        problems.append(_conflict(leg, "resource_inactive", "resource", f"航班 {no} 调配的飞机 {aircraft_id} 不存在或不可用"))
    elif parse_iso(plane["maintenance_due"]) < sta:
        due = plane["maintenance_due"]
        problems.append(_conflict(leg, "maintenance_due", "maintenance",
                                  f"航班 {no} 预计落地 {format_iso(sta)}，晚于飞机 {aircraft_id} 的维护到期时刻 {due}，违反维护间隔",
                                  aircraft_id=aircraft_id, maintenance_due=due))

    team = crew.get(crew_id)
    if not team or team.get("status") != "active":
        problems.append(_conflict(leg, "resource_inactive", "resource", f"航班 {no} 排班的机组 {crew_id} 不存在或不可用"))
    else:
        duty_start = parse_iso(team["duty_start"])
        max_minutes = team["max_duty_minutes"]
        used = int((sta - duty_start).total_seconds() // 60)
        if used > max_minutes:
            problems.append(_conflict(leg, "duty_limit", "duty",
                                      f"航班 {no} 预计落地 {format_iso(sta)}，机组 {crew_id} 自 {team['duty_start']} 起已执勤 {used} 分钟，"
                                      f"超过 {max_minutes} 分钟执勤上限",
                                      crew_id=crew_id, used_minutes=used, max_minutes=max_minutes))

    origin, destination = airports.get(leg["origin"]), airports.get(leg["destination"])
    if not origin or not destination:
        problems.append(_conflict(leg, "airport_unknown", "resource",
                                  f"航班 {no} 的航线 {leg['origin']}->{leg['destination']} 缺少机场资料，无法核对宵禁"))
        return problems

    permit, missing_code = _route_permit(permits, leg["origin"], leg["destination"], std)
    exempt = bool(permit and permit.get("curfew_exempt"))
    if missing_code and leg["origin"] != leg["destination"]:
        if missing_code == "route_permit_missing":
            problems.append(_conflict(leg, "route_permit_missing", "permit",
                                      f"航班 {no} 执行 {leg['origin']}->{leg['destination']}，起飞时刻 {format_iso(std)} 没有有效航线许可"))
        else:
            problems.append(_conflict(leg, "route_permit_window", "permit",
                                      f"航班 {no} 的 {leg['origin']}->{leg['destination']} 航线许可不覆盖起飞时刻 {format_iso(std)}"))

    if not exempt:
        start_clock, end_clock = parse_clock(origin["curfew_start"]), parse_clock(origin["curfew_end"])
        if in_curfew(std.timetz().replace(tzinfo=None), start_clock, end_clock):
            problems.append(_conflict(leg, "airport_curfew", "curfew",
                                      f"航班 {no} 起飞时刻 {format_iso(std)} 落在起飞机场 {leg['origin']} 宵禁 {origin['curfew_start']}-{origin['curfew_end']} 内",
                                      airport=leg["origin"], phase="departure"))
        start_clock, end_clock = parse_clock(destination["curfew_start"]), parse_clock(destination["curfew_end"])
        if in_curfew(sta.timetz().replace(tzinfo=None), start_clock, end_clock):
            problems.append(_conflict(leg, "airport_curfew", "curfew",
                                      f"航班 {no} 落地时刻 {format_iso(sta)} 落在降落机场 {leg['destination']} 宵禁 {destination['curfew_start']}-{destination['curfew_end']} 内",
                                      airport=leg["destination"], phase="arrival"))
    return problems


def _make_leg(flight: dict[str, Any], action: str, std: datetime | None, sta: datetime | None,
              aircraft_id: str, crew_id: str, reason: str) -> dict[str, Any]:
    old_std, old_sta = parse_iso(flight["std"]), parse_iso(flight["sta"])
    delay = int((std - old_std).total_seconds() // 60) if std is not None else 0
    return {"flight_id": flight["id"], "flight_no": flight["flight_no"], "origin": flight["origin"],
            "destination": flight["destination"], "status": flight["status"],
            "old_std": flight["std"], "old_sta": flight["sta"],
            "new_std": format_iso(std) if std is not None else None,
            "new_sta": format_iso(sta) if sta is not None else None,
            "aircraft_id": aircraft_id, "crew_id": crew_id,
            "delay_minutes": delay, "action": action, "reason": reason, "conflicts": []}


def propagate(*, trigger_flight_id: int, flights: list[dict[str, Any]], aircraft: dict[str, dict[str, Any]],
              crew: dict[str, dict[str, Any]], airports: dict[str, dict[str, Any]], permits: list[dict[str, Any]],
              action: str = "shift", new_std: str | None = None, new_sta: str | None = None,
              aircraft_id: str | None = None, crew_id: str | None = None) -> dict[str, Any]:
    """Roll an adjustment along the downstream legs of the target aircraft.

    action="cancel" keeps the trigger cancellation and stops the chain;
    action="shift" requires new_std/new_sta and optionally swaps aircraft/crew.
    """
    if action not in {"shift", "cancel"}:
        raise ValueError("action 必须是 shift 或 cancel")
    trigger = next((f for f in flights if f["id"] == trigger_flight_id), None)
    if trigger is None:
        raise LookupError(f"航班不存在: id={trigger_flight_id}")
    if trigger["status"] == "canceled":
        raise LookupError(f"航班 {trigger['flight_no']} 已取消，请走恢复流程而不是滚动调整")

    target_aircraft = aircraft_id or trigger["aircraft_id"]
    target_crew = crew_id or trigger["crew_id"]
    legs: list[dict[str, Any]] = []
    all_conflicts: list[dict[str, Any]] = []

    def add(leg: dict[str, Any]) -> None:
        legs.append(leg)
        all_conflicts.extend(leg["conflicts"])

    if action == "cancel":
        canceled = _make_leg(trigger, "cancel", None, None, trigger["aircraft_id"], trigger["crew_id"],
                             "调度员取消该航段；取消保留，滚动链到此中断")
        add(canceled)
        return _result(trigger, action, target_aircraft, target_crew, legs, all_conflicts)

    if not new_std or not new_sta:
        raise ValueError("顺延调整必须提供 new_std 和 new_sta")
    trig_std, trig_sta = parse_iso(new_std), parse_iso(new_sta)
    if trig_sta <= trig_std:
        raise ValueError("新到达时间必须晚于新起飞时间")

    trig_leg = _make_leg(trigger, "shift", trig_std, trig_sta, target_aircraft, target_crew,
                         "调度员调整的首班航段（换机或延误）")

    # The aircraft must physically be at the trigger origin and free by new_std.
    # Only legs scheduled before the trigger are predecessors; later legs belong
    # to the downstream chain and get re-timed below instead of flagging overlap.
    trigger_std_original = parse_iso(trigger["std"])
    prior_done = None
    for other in flights:
        if other["id"] == trigger["id"] or other["status"] == "canceled" or other["aircraft_id"] != target_aircraft:
            continue
        if parse_iso(other["std"]) >= trigger_std_original:
            continue
        o_std, o_sta = parse_iso(other["std"]), parse_iso(other["sta"])
        if o_std < trig_sta and trig_std < o_sta:
            trig_leg["conflicts"].append(_conflict(
                trig_leg, "previous_leg_airborne", "turnaround",
                f"航班 {trigger['flight_no']} 新起飞时刻 {format_iso(trig_std)} 早于前序航班 {other['flight_no']} 的落地时刻 {other['sta']}："
                f"前一班尚未落地，{trigger['flight_no']} 不能沿用该起飞时刻",
                previous_flight_no=other["flight_no"], previous_sta=other["sta"]))
        if o_sta <= trig_std and (prior_done is None or o_sta > parse_iso(prior_done["sta"])):
            prior_done = other
    if prior_done and prior_done["destination"] != trigger["origin"]:
        trig_leg["conflicts"].append(_conflict(
            trig_leg, "aircraft_position", "position",
            f"航班 {trigger['flight_no']} 从 {trigger['origin']} 起飞，但飞机 {target_aircraft} 前序航班 {prior_done['flight_no']} "
            f"落地在 {prior_done['destination']}，飞机无法到位",
            aircraft_id=target_aircraft, expected=trigger["origin"], actual=prior_done["destination"]))
    trig_leg["conflicts"].extend(check_leg(trig_leg, trig_std, trig_sta, target_aircraft, target_crew,
                                           aircraft, crew, airports, permits))
    add(trig_leg)

    # Downstream legs of the same aircraft, in original chronological order.
    downstream = sorted(
        (f for f in flights
         if f["id"] != trigger["id"] and f["aircraft_id"] == target_aircraft
         and parse_iso(f["std"]) >= parse_iso(trigger["std"])),
        key=lambda f: parse_iso(f["std"]))
    prev_sta, prev_dest = trig_sta, trigger["destination"]
    chain_open = True
    for flight in downstream:
        if flight["status"] == "canceled":
            # Cancellation is kept and breaks propagation, but later legs still
            # have to be listed as "unchanged" so dispatchers see the boundary.
            kept = _make_leg(flight, "cancel", None, None, flight["aircraft_id"], flight["crew_id"],
                             "该航段已取消：保留取消，滚动链到此中断，其后航段保持原样")
            add(kept)
            chain_open = False
            continue
        if not chain_open:
            untouched = _make_leg(flight, "unchanged", parse_iso(flight["std"]), parse_iso(flight["sta"]),
                                  flight["aircraft_id"], flight["crew_id"], "滚动链已在前方取消航段中断，该航段保持原时刻")
            untouched["conflicts"] = []
            add(untouched)
            continue
        old_std, old_sta = parse_iso(flight["std"]), parse_iso(flight["sta"])
        proposed_std = max(old_std, prev_sta)
        proposed_sta = proposed_std + (old_sta - old_std)
        moved = proposed_std != old_std
        leg = _make_leg(flight, "shift" if moved else "unchanged", proposed_std, proposed_sta,
                        flight["aircraft_id"], flight["crew_id"],
                        f"随前序落地时刻顺延 {int((proposed_std - old_std).total_seconds() // 60)} 分钟" if moved
                        else "前序落地不晚于原起飞时刻，保持原时刻")
        if prev_dest != flight["origin"]:
            leg["conflicts"].append(_conflict(
                leg, "aircraft_position", "position",
                f"航班 {flight['flight_no']} 从 {flight['origin']} 起飞，但前序航段落地机场为 {prev_dest}，"
                f"飞机 {target_aircraft} 无法执行该航段",
                aircraft_id=target_aircraft, expected=flight["origin"], actual=prev_dest))
        leg["conflicts"].extend(check_leg(leg, proposed_std, proposed_sta, flight["aircraft_id"], flight["crew_id"],
                                          aircraft, crew, airports, permits))
        add(leg)
        prev_sta, prev_dest = proposed_sta, flight["destination"]

    return _result(trigger, action, target_aircraft, target_crew, legs, all_conflicts)


def _result(trigger: dict[str, Any], action: str, aircraft_id: str, crew_id: str,
            legs: list[dict[str, Any]], conflicts: list[dict[str, Any]]) -> dict[str, Any]:
    shifted = [leg for leg in legs if leg["action"] == "shift"]
    canceled = [leg for leg in legs if leg["action"] == "cancel"]
    unchanged = [leg for leg in legs if leg["action"] == "unchanged"]
    delays = [max(0, leg["delay_minutes"]) for leg in shifted]
    return {
        "trigger": {"flight_id": trigger["id"], "flight_no": trigger["flight_no"], "action": action,
                    "aircraft_id": aircraft_id, "crew_id": crew_id},
        "legs": legs,
        "conflicts": conflicts,
        "feasible": not conflicts,
        "summary": {"leg_count": len(legs), "shifted_count": len(shifted), "canceled_count": len(canceled),
                    "unchanged_count": len(unchanged),
                    "total_delay_minutes": sum(delays), "max_delay_minutes": max(delays, default=0)},
    }
