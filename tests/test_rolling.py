import sys, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import AirlineRecoveryService, ApiError
import rolling

UTC = timezone.utc
BASE = datetime(2026, 10, 1, 6, 0, tzinfo=UTC)  # morning UTC, outside the 23:00-05:00 curfew


def iso(dt):
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


class RollingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.svc = AirlineRecoveryService(Path(self.tmp.name) / "rolling.db")
        # AAA/BBB curfew 23:00-05:00; CCC has no night window (curfew_start<end means 00:00-00:00 never inside)
        self.svc.seed_airport("ops", "ops_manager", {"code": "AAA", "country": "CN", "curfew_start": "23:00", "curfew_end": "05:00"})
        self.svc.seed_airport("ops", "ops_manager", {"code": "BBB", "country": "CN", "curfew_start": "23:00", "curfew_end": "05:00"})
        self.svc.seed_airport("ops", "ops_manager", {"code": "CCC", "country": "CN", "curfew_start": "00:00", "curfew_end": "00:00"})
        self.svc.seed_aircraft("ops", "ops_manager", {"id": "AC1", "model": "A320", "maintenance_due": iso(BASE + timedelta(days=5))})
        self.svc.seed_aircraft("ops", "ops_manager", {"id": "AC2", "model": "A320", "maintenance_due": iso(BASE + timedelta(days=5))})
        # CR1 12h duty from 04:00 -> limit 16:00; CR2 loose duty
        self.svc.seed_crew("ops", "ops_manager", {"id": "CR1", "name": "甲组", "base": "AAA", "duty_start": iso(BASE - timedelta(hours=2)), "max_duty_minutes": 720})
        self.svc.seed_crew("ops", "ops_manager", {"id": "CR2", "name": "乙组", "base": "AAA", "duty_start": iso(BASE - timedelta(hours=2)), "max_duty_minutes": 2400})
        self.svc.create_permit("ops", "ops_manager", {"origin": "AAA", "destination": "BBB", "valid_from": iso(BASE - timedelta(days=1)), "valid_to": iso(BASE + timedelta(days=2))})
        self.svc.create_permit("ops", "ops_manager", {"origin": "BBB", "destination": "AAA", "valid_from": iso(BASE - timedelta(days=1)), "valid_to": iso(BASE + timedelta(days=2))})
        # Single aircraft AC1 doing a closed AAA-BBB-AAA-BBB chain, 2h legs with 30m ground.
        self.f1 = self.svc.create_flight("sched", "scheduler", {"flight_no": "CA001", "origin": "AAA", "destination": "BBB", "std": iso(BASE), "sta": iso(BASE + timedelta(hours=2)), "aircraft_id": "AC1", "crew_id": "CR1", "passenger_count": 120})
        self.f2 = self.svc.create_flight("sched", "scheduler", {"flight_no": "CA002", "origin": "BBB", "destination": "AAA", "std": iso(BASE + timedelta(hours=2, minutes=30)), "sta": iso(BASE + timedelta(hours=4, minutes=30)), "aircraft_id": "AC1", "crew_id": "CR1", "passenger_count": 120})
        self.f3 = self.svc.create_flight("sched", "scheduler", {"flight_no": "CA003", "origin": "AAA", "destination": "BBB", "std": iso(BASE + timedelta(hours=5)), "sta": iso(BASE + timedelta(hours=7)), "aircraft_id": "AC1", "crew_id": "CR1", "passenger_count": 120})
        self.f4 = self.svc.create_flight("sched", "scheduler", {"flight_no": "CA004", "origin": "BBB", "destination": "AAA", "std": iso(BASE + timedelta(hours=7, minutes=30)), "sta": iso(BASE + timedelta(hours=9, minutes=30)), "aircraft_id": "AC1", "crew_id": "CR1", "passenger_count": 120})

    def tearDown(self):
        self.tmp.cleanup()

    def disrupt(self, kind="aircraft_fault", resource="AC1"):
        return self.svc.create_disruption("sched", "scheduler", {"kind": kind, "resource_id": resource,
                                   "starts_at": iso(BASE - timedelta(hours=1)), "ends_at": iso(BASE + timedelta(hours=3))})

    def legs_by_no(self, result):
        return {leg["flight_no"]: leg for leg in result["legs"]}

    def test_rolling_shift_propagates_to_every_downstream_leg(self):
        result = self.svc.preview_rolling("sched", "scheduler", {
            "flight_id": self.f1["id"], "action": "shift",
            "new_std": iso(BASE + timedelta(hours=1)), "new_sta": iso(BASE + timedelta(hours=3))})
        legs = self.legs_by_no(result)
        self.assertTrue(result["feasible"], result["conflicts"])
        # CA001 +60 overtakes CA002's original 08:30 slot; slack (30m/leg) is consumed.
        self.assertEqual(legs["CA001"]["delay_minutes"], 60)
        self.assertEqual(legs["CA002"]["new_std"], iso(BASE.replace(hour=9)))
        self.assertEqual(legs["CA002"]["new_sta"], iso(BASE.replace(hour=11)))
        self.assertEqual(legs["CA003"]["new_std"], iso(BASE.replace(hour=11)))
        self.assertEqual(legs["CA004"]["new_std"], iso(BASE.replace(hour=13, minute=30)))
        self.assertEqual(legs["CA003"]["delay_minutes"], 0)
        self.assertEqual(legs["CA004"]["delay_minutes"], 0)
        self.assertEqual(result["summary"]["total_delay_minutes"], 90)
        self.assertEqual(result["summary"]["shifted_count"], 2)
        self.assertEqual(legs["CA003"]["action"], "unchanged")
        self.assertEqual(legs["CA004"]["action"], "unchanged")

    def test_no_change_when_previous_lands_before_original_std(self):
        result = self.svc.preview_rolling("sched", "scheduler", {
            "flight_id": self.f1["id"], "action": "shift",
            "new_std": iso(BASE + timedelta(minutes=10)), "new_sta": iso(BASE + timedelta(hours=2, minutes=10))})
        legs = self.legs_by_no(result)
        # CA001 lands 08:10 <= CA002 original std 08:30, so the rest keep their times.
        self.assertTrue(result["feasible"])
        self.assertEqual(legs["CA002"]["action"], "unchanged")
        self.assertEqual(legs["CA004"]["action"], "unchanged")
        self.assertEqual(result["summary"]["shifted_count"], 1)

    def test_curfew_conflict_reports_flight_number_and_constraint(self):
        # Push CA001 by 16h: CA003/CA004 land inside the 23:00-05:00 curfew.
        result = self.svc.preview_rolling("sched", "scheduler", {
            "flight_id": self.f1["id"], "action": "shift",
            "new_std": iso(BASE + timedelta(hours=16)), "new_sta": iso(BASE + timedelta(hours=18))})
        self.assertFalse(result["feasible"])
        curfew = [c for c in result["conflicts"] if c["code"] == "airport_curfew"]
        self.assertTrue(curfew)
        self.assertTrue(any("宵禁" in c["message"] for c in curfew), curfew)
        for c in result["conflicts"]:
            self.assertIn("flight_no", c)

    def test_maintenance_due_conflict_blocks_confirm_and_writes_nothing(self):
        self.svc.seed_aircraft("ops", "ops_manager", {"id": "AC1", "model": "A320", "maintenance_due": iso(BASE + timedelta(hours=10))})
        disruption = self.disrupt()
        with self.assertRaises(ApiError) as ctx:
            self.svc.confirm_rolling("sched", "scheduler", {
                "flight_id": self.f1["id"], "action": "shift",
                "new_std": iso(BASE + timedelta(hours=10)), "new_sta": iso(BASE + timedelta(hours=12)),
                "disruption_id": disruption["id"], "name": "撞维护"})
        self.assertEqual(ctx.exception.code, "rolling_conflict")
        codes = {c["code"] for c in ctx.exception.details["conflicts"]}
        self.assertIn("maintenance_due", codes)
        # Nothing persisted: no plan, no assignment, no record; flights untouched.
        state = self.svc.state()
        self.assertEqual(state["plans"], [])
        self.assertEqual(self.svc.list_rolling_records("sched", "scheduler")["records"], [])
        f1 = next(f for f in state["flights"] if f["id"] == self.f1["id"])
        self.assertEqual(f1["std"], iso(BASE))

    def test_route_permit_missing_on_a_downstream_leg(self):
        # Cancel CA003/CA004 and tail the AC1 chain with AAA->CCC (no CCC permit):
        # CA001(AAA->BBB), CA002(BBB->AAA), CA013(AAA->CCC, 10:30-12:30).
        self.svc.cancel_flight(self.f3["id"], "sched", "scheduler", {"reason": "改航 CCC"})
        self.svc.cancel_flight(self.f4["id"], "sched", "scheduler", {"reason": "改航 CCC"})
        self.svc.create_flight("sched", "scheduler", {"flight_no": "CA013", "origin": "AAA", "destination": "CCC", "std": iso(BASE + timedelta(hours=4, minutes=30)), "sta": iso(BASE + timedelta(hours=6, minutes=30)), "aircraft_id": "AC1", "crew_id": "CR2", "passenger_count": 10})
        result = self.svc.preview_rolling("sched", "scheduler", {
            "flight_id": self.f1["id"], "action": "shift",
            "new_std": iso(BASE + timedelta(minutes=30)), "new_sta": iso(BASE + timedelta(hours=2, minutes=30))})
        permit_conflicts = [c for c in result["conflicts"] if c["code"] == "route_permit_missing"]
        self.assertTrue(any(c["flight_no"] == "CA013" for c in permit_conflicts), result["conflicts"])

    def test_previous_leg_airborne_cannot_keep_original_departure(self):
        # Swap CA001 onto AC2, which already flies CA900 landing at 08:00 -> 06:00 departure invalid.
        self.svc.create_flight("sched", "scheduler", {"flight_no": "CA900", "origin": "AAA", "destination": "BBB", "std": iso(BASE - timedelta(hours=2)), "sta": iso(BASE + timedelta(hours=2)), "aircraft_id": "AC2", "crew_id": "CR2"})
        result = self.svc.preview_rolling("sched", "scheduler", {
            "flight_id": self.f1["id"], "action": "shift",
            "new_std": iso(BASE), "new_sta": iso(BASE + timedelta(hours=2)), "aircraft_id": "AC2", "crew_id": "CR2"})
        airborne = [c for c in result["conflicts"] if c["code"] == "previous_leg_airborne"]
        self.assertEqual(len(airborne), 1)
        self.assertEqual(airborne[0]["flight_no"], "CA001")
        self.assertIn("CA900", airborne[0]["message"])
        self.assertIn("尚未落地", airborne[0]["message"])

    def test_position_conflict_when_swap_plane_is_at_other_airport(self):
        # AC2 last landed at CCC at 05:00; CA001 departs AAA at 06:00 -> position conflict.
        self.svc.create_permit("ops", "ops_manager", {"origin": "AAA", "destination": "CCC", "valid_from": iso(BASE - timedelta(days=1)), "valid_to": iso(BASE + timedelta(days=2))})
        self.svc.create_flight("sched", "scheduler", {"flight_no": "CA901", "origin": "AAA", "destination": "CCC", "std": iso(BASE - timedelta(hours=3)), "sta": iso(BASE - timedelta(hours=1)), "aircraft_id": "AC2", "crew_id": "CR2"})
        result = self.svc.preview_rolling("sched", "scheduler", {
            "flight_id": self.f1["id"], "action": "shift",
            "new_std": iso(BASE), "new_sta": iso(BASE + timedelta(hours=2)), "aircraft_id": "AC2", "crew_id": "CR2"})
        pos = [c for c in result["conflicts"] if c["code"] == "aircraft_position"]
        self.assertTrue(any(c["flight_no"] == "CA001" and "CA901" in c["message"] for c in pos), result["conflicts"])

    def test_cancel_keeps_cancellation_and_stops_chain(self):
        self.svc.cancel_flight(self.f2["id"], "sched", "scheduler", {"reason": "机械故障"})
        result = self.svc.preview_rolling("sched", "scheduler", {"flight_id": self.f1["id"], "action": "cancel"})
        legs = self.legs_by_no(result)
        self.assertEqual(legs["CA001"]["action"], "cancel")
        self.assertTrue(result["feasible"])
        # Chain stops at the trigger: downstream legs are not even listed as changed.
        self.assertEqual([leg["flight_no"] for leg in result["legs"]], ["CA001"])

    def test_existing_canceled_downstream_leg_keeps_cancel_then_chain_breaks(self):
        self.svc.cancel_flight(self.f2["id"], "sched", "scheduler", {"reason": "机务停场"})
        result = self.svc.preview_rolling("sched", "scheduler", {
            "flight_id": self.f1["id"], "action": "shift",
            "new_std": iso(BASE + timedelta(hours=2)), "new_sta": iso(BASE + timedelta(hours=4))})
        legs = self.legs_by_no(result)
        self.assertEqual(legs["CA002"]["action"], "cancel")
        # CA003/CA004 stay as-is and are shown as the chain boundary.
        self.assertEqual(legs["CA003"]["action"], "unchanged")
        self.assertEqual(legs["CA004"]["action"], "unchanged")
        self.assertEqual(legs["CA003"]["new_std"], legs["CA003"]["old_std"])

    def test_confirm_writes_only_affected_legs_into_new_plan_and_locks(self):
        disruption = self.disrupt()
        out = self.svc.confirm_rolling("sched", "scheduler", {
            "flight_id": self.f1["id"], "action": "shift",
            "new_std": iso(BASE + timedelta(minutes=10)), "new_sta": iso(BASE + timedelta(hours=2, minutes=10)),
            "disruption_id": disruption["id"], "name": "延误10分钟"})
        self.assertTrue(out["created_plan"])
        plan = out["plan"]
        self.assertEqual({a["flight_no"] for a in plan["assignments"]}, {"CA001"})
        assignment = plan["assignments"][0]
        self.assertEqual(assignment["new_std"], iso(BASE + timedelta(minutes=10)))
        self.assertEqual(plan["revision"], 2)
        locked = self.svc.lock_plan(plan["id"], "ops", "ops_manager", {"expected_revision": 2})
        self.assertEqual(locked["status"], "locked")
        state = self.svc.state()
        f1 = next(f for f in state["flights"] if f["id"] == self.f1["id"])
        f2 = next(f for f in state["flights"] if f["id"] == self.f2["id"])
        self.assertEqual(f1["std"], iso(BASE + timedelta(minutes=10)))
        self.assertEqual(f2["std"], iso(BASE + timedelta(hours=2, minutes=30)))  # untouched
        records = self.svc.list_rolling_records("sched", "scheduler")["records"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["trigger_flight_no"], "CA001")
        self.assertEqual(records[0]["shifted_count"], 1)

    def test_confirm_appends_to_existing_draft_with_revision_check(self):
        disruption = self.disrupt()
        plan = self.svc.create_plan("sched", "scheduler", {"disruption_id": disruption["id"], "name": "草案", "assignments": []})
        with self.assertRaises(ApiError) as ctx:
            self.svc.confirm_rolling("sched", "scheduler", {
                "flight_id": self.f1["id"], "action": "shift",
                "new_std": iso(BASE + timedelta(minutes=10)), "new_sta": iso(BASE + timedelta(hours=2, minutes=10)),
                "plan_id": plan["id"], "expected_revision": 99})
        self.assertEqual(ctx.exception.code, "revision_conflict")
        out = self.svc.confirm_rolling("sched", "scheduler", {
            "flight_id": self.f1["id"], "action": "shift",
            "new_std": iso(BASE + timedelta(minutes=10)), "new_sta": iso(BASE + timedelta(hours=2, minutes=10)),
            "plan_id": plan["id"], "expected_revision": 1})
        self.assertFalse(out["created_plan"])
        self.assertEqual(out["plan"]["revision"], 2)

    def test_confirm_canceled_chain_marks_flight_canceled_on_lock(self):
        self.svc.cancel_flight(self.f2["id"], "sched", "scheduler", {"reason": "机务停场"})
        disruption = self.disrupt()
        out = self.svc.confirm_rolling("sched", "scheduler", {
            "flight_id": self.f1["id"], "action": "cancel", "disruption_id": disruption["id"]})
        self.assertEqual({a["status"] for a in out["plan"]["assignments"]}, {"canceled"})
        locked = self.svc.lock_plan(out["plan"]["id"], "ops", "ops_manager", {"expected_revision": 2})
        self.assertEqual(locked["status"], "locked")
        state = self.svc.state()
        f1 = next(f for f in state["flights"] if f["id"] == self.f1["id"])
        f3 = next(f for f in state["flights"] if f["id"] == self.f3["id"])
        self.assertEqual(f1["status"], "canceled")
        self.assertEqual(f3["status"], "scheduled")  # chain broke at CA001

    def test_permissions(self):
        with self.assertRaises(ApiError) as ctx:
            self.svc.preview_rolling("v", "viewer", {"flight_id": self.f1["id"], "action": "cancel"})
        self.assertEqual(ctx.exception.status, 403)
        with self.assertRaises(ApiError) as ctx:
            self.svc.confirm_rolling("a", "auditor", {"flight_id": self.f1["id"], "action": "cancel", "disruption_id": 1})
        self.assertEqual(ctx.exception.status, 403)

    def test_engine_is_pure_and_unit_callable(self):
        snapshot = {
            "flights": [{"id": 1, "flight_no": "X1", "origin": "AAA", "destination": "BBB", "std": iso(BASE), "sta": iso(BASE + timedelta(hours=2)), "aircraft_id": "AC1", "crew_id": "CR1", "status": "scheduled"}],
            "aircraft": {"AC1": {"id": "AC1", "status": "active", "maintenance_due": iso(BASE + timedelta(days=5))}},
            "crew": {"CR1": {"id": "CR1", "status": "active", "duty_start": iso(BASE - timedelta(hours=2)), "max_duty_minutes": 720}},
            "airports": {"AAA": {"code": "AAA", "curfew_start": "23:00", "curfew_end": "05:00"},
                         "BBB": {"code": "BBB", "curfew_start": "23:00", "curfew_end": "05:00"}},
            "permits": [{"origin": "AAA", "destination": "BBB", "valid_from": iso(BASE - timedelta(days=1)), "valid_to": iso(BASE + timedelta(days=2)), "curfew_exempt": 0}],
        }
        result = rolling.propagate(trigger_flight_id=1, action="shift",
                                   new_std=iso(BASE + timedelta(minutes=5)), new_sta=iso(BASE + timedelta(hours=2, minutes=5)), **snapshot)
        self.assertTrue(result["feasible"])
        self.assertEqual(result["legs"][0]["delay_minutes"], 5)


if __name__ == "__main__":
    unittest.main()
