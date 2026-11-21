"""Verify that billing is installed while procurement is waiting on the same database."""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE / "procurement"), str(HERE / "billing")]

from module import install, register
from run_experiment import fixture
from runtime import Denied


def main():
    postgres, runtime, tokens = fixture()
    try:
        owner, employee, other, service = (tokens[x] for x in ("owner", "employee", "other", "service"))
        pending = runtime.action("purchase.submit", employee, {"project_id": "p1", "amount": 15_000}, "coexist-submit")
        before = runtime.query("records", owner, {"object": "process"})[0]
        assert before["id"] == pending["process_id"] and before["status"] == "waiting"

        register(runtime)
        install(runtime, owner)
        current_a = runtime.current_definition("a")
        assert "billing_usage" in current_a["objects"] and "project" in current_a["objects"]
        assert "billing_usage" not in runtime.current_definition("b")["objects"]
        try:
            runtime.query("billing.statement", other, {"product": "api", "account": "probe", "period": "2026-10"})
        except Denied:
            pass
        else:
            raise AssertionError("tenant B reached a query from A's installation")

        scope = {"product": "api", "account": "probe", "period": "2026-10"}
        runtime.action("billing.publish_rate", owner, {"product": "api", "version": "r1", "effective_from": "2026-10-01T00:00:00Z", "price_micros": 20}, "coexist-rate")
        runtime.action("billing.import_usage", owner, {"product": "api", "account": "probe", "source_key": "coexist-usage", "occurred_at": "2026-10-10T00:00:00Z", "quantity": 50_000}, "coexist-usage")
        invoice = runtime.action("billing.recalculate", owner, scope, "coexist-bill")
        assert invoice["total_cents"] == 100

        runtime.action("approval.approve", owner, {"id": pending["approval_id"]}, "coexist-decision")
        runtime.tick(service)
        after = runtime.query("records", owner, {"object": "process"})[0]
        assert before["id"] == after["id"] and before["release_id"] == after["release_id"]
        assert after["step"] == "wait_receipt"
        reservations = runtime.query("records", owner, {"object": "reservation"})
        assert len(reservations) == 1 and reservations[0]["amount"] == 15_000
        assert runtime.query("billing.statement", owner, scope)["invoice"]["total_cents"] == 100
        print(json.dumps({
            "passed": True, "database": "one isolated PostgreSQL fixture",
            "operating_release": current_a["release_id"], "pending_process_release": before["release_id"],
            "process_after_install": after["step"], "active_reservation_cny": reservations[0]["amount"],
            "invoice_cents": invoice["total_cents"], "tenant_B_billing_query_denied": True,
        }, indent=2))
    finally:
        postgres.stop()


if __name__ == "__main__":
    main()
