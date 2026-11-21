"""Run a versioned BusinessOS change against a disposable local database."""

import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import psycopg

experiments = Path(__file__).resolve().parent / "experiments"
sys.path[:0] = [str(experiments / "procurement"), str(experiments / "builder")]

from builder_platform import BuilderPlatform
from postgres import IsolatedPostgres
from runtime import Denied, Runtime


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def run_demo(database, evidence):
    runtime = Runtime(database.dsn)
    runtime.bootstrap()
    owner = runtime.add_actor("demo", "owner")
    employee = runtime.add_actor("demo", "employee")
    service = runtime.add_actor("demo", "service")
    delivery = runtime.add_actor("demo", "delivery")
    builder = BuilderPlatform(runtime, evidence, tenant="demo")
    with psycopg.connect(database.dsn) as connection:
        connection.execute(
            "INSERT INTO project (tenant_id, id, release_id, name, budget, reserved, cost) "
            "VALUES ('demo', 'project-1', 'v1', 'Demo project', 20000, 0, 0)"
        )

    def record(table, record_id):
        rows = runtime.query("records", owner, {"object": table})
        return next(row for row in rows if row["id"] == record_id)

    print("BusinessOS · Local PostgreSQL demo")
    original = runtime.action("contract.create", employee, {"amount": 80_000}, "old-contract")
    pending = runtime.action(
        "purchase.submit", employee, {"project_id": "project-1", "amount": 15_000}, "purchase"
    )
    process = record("process", pending["process_id"])
    require(process["release_id"] == original["release_id"], "Initial release bindings differ")
    require(process["step"] == "wait_approval", "Expected a persisted approval wait")
    print(f"1. {process['release_id']}: saved an old contract and a purchase waiting for approval.")

    request = "Require a cost assessment before signing any new contract above CNY 30,000."
    candidate = builder.catalog()
    candidate["version"] += 1
    candidate["release_id"] = "demo-v2"
    candidate["policy"].update(cost_gate=True, cost_threshold=30_000)
    expression = experiments / "builder/generated/cost_gate.expr"
    candidate["extensions"]["contract.sign"] = expression.read_text().strip()
    draft = builder.draft(request, candidate, "cost-assessment")
    require(builder.validate(draft), "The demo rule failed validation")
    release = builder.publish(draft, owner)
    print(f"2. {release}: validated and activated the sample rule in the local fixture.")

    old_result = runtime.action("contract.sign", owner, {"id": original["id"]}, "sign-old")
    require(old_result["status"] == "signed", "The old contract failed under the original rule")
    require(old_result["release_id"] == original["release_id"], "The old contract changed releases")
    boundary = runtime.action("contract.create", employee, {"amount": 30_000}, "boundary")
    edge = runtime.action("contract.sign", owner, {"id": boundary["id"]}, "sign-boundary")
    require(edge == {"status": "signed", "release_id": release}, "The threshold must be exclusive")
    current = runtime.action("contract.create", employee, {"amount": 30_001}, "new-contract")
    require(current["release_id"] == release, "The new contract did not bind to the new release")
    try:
        runtime.action("contract.sign", owner, {"id": current["id"]}, "sign-without-assessment")
    except Denied as error:
        require(str(error) == "cost assessment required", "Signing was blocked for the wrong reason")
    else:
        raise RuntimeError("The new rule allowed signing without assessment")
    require(record("contract", current["id"])["status"] == "draft", "Rejected signing mutated the draft")
    runtime.action("contract.assess", delivery, {"id": current["id"]}, "assessment")
    new_result = runtime.action("contract.sign", owner, {"id": current["id"]}, "sign-new")
    require(new_result == {"status": "signed", "release_id": release}, "Signing failed after assessment")
    print("3. Old contract keeps v1. CNY 30,000 signs directly; CNY 30,001 requires assessment.")

    runtime.action("approval.approve", owner, {"id": pending["approval_id"]}, "approve-purchase")
    runtime.tick(service)
    process_after = record("process", pending["process_id"])
    require(process_after["release_id"] == process["release_id"], "The waiting process changed releases")
    require(process_after["step"] == "wait_receipt", "The waiting purchase did not resume")
    project = record("project", "project-1")
    require(project["reserved"] == 15_000, "Expected one CNY 15,000 budget reservation")
    print("4. The old purchase resumes on v1 and reserves CNY 15,000 on the existing project.")
    print("Demo passed. Local fixture will be removed.")


if __name__ == "__main__":
    with TemporaryDirectory(prefix="businessos-demo-") as temp:
        try:
            with IsolatedPostgres(Path(temp) / "pg") as postgres:
                run_demo(postgres, Path(temp) / "evidence")
        except subprocess.CalledProcessError as error:
            if error.stderr:
                print(error.stderr.decode(errors="replace").strip(), file=sys.stderr)
            raise SystemExit("PostgreSQL fixture failed. See README.md for prerequisites.") from error
