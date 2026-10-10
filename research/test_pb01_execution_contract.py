import copy
import json
import tempfile
import unittest
from pathlib import Path
from research.pb01_pipeline import sha256, metadata
from research.pb01_execution_contract import validate_execution_event, validate_cash_action


class ExecutionBoundaries(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        p = self.root / "synthetic.json"; p.write_text('{"synthetic":true}')
        self.ref = {"path": "synthetic.json", "sha256": sha256(p)}
        self.sessions = ["2026-09-30", "2026-10-08", "2026-10-09", "2026-10-13", "2026-10-15"]
        self.event = {"event_id": "fixture", "instrument_id": "fixture-stock", "book": "strategy", "side": "buy",
            "decision_at": "2026-09-30T15:05:00+08:00", "human_received_at": "2026-10-08T09:35:00+08:00",
            "human_submitted_at": "2026-10-08T09:35:10+08:00", "execution_at": "2026-10-08T09:35:12+08:00",
            "quote": {"observed_at": "2026-10-08T09:35:11+08:00", "bid": 10., "ask": 10.01, "bid_qty": 400, "ask_qty": 400, "source_ref": self.ref},
            "requested_qty": 200, "executed_qty": 100, "execution_price": 10.01, "outcome": "partial",
            "position_qty_before": 0, "last_buy_session": None, "evidence": [self.ref]}

    def validate(self, event):
        return validate_execution_event(self.root, event, sessions=self.sessions)

    def test_long_lived_task_keeps_bounded_metadata_envelope(self):
        task=self.root/'WORKFLOW_TASK.json'
        task.write_text(json.dumps({'evidence':'x'*273000}),encoding='utf-8')
        self.assertEqual(len(metadata(self.root,'WORKFLOW_TASK.json')['evidence']),273000)
        task.write_text(json.dumps({'evidence':'x'*1048576}),encoding='utf-8')
        with self.assertRaises(ValueError):metadata(self.root,'WORKFLOW_TASK.json')

    def test_larger_task_does_not_raise_other_metadata_budget(self):
        (self.root/'source.json').write_text(json.dumps({'x':'x'*273000}),encoding='utf-8')
        with self.assertRaises(ValueError):metadata(self.root,'source.json')

    def test_task_duplicate_keys_and_nonfinite_still_reject(self):
        task=self.root/'WORKFLOW_TASK.json'
        for payload in ('{"x":1,"x":2}','{"x":NaN}'):
            task.write_text(payload,encoding='utf-8')
            with self.assertRaises(ValueError):metadata(self.root,'WORKFLOW_TASK.json')

    def test_partial_entry_is_retained_but_never_becomes_real_ledger_or_order(self):
        r = self.validate(self.event)
        self.assertEqual(r["unfilled_qty"], 100)
        self.assertEqual(r["residual_position_qty"], 100)
        for flag in ("ledger_replayed", "economic_admitted", "production_eligible", "auto_orders"):
            self.assertFalse(r[flag])

    def test_quote_stale_future_size_and_touch_do_not_create_fill(self):
        for mutation in ("stale", "future", "size", "crossed", "price", "noquote"):
            with self.subTest(mutation=mutation):
                e = copy.deepcopy(self.event)
                if mutation == "stale": e["quote"]["observed_at"] = "2026-10-08T09:33:00+08:00"
                elif mutation == "future": e["quote"]["observed_at"] = "2026-10-08T09:35:13+08:00"
                elif mutation == "size": e["quote"]["ask_qty"] = 0
                elif mutation == "crossed": e["quote"]["bid"] = 11
                elif mutation == "price": e["execution_price"] = 10
                else: e["quote"] = None
                with self.assertRaises(ValueError): self.validate(e)

    def test_unknown_fields_missing_evidence_fake_quantities_or_timestamps_reject(self):
        for key, value in (("executed_qty", True), ("requested_qty", 150), ("execution_price", float("nan")),
                           ("human_submitted_at", "2026-10-08T09:35:13+08:00"), ("evidence", []),
                           ("execution_at", "2026-10-08T09:35:12"), ("extra", "ignored")):
            with self.subTest(key=key):
                e = copy.deepcopy(self.event); e[key] = value
                with self.assertRaises(ValueError): self.validate(e)

    def test_daily_open_never_replaces_original_minute_window(self):
        e = copy.deepcopy(self.event); e["execution_at"] = "2026-10-08T09:30:00+08:00"
        with self.assertRaises(ValueError): self.validate(e)

    def test_no_fill_cannot_have_execution_price(self):
        e = copy.deepcopy(self.event); e.update(executed_qty=0, outcome="no_fill")
        with self.assertRaises(ValueError): self.validate(e)
        e["execution_price"] = None
        r = self.validate(e); self.assertEqual(r["unfilled_qty"], 200); self.assertEqual(r["residual_position_qty"], 0)

    def sell(self):
        e = copy.deepcopy(self.event)
        e.update(side="sell", requested_qty=200, executed_qty=100, position_qty_before=300, last_buy_session="2026-10-08",
                 human_received_at="2026-10-09T14:55:00+08:00", human_submitted_at="2026-10-09T14:55:10+08:00",
                 execution_at="2026-10-09T14:55:12+08:00", execution_price=10.)
        e["quote"]["observed_at"] = "2026-10-09T14:55:11+08:00"
        return e

    def test_partial_exit_preserves_inventory_and_T1_blocks_same_day_sell(self):
        e = self.sell(); self.assertEqual(self.validate(e)["residual_position_qty"], 200)
        e["last_buy_session"] = "2026-10-09"
        with self.assertRaises(ValueError): self.validate(e)

    def test_exit_cannot_sell_more_than_position(self):
        e = self.sell(); e["position_qty_before"] = 0
        with self.assertRaises(ValueError): self.validate(e)

    def test_exit_boundary_retains_microseconds_and_is_strictly_before_1457(self):
        e = self.sell()
        for at in ("2026-10-09T14:57:00+08:00", "2026-10-09T14:57:00.999999+08:00"):
            with self.subTest(at=at):
                e["execution_at"] = at; e["quote"]["observed_at"] = at
                with self.assertRaises(ValueError): self.validate(e)
        e["execution_at"] = "2026-10-09T14:56:59.999999+08:00"
        e["quote"]["observed_at"] = e["execution_at"]
        self.assertTrue(self.validate(e)["candidate_structure_valid"])

    def test_buy_must_follow_previous_completed_session_and_sell_request_cannot_exceed_inventory(self):
        for at in ("2026-10-07T15:05:00+08:00", "2026-09-30T14:59:59+08:00"):
            e = copy.deepcopy(self.event); e["decision_at"] = at
            with self.assertRaises(ValueError): self.validate(e)
        e = self.sell(); e["requested_qty"] = 400
        with self.assertRaises(ValueError): self.validate(e)

    def action(self):
        return {"action_id": "fixture-action", "instrument_id": "fixture-etf",
                "record_at": "2026-10-08T15:00:00+08:00", "ex_at": "2026-10-09T09:15:00+08:00",
                "payable_on": "2026-10-13", "announced_at": "2026-10-07T16:00:00+08:00",
                "cash_per_unit_CNY": "0.123", "quantity_unit": "fund_unit", "evidence": [self.ref]}

    def test_announced_pay_date_does_not_create_cash_or_credit(self):
        r = validate_cash_action(self.root, self.action(), held_quantity=100,
                                known_at="2026-10-15T15:00:00+08:00")
        self.assertEqual(r["declared_gross_entitlement_CNY"], "12.300")
        self.assertFalse(r["actual_credit_supplied"])
        self.assertFalse(r["cash_available_for_orders_verified"])

    def test_future_action_unknown_unit_and_binary_float_amount_reject(self):
        for key, value in (("announced_at", "2026-10-16T16:00:00+08:00"),
                           ("quantity_unit", "unknown_provider_volume"), ("cash_per_unit_CNY", .123)):
            with self.subTest(key=key):
                a = self.action(); a[key] = value
                with self.assertRaises(ValueError):
                    validate_cash_action(self.root, a, held_quantity=100, known_at="2026-10-15T15:00:00+08:00")

    def test_credit_identity_future_or_pre_payable_clock_requires_evidence(self):
        credit = {"action_id": "fixture-action", "instrument_id": "fixture-etf",
                  "credited_at": "2026-10-13T09:15:00+08:00", "amount_CNY": "12.3", "evidence": [self.ref]}
        r = validate_cash_action(self.root, self.action(), held_quantity=100, known_at="2026-10-15T15:00:00+08:00", actual_credit=credit)
        self.assertTrue(r["actual_credit_supplied"])
        self.assertFalse(r["cash_available_for_orders_verified"])
        for key, value in (("action_id", "other"), ("credited_at", "2026-10-09T09:16:00+08:00"),
                           ("credited_at", "2026-10-16T09:16:00+08:00"), ("evidence", [])):
            with self.subTest(key=key):
                c = copy.deepcopy(credit); c[key] = value
                with self.assertRaises(ValueError):
                    validate_cash_action(self.root, self.action(), held_quantity=100, known_at="2026-10-15T15:00:00+08:00", actual_credit=c)

    def test_credit_payable_date_uses_shanghai_not_supplied_offset_date(self):
        credit = {"action_id": "fixture-action", "instrument_id": "fixture-etf",
                  "credited_at": "2026-10-13T00:30:00+14:00", "amount_CNY": "12.3", "evidence": [self.ref]}
        with self.assertRaises(ValueError):
            validate_cash_action(self.root, self.action(), held_quantity=100, known_at="2026-10-15T15:00:00+08:00", actual_credit=credit)
        credit["credited_at"] = "2026-10-12T17:00:00+00:00"
        self.assertTrue(validate_cash_action(self.root, self.action(), held_quantity=100, known_at="2026-10-15T15:00:00+08:00", actual_credit=credit)["actual_credit_supplied"])


if __name__ == "__main__":
    unittest.main()
