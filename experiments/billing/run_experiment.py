#!/usr/bin/env python3
"""Independent domain tests using the SAME Runtime and PostgreSQL fixture as procurement."""
import concurrent.futures
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
import uuid

HERE = Path(__file__).resolve().parent
SHARED = HERE.parent / "procurement"
sys.path.insert(0, str(SHARED))
from runtime import Runtime, Denied, Conflict
from postgres import IsolatedPostgres
from module import install, register, BillingError

OBSERVED = {}


class BillingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pg = IsolatedPostgres().start()
        cls.rt = Runtime(cls.pg.dsn)
        cls.rt.bootstrap()
        register(cls.rt)

    @classmethod
    def tearDownClass(cls):
        cls.pg.stop()

    def setUp(self):
        self.t = "billing-" + uuid.uuid4().hex
        self.owner = self.rt.add_actor(self.t, "owner")
        self.other = self.rt.add_actor(self.t + "-other", "owner")
        self.reader = self.rt.add_actor(self.t, "billing_reader")
        self.ingestor = self.rt.add_actor(self.t, "billing_ingestor")
        self.agent = self.rt.add_actor(self.t, "agent")
        install(self.rt,self.owner)
        install(self.rt,self.other)
        self.scope = {"account": "synthetic-customer", "product": "api", "period": "2026-10"}
        self.action("publish_rate", {"product":"api","version":"r1","effective_from":"2026-10-01T00:00:00Z","price_micros":20})

    def action(self, name, p, *, key=None, token=None):
        return self.rt.action("billing."+name, token or self.owner, p, key or uuid.uuid4().hex)

    def usage(self, source="u1", quantity=1_250_000, occurred="2026-10-10T12:00:00Z", received="2026-10-10T12:01:00Z", **kw):
        return self.action("import_usage", {"account":"synthetic-customer","product":"api","source_key":source,"quantity":quantity,"occurred_at":occurred,"received_at":received}, **kw)

    def statement(self, token=None):
        return self.rt.query("billing.statement", token or self.owner, self.scope)

    def draft(self):
        return self.action("recalculate", self.scope)

    def frozen(self):
        d = self.draft()
        return self.action("freeze", {**self.scope,"expected_revision":d["revision"]})

    def test_01_complete_replay_old_rate_late_usage_adjustment(self):
        self.action("publish_rate", {"product":"api","version":"r2","effective_from":"2026-10-15T00:00:00Z","price_micros":30})
        self.usage("old",1_000_000)
        self.usage("new",250_000,"2026-10-20T00:00:00Z","2026-10-20T00:00:01Z")
        before = self.frozen()
        late = self.usage("late-old",50_000,"2026-10-12T00:00:00Z","2026-11-03T00:00:00Z")
        self.assertEqual(late["rate_version"],"r1")
        self.assertTrue(late["late"])
        adjustment = self.action("adjust_frozen",self.scope)
        again = self.action("adjust_frozen",self.scope)
        s = self.statement()
        self.assertEqual(before["total_cents"],2750)
        self.assertEqual(s["invoice"]["total_cents"],2750)
        self.assertEqual(s["adjusted_total_cents"],2850)
        self.assertEqual(s["adjustments"][0]["delta_cents"],100)
        self.assertEqual(again["new_adjustments"],0)
        OBSERVED["replay"]={"frozen_cny":"27.50","late_usage_rate":"r1","adjustment_cny":"1.00","adjusted_cny":"28.50","frozen_original_unchanged":True,"second_reconcile_new_adjustments":0,"statement":s}

    def test_02_runtime_idempotency_and_changed_input_conflict(self):
        p={"account":"synthetic-customer","product":"api","source_key":"same-source","quantity":100,"occurred_at":"2026-10-12T00:00:00Z","received_at":"2026-10-12T00:00:01Z"}
        a=self.action("import_usage",p,key="request-1")
        self.assertEqual(a,self.action("import_usage",p,key="request-1"))
        with self.assertRaises(Conflict):self.action("import_usage",{**p,"quantity":101},key="request-1")
        with self.assertRaises(BillingError):self.action("import_usage",{**p,"quantity":101},key="request-2")
        replay=self.action("import_usage",p,key="request-3")
        self.assertEqual(a["usage_id"],replay["usage_id"])
        self.assertEqual(self.draft()["lines"],1)

    def test_03_occurrence_boundary_rate_immutable(self):
        self.action("publish_rate",{"product":"api","version":"r2","effective_from":"2026-10-15T00:00:00Z","price_micros":30})
        before=self.usage("before",1,"2026-10-14T23:59:59Z","2026-11-01T00:00:00Z")
        boundary=self.usage("boundary",1,"2026-10-15T00:00:00Z","2026-11-01T00:00:00Z")
        self.assertEqual(before["charge_micros"],20)
        self.assertEqual(boundary["charge_micros"],30)
        with self.assertRaises(BillingError):self.action("publish_rate",{"product":"api","version":"r1","effective_from":"2026-10-01T00:00:00Z","price_micros":99})
        with self.assertRaises(BillingError):self.action("publish_rate",{"product":"api","version":"retroactive","effective_from":"2026-10-14T00:00:00Z","price_micros":99})

    def test_04_stale_draft_must_recalculate_before_freeze(self):
        self.usage();d=self.draft();self.usage("late-draft",50_000)
        with self.assertRaises(BillingError):self.action("freeze",{**self.scope,"expected_revision":d["revision"]})
        d=self.draft();self.assertEqual(d["total_cents"],2600)
        self.action("freeze",{**self.scope,"expected_revision":d["revision"]})
        with self.assertRaises(BillingError):self.draft()

    def test_05_cumulative_rounding_adjustments_preserve_subcent_amounts(self):
        self.action("publish_rate",{"product":"tiny","version":"tiny-v1","effective_from":"2026-10-01T00:00:00Z","price_micros":3333})
        scope={**self.scope,"product":"tiny"}
        for i in range(3):
            self.action("import_usage",{"account":"synthetic-customer","product":"tiny","source_key":f"tiny-{i}","quantity":1,"occurred_at":"2026-10-10T00:00:00Z","received_at":"2026-11-01T00:00:00Z"})
            if i==0:
                d=self.action("recalculate",scope);self.action("freeze",{**scope,"expected_revision":d["revision"]})
            else:self.action("adjust_frozen",scope)
        s=self.rt.query("billing.statement",self.owner,scope)
        self.assertEqual(s["invoice"]["total_cents"],0)
        self.assertEqual(sorted(a["delta_cents"] for a in s["adjustments"]),[0,1])
        self.assertEqual(s["adjusted_total_cents"],1)

    def test_06_real_postgres_concurrent_adjustment_applies_once(self):
        self.usage();self.frozen();self.usage("late",50_000,"2026-10-10T00:00:00Z","2026-11-01T00:00:00Z")
        barrier=threading.Barrier(2)
        def work(i):
            barrier.wait();return self.action("adjust_frozen",self.scope,key="concurrent-adjust-"+str(i))
        with concurrent.futures.ThreadPoolExecutor(2) as pool:out=list(pool.map(work,[1,2]))
        self.assertEqual(sum(x["new_adjustments"] for x in out),1)
        self.assertEqual(self.statement()["adjusted_total_cents"],2600)
        OBSERVED["concurrent_adjustment"]={"workers":2,"new_adjustments":sorted(x["new_adjustments"] for x in out),"adjusted_total_cents":2600}

    def test_07_real_postgres_freeze_races_with_usage_import(self):
        self.usage();d=self.draft();barrier=threading.Barrier(2)
        def freeze():
            barrier.wait()
            try:return self.action("freeze",{**self.scope,"expected_revision":d["revision"]})
            except BillingError:return {"status":"stale_draft_rejected"}
        def ingest():
            barrier.wait();return self.usage("race",50_000,"2026-10-10T00:00:00Z","2026-11-01T00:00:00Z")
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            a=pool.submit(freeze);b=pool.submit(ingest);out=[a.result(),b.result()]
        s=self.statement()
        if s["invoice"]["status"]=="draft":self.frozen()
        self.action("adjust_frozen",self.scope)
        self.assertEqual(self.statement()["adjusted_total_cents"],2600)
        OBSERVED["freeze_import_race"]={"freeze_outcome":out[0]["status"],"final_total_cents":2600,"lost_usage":False}

    def test_08_dual_tenant_and_current_permission(self):
        self.usage();self.frozen()
        self.assertFalse(self.statement(self.other)["found"])
        self.assertTrue(self.statement(self.reader)["found"])
        with self.assertRaises(Denied):self.action("freeze",{**self.scope,"expected_revision":1},token=self.agent)
        with self.assertRaises(Denied):self.action("publish_rate",{"product":"api","version":"unauthorized","effective_from":"2026-12-01T00:00:00Z","price_micros":40},token=self.ingestor)
        self.rt.revoke(self.reader)
        with self.assertRaises(Denied):self.statement(self.reader)
        # Database RLS also protects a shared generic Query without explicit tenant predicates.
        self.assertEqual(self.rt.query("records",self.other,{"object":"billing_usage"}),[])

    def test_09_new_process_reads_committed_data(self):
        self.usage();self.frozen()
        env={**os.environ,"BILLING_PROBE_DSN":self.pg.dsn,"BILLING_PROBE_TOKEN":self.owner}
        proc=subprocess.run([sys.executable,str(__file__),"--probe"],env=env,capture_output=True,text=True,check=True)
        self.assertEqual(json.loads(proc.stdout)["invoice"]["total_cents"],2500)
        OBSERVED["persistence"]={"separate_python_process":True,"committed_total_cents":2500}

    def test_10_ingestor_cannot_spoof_ingestion_time(self):
        with self.assertRaises(BillingError):self.usage(token=self.ingestor)
        p={"account":"synthetic-customer","product":"api","source_key":"trusted-now","quantity":1,"occurred_at":"2026-10-10T00:00:00Z"}
        self.action("import_usage",p,token=self.ingestor)

    def test_11_validation_rejects_missing_rate_floats_and_ambiguous_time(self):
        with self.assertRaises(BillingError):self.usage(quantity=1.5)
        with self.assertRaises(BillingError):self.usage(occurred="2026-10-10T00:00:00")
        with self.assertRaises(BillingError):self.usage(occurred="2026-09-30T00:00:00Z",received="2026-10-10T00:00:00Z")


def main():
    if "--probe" in sys.argv:
        rt=Runtime(os.environ["BILLING_PROBE_DSN"]);register(rt)
        print(json.dumps(rt.query("billing.statement",os.environ["BILLING_PROBE_TOKEN"],{"account":"synthetic-customer","product":"api","period":"2026-10"})))
        return
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(BillingTests)
    outcome=unittest.TextTestRunner(verbosity=2).run(suite)
    report={"experiment":"billing on procurement's shared Runtime and real PostgreSQL","tests_run":outcome.testsRun,"failures":len(outcome.failures),"errors":len(outcome.errors),"passed":outcome.wasSuccessful(),"observed":OBSERVED,"limits":["trusted Python domain plugin, not Builder sandbox","UTC month and CNY only","no tax/payment/revenue recognition","Process interpreter itself is not generalized or verified by this billing module"]}
    (HERE/"results.json").write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps({k:v for k,v in report.items() if k!="observed"},ensure_ascii=False))
    sys.exit(0 if outcome.wasSuccessful() else 1)


if __name__=="__main__":main()
