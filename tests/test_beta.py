import copy
from pathlib import Path
import tempfile
import unittest
from urllib import request, error
import json

import monitor as m
from checkout_beta import BetaError, MAX_TOTAL_CENTS, TEST_PART
from test_monitor import config, observations, serve


class BetaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "beta.sqlite3"
        self.state = m.State(self.path, config())
        self.beta = self.state.beta

    def tearDown(self):
        self.state.db.close()
        self.temp.cleanup()

    def start_preparing(self):
        self.beta.arm()
        self.state.apply(observations("available"))
        job = self.beta.claim()
        self.assertEqual(job["status"], "queued")
        return job, json.loads(job["target_json"])

    def test_default_limits_follow_user_correction_and_never_enable_payment(self):
        beta = self.beta.snapshot()
        self.assertEqual(MAX_TOTAL_CENTS, 230000)
        self.assertEqual(beta["max_total_cents"], 230000)
        self.assertEqual(beta["quantity"], 1)
        self.assertFalse(beta["applecare"])
        self.assertFalse(beta["payment_submission"])
        self.assertEqual(beta["mode"], "review_only")

    def test_one_fresh_stock_event_claims_one_run_despite_many_stores(self):
        job, target = self.start_preparing()
        self.assertIn(target["store_id"], m.STORES)
        self.assertIn(target["part"], m.PARTS)
        self.assertEqual(target["quantity"], 1)
        self.assertIsNone(self.beta.claim())
        self.state.apply(observations("available"))
        self.assertEqual(self.beta.snapshot()["run_id"], job["run_id"])
        with self.assertRaises(BetaError):
            self.beta.arm()

    def test_unverified_or_sold_out_stock_never_opens_browser(self):
        self.beta.arm()
        self.state.apply(observations("unavailable"))
        self.assertIsNone(self.beta.claim())
        rows = observations("available")
        for row in rows.values(): row["verified"] = False
        self.state.apply(rows)
        self.assertIsNone(self.beta.claim())
        self.assertEqual(self.beta.snapshot()["status"], "armed")

    def test_store_and_color_selection_are_enforced(self):
        self.beta.arm(store_id="R654", part="MK1E4LL/A")
        rows = observations("available")
        rows[("R654", "MK1E4LL/A")]["status"] = "unavailable"
        self.state.apply(rows)
        self.assertIsNone(self.beta.claim())
        rows[("R654", "MK1E4LL/A")]["status"] = "available"
        self.state.apply(rows)
        target = json.loads(self.beta.claim()["target_json"])
        self.assertEqual((target["store_id"],target["part"]), ("R654","MK1E4LL/A"))

    def test_removed_store_and_test_part_cannot_be_armed_as_live(self):
        for kwargs in ({"store_id":"R612"},{"store_id":"R753"},{"part":TEST_PART}):
            with self.subTest(kwargs=kwargs), self.assertRaises(BetaError):
                self.beta.arm(**kwargs)

    def test_restart_does_not_rearm_or_replay_browser_preparation(self):
        self.beta.arm()
        self.state.db.close()
        self.state = m.State(self.path, config()); self.beta = self.state.beta
        self.assertEqual(self.beta.snapshot()["status"], "cancelled")
        self.start_preparing()
        self.state.db.close()
        self.state = m.State(self.path, config()); self.beta = self.state.beta
        self.assertEqual(self.beta.snapshot()["status"], "needs_attention")
        self.assertIsNone(self.beta.claim())
        with self.assertRaises(BetaError): self.beta.arm()

    def test_stop_cannot_unlock_a_still_running_browser_worker(self):
        job, target = self.start_preparing()
        self.beta.stop()
        self.assertTrue(self.beta.cancelled(job["run_id"]))
        with self.assertRaises(BetaError): self.beta.reset(True)
        with self.assertRaises(BetaError): self.beta.arm()
        self.beta.finish(job["run_id"], {"status":"needs_attention"})
        self.assertFalse(self.beta.snapshot()["worker_active"])
        with self.assertRaises(BetaError): self.beta.reset(False)
        self.beta.reset(True)
        self.assertTrue(self.beta.snapshot()["can_arm"])

    def test_ready_for_review_requires_every_fact_and_total_under_cap(self):
        job, target = self.start_preparing()
        review = {"part":target["part"],"store_id":target["store_id"],"quantity":1,
                  "applecare":False,"currency":"USD","tax_included":True,"total_cents":229999}
        self.beta.finish(job["run_id"], {"status":"review_ready","review":review})
        self.assertEqual(self.beta.snapshot()["status"], "review_ready")
        self.assertFalse(self.beta.snapshot()["can_arm"])

    def test_over_budget_wrong_quantity_care_store_or_unknown_tax_blocks_review(self):
        for change in ({"total_cents":230001},{"quantity":2},{"quantity":True},{"applecare":True},
                       {"tax_included":False},{"currency":"EUR"},{"store_id":"R612"},{"part":TEST_PART}):
            with self.subTest(change=change):
                self.beta.reset(True)
                job, target = self.start_preparing()
                review = {"part":target["part"],"store_id":target["store_id"],"quantity":1,
                          "applecare":False,"currency":"USD","tax_included":True,"total_cents":220000,**change}
                self.beta.finish(job["run_id"], {"status":"review_ready","review":review})
                self.assertEqual(self.beta.snapshot()["status"], "needs_attention")

    def test_diagnostic_stock_stays_separate_from_monitor_and_alerts(self):
        self.beta.arm(is_test=True)
        job = self.beta.claim()
        rows = {("R654",TEST_PART):{"status":"available","verified":True}}
        self.beta.test_stock_checked(job["run_id"],rows)
        target = json.loads(self.beta.claim()["target_json"])
        self.assertTrue(target["is_test"])
        self.assertEqual((target["part"],target["store_id"]),(TEST_PART,"R654"))
        self.assertNotIn(TEST_PART,m.PARTS)
        self.assertEqual(self.state.snapshot()["events"],[])

    def test_http_control_rejects_other_origins_and_does_not_accept_payment_mode(self):
        with serve(m.handler_for(self.state)) as server:
            base = f"http://127.0.0.1:{server.server_port}"
            def post(path, body, origin=None):
                headers={"Content-Type":"application/json"}
                if origin: headers["Origin"]=origin
                return request.urlopen(request.Request(base+path,data=json.dumps(body).encode(),headers=headers),timeout=2)
            with self.assertRaises(error.HTTPError) as caught:
                post('/api/beta/arm',{},'https://example.com')
            self.assertEqual(caught.exception.code,403); caught.exception.close()
            with post('/api/beta/arm',{"mode":"auto_pay","max_total_cents":999999},base) as response:
                beta=json.load(response)
            self.assertFalse(beta["payment_submission"])
            self.assertEqual(beta["max_total_cents"],230000)
            with self.assertRaises(error.HTTPError) as caught:
                post('/api/beta/arm',{},base)
            self.assertEqual(caught.exception.code,409); caught.exception.close()


if __name__ == '__main__':
    unittest.main()
