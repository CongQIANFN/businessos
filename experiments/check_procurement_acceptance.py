"""Independent acceptance probes for the shared PostgreSQL experiment.

Run from any directory using procurement's virtual environment. Fails if a
boundary is missing. Uses its own disposable database and synthetic identities.
"""
import inspect
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "procurement"))

import psycopg
from run_experiment import fixture
from runtime import Conflict, Denied


def capture(function):
    try:
        return function(), None
    except (Denied, Conflict, ValueError) as error:
        return None, type(error).__name__


def run(runtime, tokens, dsn):
    checks = {}
    owner, employee, other = [tokens[name] for name in ("owner", "employee", "other")]
    results = runtime.query("records", other, {"object": "project"})
    checks["tenant_B_reads_own_project"] = len(results) == 1 and results[0]["tenant_id"] == "b"
    _, denial = capture(lambda: runtime.action(
        "budget.reserve", owner,
        {"project_id": "p1", "source": "unapproved-probe", "amount": 1}, "unapproved-probe",
    ))
    checks["direct_budget_reserve_rejected"] = denial is not None

    payload = {"project_id": "p1", "amount": 100}
    original = runtime.action("purchase.submit", owner, payload, "shared-probe-key")
    other_result, denial = capture(lambda: runtime.action(
        "purchase.submit", employee, payload, "shared-probe-key",
    ))
    checks["other_identity_cannot_obtain_original_receipt"] = (
        denial is not None or other_result != original
    )

    target = {"amount": 100, "release_id": "v1"}
    _, denial = capture(lambda: runtime.action("contract.create", employee, target, "old-version-probe"))
    checks["caller_cannot_pin_old_release_for_new_record"] = denial is not None

    parameters = inspect.signature(runtime.current_definition).parameters
    def definition(tenant):
        return runtime.current_definition(tenant) if parameters else runtime.current_definition()

    before_b = definition("b")["release_id"]
    original_contract = runtime.action("contract.create", owner, {"amount": 80_000}, "inherited-release-probe-contract")
    colliding = definition("a")
    colliding["policy"]["cost_gate"] = True
    _, denial = capture(lambda: runtime.publish(colliding, owner))
    checks["tenant_cannot_shadow_inherited_release_id"] = denial is not None
    signed, denial = capture(lambda: runtime.action(
        "contract.sign", owner, {"id": original_contract["id"]}, "inherited-release-probe-sign",
    ))
    checks["old_contract_keeps_original_rule_after_collision_attempt"] = (
        denial is None and signed["status"] == "signed" and signed["release_id"] == original_contract["release_id"]
    )
    candidate = definition("a")
    candidate["release_id"] = "acceptance-release-a"
    candidate["objects"]["project"]["columns"]["a_internal_note"] = "text"
    result = runtime.publish(candidate, owner)
    checks["tenant_A_release_activated"] = definition("a")["release_id"] == candidate["release_id"]
    checks["tenant_B_release_unaffected"] = definition("b")["release_id"] == before_b
    checks["tenant_B_query_excludes_A_custom_column"] = "a_internal_note" not in runtime.query(
        "records", other, {"object": "project"},
    )[0]
    mismatched = definition("b")
    mismatched["release_id"] = "acceptance-release-incompatible-b"
    mismatched["objects"]["project"]["columns"]["a_internal_note"] = "bigint"
    _, denial = capture(lambda: runtime.publish(mismatched, other))
    checks["incompatible_shared_physical_type_is_rejected"] = denial is not None

    receipt_probe = runtime.action("purchase.submit", employee, payload, "external-probe-submit")
    runtime.action("approval.approve", owner, {"id": receipt_probe["approval_id"]}, "external-probe-approval")
    runtime.action("purchase.authorize", owner, {"id": receipt_probe["purchase_id"]}, "external-probe-budget")
    pending = runtime.action("purchase.dispatch", owner, {"id": receipt_probe["purchase_id"]}, "external-probe-send")
    for who in ("owner", "service"):
        _, denial = capture(lambda: runtime.action(
            "external.confirm", tokens[who],
            {"id": pending["external_id"], "receipt": "unverified"}, "external-probe-confirm-" + who,
        ))
        checks[who + "_cannot_supply_unverified_connector_receipt"] = denial is not None
    with psycopg.connect(dsn) as connection:
        checks["no_unapproved_reservation_in_database"] = connection.execute(
            "SELECT count(*) FROM reservation WHERE tenant_id='a' AND source='unapproved-probe'"
        ).fetchone()[0] == 0
        checks["external_order_stays_pending_after_unverified_receipt"] = connection.execute(
            "SELECT status FROM external_order WHERE tenant_id='a' AND id=%s", (pending["external_id"],),
        ).fetchone()[0] == "pending"
    return {"checks": checks, "published_release": result}


if __name__ == "__main__":
    postgres, runtime, tokens = fixture()
    try:
        result = run(runtime, tokens, postgres.dsn)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        postgres.stop()
    raise SystemExit(0 if all(result["checks"].values()) else 1)
