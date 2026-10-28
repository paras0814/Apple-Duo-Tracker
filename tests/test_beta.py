import copy
from pathlib import Path
import tempfile
import unittest
from urllib import request, error
import json
import threading

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
        self.assertEqual(beta["pickup_slot_strategy"], "earliest")

    def test_one_fresh_stock_event_claims_one_run_despite_many_stores(self):
        job, target = self.start_preparing()
        self.assertIn(target["store_id"], m.STORES)
        self.assertIn(target["part"], m.PARTS)
        self.assertEqual(target["quantity"], 1)
        self.assertEqual(target["pickup_slot_strategy"], "earliest")
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
            with post('/api/beta/arm',{"mode":"auto_pay","max_total_cents":999999,"pickup_slot_strategy":"latest"},base) as response:
                beta=json.load(response)
            self.assertFalse(beta["payment_submission"])
            self.assertEqual(beta["max_total_cents"],230000)
            self.assertEqual(beta["pickup_slot_strategy"],"earliest")
            with self.assertRaises(error.HTTPError) as caught:
                post('/api/beta/arm',{},base)
            self.assertEqual(caught.exception.code,409); caught.exception.close()

    def restart_state(self):
        self.state.db.close()
        self.state = m.State(self.path, config())
        self.beta = self.state.beta

    def test_atomic_cart_intent_rejects_two_workers_and_no_final_order_key(self):
        job, _ = self.start_preparing()
        barrier = threading.Barrier(2)
        claims, errors = [], []
        def claim():
            barrier.wait()
            try: claims.append(self.beta.claim_mutation(job["run_id"], "add_to_cart", "adding"))
            except BetaError: errors.append(True)
        threads = [threading.Thread(target=claim) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertEqual((len(claims), len(errors)), (1, 1))
        with self.assertRaises(BetaError):
            self.beta.claim_mutation(job["run_id"], "submit_order", "order")

    def test_restart_turns_lost_intent_unknown_and_invalidates_old_completion(self):
        job, target = self.start_preparing()
        run = job["run_id"]
        token = self.beta.claim_mutation(run, "add_to_cart", "adding")
        self.beta.checkpoint(run, "adding", {"part":target["part"], "cookie":"SECRET_COOKIE", "card_number":"SECRET_PAN"})
        self.restart_state()
        self.assertEqual(self.beta.snapshot()["status"], "needs_attention")
        self.assertTrue(self.beta.resume_info(run)["requires_reconciliation"])
        self.assertFalse(self.beta.complete_mutation(run, "add_to_cart", token, "confirmed", {"part":target["part"],"quantity":1,"bag_count":1,"applecare":False}))
        with self.assertRaises(BetaError): self.beta.resume(run, reconciled=True)
        self.assertNotIn("SECRET", json.dumps(self.beta.snapshot()))
        self.assertIsNone(self.beta.claim())

    def test_fresh_cart_reconciliation_resumes_same_run_without_replaying_add(self):
        job, target = self.start_preparing(); run = job["run_id"]
        self.beta.claim_mutation(run, "add_to_cart", "adding")
        self.restart_state()
        with self.assertRaises(BetaError):
            self.beta.reconcile_mutation(run, "add_to_cart", "confirmed", {"part":target["part"],"quantity":2,"bag_count":2,"applecare":False})
        self.beta.reconcile_mutation(run, "add_to_cart", "confirmed", {"part":target["part"],"quantity":1,"bag_count":1,"applecare":False})
        with self.assertRaises(BetaError): self.beta.resume(run)
        self.beta.resume(run, reconciled=True)
        resumed = self.beta.claim()
        self.assertEqual(resumed["run_id"], run)
        with self.assertRaises(BetaError): self.beta.claim_mutation(run, "add_to_cart", "adding")
        self.assertTrue(self.beta.claim_mutation(run, "checkout_entry", "checkout_entry"))

    def test_rejected_cart_needs_empty_model_and_never_retries_same_key(self):
        job, _ = self.start_preparing(); run = job["run_id"]
        token = self.beta.claim_mutation(run, "add_to_cart", "adding")
        self.beta.complete_mutation(run, "add_to_cart", token, "rejected")
        self.assertEqual(self.beta.resume_info(run)["mutations"][0]["status"], "unknown")
        with self.assertRaises(BetaError): self.beta.claim_mutation(run, "add_to_cart", "adding")
        self.beta.finish(run, {"status":"blocked", "stage":"adding"})
        self.assertEqual(self.beta.snapshot()["status"], "needs_attention")
        self.assertFalse(self.beta.snapshot()["can_arm"])

    def test_wrong_token_cannot_complete_mutation_and_billing_alias_is_one_attempt(self):
        job, _ = self.start_preparing(); run = job["run_id"]
        token = self.beta.claim_mutation(run, "billing_card", "billing_validation")
        self.assertFalse(self.beta.complete_mutation(run, "billing_card", "wrong-token", "confirmed"))
        self.assertTrue(self.beta.complete_mutation(run, "billing_card", token, "unknown"))
        with self.assertRaises(BetaError): self.beta.claim_mutation(run, "payment_validation", "billing_validation")

    def test_stop_blocks_new_mutations_and_redacts_checkpoint_values(self):
        job, target = self.start_preparing(); run = job["run_id"]
        self.beta.checkpoint(run, "checkout_entry", {"part":target["part"],"checkout_host":"secure8.store.apple.com", "checkout_path":"/shop/checkout?token=PRIVATE_TOKEN", "email":"PRIVATE_EMAIL", "quantity":True})
        data = self.beta.resume_info(run)
        self.assertEqual(data["facts"], {"part":target["part"],"checkout_host":"secure8.store.apple.com"})
        self.beta.stop()
        with self.assertRaises(BetaError): self.beta.claim_mutation(run, "guest_login", "guest_login")
        with self.assertRaises(BetaError): self.beta.checkpoint(run, "guest_login", {})

    def test_late_result_cannot_overwrite_completed_review(self):
        job, target = self.start_preparing()
        review = {"part":target["part"],"store_id":target["store_id"],"quantity":1,"applecare":False,"currency":"USD","tax_included":True,"total_cents":220000}
        self.beta.finish(job["run_id"], {"status":"review_ready","review":review})
        self.beta.finish(job["run_id"], {"status":"blocked","message":"Late failure"})
        self.assertEqual(self.beta.snapshot()["status"], "review_ready")

    def test_failure_webhook_dedup_survives_restart_and_ambiguous_send(self):
        job, _ = self.start_preparing(); run = job["run_id"]
        event = {"classification":"validation_failed","stage":"billing_validation","source":"apple","code":"invalidExpiration"}
        first = self.beta.record_failure(run, event)
        duplicate = self.beta.record_failure(run, {**event,"reason":"Different untrusted server text"})
        self.assertEqual(first["id"], duplicate["id"])
        send = self.beta.claim_failure_alert()
        self.assertIsNone(self.beta.claim_failure_alert())
        self.restart_state()
        self.assertEqual(self.beta.snapshot()["failure_alerts"], {"uncertain":1})
        self.assertFalse(self.beta.complete_failure_alert(send, message_id="late-message"))
        self.assertIsNone(self.beta.claim_failure_alert())
        self.assertEqual(self.beta.record_failure(run, event)["delivery"], "uncertain")

    def test_failure_alert_definite_failure_retries_but_unknown_never_resends(self):
        from unittest.mock import patch
        job, _ = self.start_preparing(); run = job["run_id"]
        self.beta.record_failure(run, {"kind":"network_error","stage":"billing_validation"})
        first = self.beta.claim_failure_alert()
        self.beta.complete_failure_alert(first, failure=m.FetchError("Temporary failure", retry_after=1))
        self.assertIsNone(self.beta.claim_failure_alert())
        with patch('checkout_beta.time.time', return_value=10**12): second = self.beta.claim_failure_alert()
        self.assertEqual(second["attempts"], 2)
        self.beta.complete_failure_alert(second, failure=m.FetchError("Unknown send", ambiguous=True))
        with patch('checkout_beta.time.time', return_value=10**12): self.assertIsNone(self.beta.claim_failure_alert())

    def test_failure_payload_storage_excludes_private_contact_or_payment_text(self):
        job, _ = self.start_preparing(); run = job["run_id"]
        self.beta.record_failure(run, {"kind":"field_validation","source":"apple","stage":"billing_validation", "reason":"Rejected Jane Example at 123 Example Street; CVV 123; 4111 1111 1111 1111; person@example.com; token=PRIVATE_TOKEN"})
        alert = self.beta.claim_failure_alert()
        saved = json.dumps({"event":alert["event_json"],"payload":alert["payload"]})
        for private in ("Jane Example", "Example Street", "4111", "person@example", "PRIVATE_TOKEN", "CVV 123"):
            self.assertNotIn(private, saved)
        self.assertIn("Apple rejected checkout input", saved)

    def prepare_order_review(self):
        job, target = self.start_preparing()
        review = {"part":target["part"],"store_id":target["store_id"],"quantity":1,
                  "applecare":False,"currency":"USD","tax_included":True,"total_cents":220000}
        self.beta.finish(job["run_id"], {"status":"review_ready","review":review})
        return job, review

    def test_order_cannot_be_authorized_before_persisted_verified_review(self):
        job, target = self.start_preparing()
        proof = {"part":target["part"],"store_id":target["store_id"],"quantity":1,
                 "applecare":False,"currency":"USD","tax_included":True,"total_cents":220000}
        with self.assertRaises(BetaError): self.beta.authorize_order_once(job["run_id"], proof)
        with self.assertRaises(BetaError): self.beta.claim_mutation(job["run_id"],"place_order","order_submission")
        self.assertFalse(self.beta.snapshot()["order_submission_authorized"])

    def test_order_authorization_enforces_exact_review_and_spending_constraints(self):
        job, proof = self.prepare_order_review()
        for changes in ({"total_cents":230001},{"total_cents":219999},{"quantity":True},
                        {"quantity":2},{"applecare":True},{"tax_included":False},
                        {"currency":"CAD"},{"store_id":"R612"},{"part":TEST_PART}):
            with self.subTest(changes=changes), self.assertRaises(BetaError):
                self.beta.authorize_order_once(job["run_id"],{**proof,**changes})
        self.assertFalse(self.beta.snapshot()["payment_submission"])

    def test_single_order_authorization_is_idempotent_but_racing_alias_claims_are_once(self):
        job, proof = self.prepare_order_review(); run = job["run_id"]
        self.beta.authorize_order_once(run,proof)
        self.assertTrue(self.beta.authorize_order_once(run,proof)["order_submission_authorized"])
        barrier = threading.Barrier(2); claims=[]; errors=[]
        def claim(key):
            barrier.wait()
            try:claims.append(self.beta.claim_mutation(run,key,"order_submission"))
            except BetaError:errors.append(True)
        threads=[threading.Thread(target=claim,args=(key,)) for key in ("place_order","submit_order")]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertEqual((len(claims),len(errors)),(1,1))
        self.assertTrue(self.beta.snapshot()["order_submission_attempted"])
        self.assertFalse(self.beta.snapshot()["order_submission_authorized"])
        with self.assertRaises(BetaError):self.beta.authorize_order_once(run,proof)
        with self.assertRaises(BetaError):self.beta.claim_mutation(run,"billing_card","billing_validation")

    def test_active_worker_may_authorize_only_matching_review_ready_checkpoint(self):
        job,target=self.start_preparing();run=job["run_id"]
        proof={"part":target["part"],"store_id":target["store_id"],"quantity":1,
               "applecare":False,"currency":"USD","tax_included":True,"total_cents":220000}
        self.beta.checkpoint(run,"billing",proof)
        with self.assertRaises(BetaError):self.beta.authorize_order_once(run,proof)
        self.beta.checkpoint(run,"review_ready",proof)
        self.assertTrue(self.beta.authorize_order_once(run,proof)["order_submission_authorized"])
        self.beta.checkpoint(run,"review_ready",{**proof,"total_cents":219999})
        with self.assertRaises(BetaError):self.beta.claim_mutation(run,"place_order","order_submission")

    def test_stop_invalidates_unspent_order_grant_and_new_runs_do_not_inherit_it(self):
        job,proof=self.prepare_order_review();run=job["run_id"]
        self.beta.authorize_order_once(run,proof);self.beta.stop()
        with self.assertRaises(BetaError):self.beta.claim_mutation(run,"place_order","order_submission")
        self.beta.reset(True);self.beta.arm()
        self.assertFalse(self.beta.snapshot()["order_submission_authorized"])
        self.assertFalse(self.beta.snapshot()["payment_submission"])

    def test_restart_invalidates_unspent_order_grant_without_auto_submitting(self):
        job,proof=self.prepare_order_review();run=job["run_id"]
        self.beta.authorize_order_once(run,proof);self.restart_state()
        self.assertEqual(self.beta.snapshot()["status"],"needs_attention")
        self.assertFalse(self.beta.snapshot()["order_submission_authorized"])
        self.assertIsNone(self.beta.claim())
        with self.assertRaises(BetaError):self.beta.claim_mutation(run,"place_order","order_submission")

    def test_uncertain_order_restart_cannot_retry_or_reset_on_empty_bag_alone(self):
        job,proof=self.prepare_order_review();run=job["run_id"]
        self.beta.authorize_order_once(run,proof)
        token=self.beta.claim_mutation(run,"place_order","order_submission")
        self.restart_state()
        self.assertFalse(self.beta.complete_mutation(run,"place_order",token,"rejected",{"order_result":"declined","apple_response_confirmed":True}))
        with self.assertRaises(BetaError):self.beta.reset(True)
        with self.assertRaises(BetaError):self.beta.resume(run,reconciled=True)
        with self.assertRaises(BetaError):self.beta.reconcile_mutation(run,"place_order","rejected",{"confirmed_empty":True,"bag_count":0})
        self.beta.reconcile_mutation(run,"place_order","rejected",{"order_result":"declined","apple_response_confirmed":True})
        self.beta.reset(True)
        self.assertTrue(self.beta.snapshot()["can_arm"])
        with self.assertRaises(BetaError):self.beta.claim_mutation(run,"place_order","order_submission")

    def test_order_response_requires_explicit_acceptance_or_rejection_evidence(self):
        job,proof=self.prepare_order_review();run=job["run_id"]
        self.beta.authorize_order_once(run,proof)
        token=self.beta.claim_mutation(run,"place_order","order_submission")
        self.assertFalse(self.beta.complete_mutation(run,"place_order","stale","confirmed",{"order_result":"accepted","apple_response_confirmed":True}))
        self.assertTrue(self.beta.complete_mutation(run,"place_order",token,"confirmed",{"http_status":200}))
        intent=self.beta.resume_info(run)["mutations"][-1]
        self.assertEqual(intent["status"],"unknown")
        self.beta.finish(run,{"status":"blocked","stage":"order_submission"})
        self.assertEqual(self.beta.snapshot()["status"],"needs_attention")
        with self.assertRaises(BetaError):self.beta.reset(True)


if __name__ == '__main__':
    unittest.main()
