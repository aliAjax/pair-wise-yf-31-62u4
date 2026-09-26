import sys, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import AirlineRecoveryService, ApiError, iso

BASE = datetime(2026, 9, 27, 8, 0, tzinfo=timezone.utc)


class RollingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.svc = AirlineRecoveryService(Path(self.tmp.name) / "test.db")
        self.svc.seed_airport("ops", "ops_manager", {"code": "AAA", "country": "CN", "curfew_start": "23:00", "curfew_end": "05:00"})
        self.svc.seed_airport("ops", "ops_manager", {"code": "BBB", "country": "CN", "curfew_start": "23:00", "curfew_end": "05:00"})
        self.svc.seed_aircraft("ops", "ops_manager", {"id": "AC1", "model": "A320", "maintenance_due": iso(BASE + timedelta(days=5))})
        self.svc.seed_aircraft("ops", "ops_manager", {"id": "AC2", "model": "A320", "maintenance_due": iso(BASE + timedelta(days=5))})
        self.svc.seed_crew("ops", "ops_manager", {"id": "CR1", "name": "甲组", "base": "AAA", "duty_start": iso(BASE - timedelta(hours=2)), "max_duty_minutes": 720})
        self.svc.seed_crew("ops", "ops_manager", {"id": "CR2", "name": "乙组", "base": "AAA", "duty_start": iso(BASE - timedelta(hours=2)), "max_duty_minutes": 720})
        self.svc.create_permit("ops", "ops_manager", {"origin": "AAA", "destination": "BBB", "valid_from": iso(BASE - timedelta(days=1)), "valid_to": iso(BASE + timedelta(days=2))})
        self.svc.create_permit("ops", "ops_manager", {"origin": "BBB", "destination": "AAA", "valid_from": iso(BASE - timedelta(days=1)), "valid_to": iso(BASE + timedelta(days=2))})
        self.f1 = self.make_flight("RL100", "AAA", "BBB", 0, "AC1", "CR1")
        self.f2 = self.make_flight("RL101", "BBB", "AAA", 3, "AC1", "CR1")
        self.f3 = self.make_flight("RL102", "AAA", "BBB", 6, "AC1", "CR1")
        self.other = self.make_flight("RL200", "AAA", "BBB", 1, "AC2", "CR2")

    def tearDown(self): self.tmp.cleanup()

    def make_flight(self, number, origin, destination, offset_hours, aircraft, crew):
        return self.svc.create_flight("sched", "scheduler", {"flight_no": number, "origin": origin, "destination": destination,
            "std": iso(BASE + timedelta(hours=offset_hours)), "sta": iso(BASE + timedelta(hours=offset_hours + 2)),
            "aircraft_id": aircraft, "crew_id": crew, "passenger_count": 100})

    def flight(self, flight_id):
        return dict(self.svc.repo.conn.execute("SELECT * FROM flights WHERE id=?", (flight_id,)).fetchone())

    def delay_f1(self):
        return self.svc.preview_rolling("sched", "scheduler", {"flight_id": self.f1["id"], "action": "delay",
            "new_std": iso(BASE + timedelta(hours=4)), "new_sta": iso(BASE + timedelta(hours=6))})

    def test_delay_rolls_downstream_and_confirm_applies(self):
        preview = self.delay_f1()
        self.assertTrue(preview["result"]["ok"], preview["result"]["conflicts"])
        actions = {s["flight_no"]: s for s in preview["result"]["segments"]}
        self.assertEqual(actions["RL100"]["action"], "adjusted")
        self.assertEqual(actions["RL101"]["action"], "shifted")
        self.assertEqual(actions["RL101"]["new_std"], iso(BASE + timedelta(hours=6)))  # 前一班落地才能起飞
        self.assertEqual(actions["RL102"]["action"], "shifted")
        self.assertEqual(actions["RL102"]["new_std"], iso(BASE + timedelta(hours=8)))
        outcome = self.svc.confirm_rolling(preview["record"]["id"], "sched", "scheduler")
        self.assertFalse(outcome["idempotent"])
        self.assertEqual(self.flight(self.f2["id"])["std"], iso(BASE + timedelta(hours=6)))
        self.assertEqual(self.flight(self.f3["id"])["sta"], iso(BASE + timedelta(hours=10)))
        self.assertEqual(self.flight(self.other["id"])["std"], iso(BASE + timedelta(hours=1)))  # 其他航班保持原样
        again = self.svc.confirm_rolling(preview["record"]["id"], "sched", "scheduler")
        self.assertTrue(again["idempotent"])
        records = self.svc.list_rolling()["records"]
        self.assertEqual(records[0]["status"], "confirmed")
        self.assertEqual(records[0]["confirmed_by"], "sched")

    def test_conflict_reports_flight_and_constraint_and_blocks_confirm(self):
        self.svc.seed_aircraft("ops", "ops_manager", {"id": "AC1", "model": "A320", "maintenance_due": iso(BASE + timedelta(hours=9, minutes=30))})
        preview = self.delay_f1()
        self.assertFalse(preview["result"]["ok"])
        conflict = preview["result"]["conflicts"][0]
        self.assertEqual(conflict["flight_no"], "RL102")
        self.assertEqual(conflict["constraint"], "maintenance_due")
        with self.assertRaises(ApiError) as ctx:
            self.svc.confirm_rolling(preview["record"]["id"], "sched", "scheduler")
        self.assertEqual(ctx.exception.code, "rolling_has_conflicts")
        self.assertEqual(self.flight(self.f3["id"])["std"], iso(BASE + timedelta(hours=6)))  # 未确认前执行方案不变

    def test_canceled_segment_kept_canceled(self):
        self.svc.cancel_flight(self.f2["id"], "sched", "scheduler", {"reason": "机务检查"})
        preview = self.delay_f1()
        actions = {s["flight_no"]: s["action"] for s in preview["result"]["segments"]}
        self.assertEqual(actions["RL101"], "kept_canceled")
        self.assertEqual(actions["RL102"], "kept")
        self.svc.confirm_rolling(preview["record"]["id"], "sched", "scheduler")
        self.assertEqual(self.flight(self.f2["id"])["status"], "canceled")
        self.assertEqual(self.flight(self.f3["id"])["std"], iso(BASE + timedelta(hours=6)))

    def test_stale_preview_rejected(self):
        preview = self.delay_f1()
        self.svc.cancel_flight(self.f2["id"], "sched", "scheduler", {"reason": "机务检查"})
        with self.assertRaises(ApiError) as ctx:
            self.svc.confirm_rolling(preview["record"]["id"], "sched", "scheduler")
        self.assertEqual(ctx.exception.code, "stale_preview")
        self.assertEqual(ctx.exception.details[0]["flight_no"], "RL101")

    def test_cancel_action_keeps_downstream_unchanged(self):
        preview = self.svc.preview_rolling("sched", "scheduler", {"flight_id": self.f1["id"], "action": "cancel", "reason": "机械故障"})
        actions = {s["flight_no"]: s["action"] for s in preview["result"]["segments"]}
        self.assertEqual(actions, {"RL100": "canceled", "RL101": "kept", "RL102": "kept"})
        self.svc.confirm_rolling(preview["record"]["id"], "sched", "scheduler")
        canceled = self.flight(self.f1["id"])
        self.assertEqual(canceled["status"], "canceled")
        self.assertEqual(canceled["cancel_reason"], "机械故障")
        self.assertEqual(self.flight(self.f2["id"])["std"], iso(BASE + timedelta(hours=3)))

    def test_viewer_cannot_preview(self):
        with self.assertRaises(ApiError) as ctx:
            self.svc.preview_rolling("guest", "viewer", {"flight_id": self.f1["id"], "action": "delay",
                "new_std": iso(BASE + timedelta(hours=4)), "new_sta": iso(BASE + timedelta(hours=6))})
        self.assertEqual(ctx.exception.status, 403)


if __name__ == "__main__": unittest.main()
