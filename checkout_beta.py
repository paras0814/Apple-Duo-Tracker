"""Durable, one-phone checkout beta. Browser automation always stops for review."""
from __future__ import annotations

import json
import re
from pathlib import Path
import queue
import subprocess
import threading
import time
import uuid

from beta.alerts import checkout_failure_payload, normalize_failure, safe_text

MAX_TOTAL_CENTS = 230_000
TEST_PART = "MHRN4LL/A"
TEST_PRODUCT = {
    "part": TEST_PART, "model": "iPhone 17e", "storage": "256GB", "color": "Black",
    "buy_url": "https://www.apple.com/shop/buy-iphone/iphone-17e/6.1-inch-display-256gb-black-unlocked",
}
TEST_PRODUCTS = {TEST_PART: TEST_PRODUCT, "MG474LL/A": {
    "part": "MG474LL/A", "model": "iPhone 17", "storage": "256GB", "color": "White",
    "buy_url": "https://www.apple.com/shop/buy-iphone/iphone-17/6.3-inch-display-256gb-white-unlocked",
}}
ACTIVE = {"armed", "checking_stock", "checking_stock_running", "queued", "preparing", "needs_attention", "review_ready"}
MUTATIONS = {"add_to_cart", "checkout_entry", "guest_login", "checkout_start", "select_pickup", "select_store", "select_slot", "pickup_contact", "select_credit", "zip_lookup", "billing", "payment_validation"}


def _mutation_key(value):
    # One billing-card intent regardless of the caller-facing label.
    return {"billing_card": "payment_validation", "submit_order": "place_order"}.get(value, value) if isinstance(value, str) else ""


def _verified_review(target, review):
    if not isinstance(review, dict) or not isinstance(target, dict):
        return None
    if not (review.get("part") == target.get("part") and review.get("store_id") == target.get("store_id")
            and type(review.get("quantity")) is int and review["quantity"] == 1
            and review.get("applecare") is False and review.get("currency") == "USD"
            and review.get("tax_included") is True and type(review.get("total_cents")) is int
            and 0 < review["total_cents"] <= MAX_TOTAL_CENTS):
        return None
    return {key: review[key] for key in ("part", "store_id", "quantity", "applecare", "currency", "tax_included", "total_cents")}


def _phase(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value):
        raise BetaError("Invalid API checkpoint phase.")
    return value


def _safe_facts(facts):
    """Checkpoint only model facts; cookies, contact and payment data stay elsewhere."""
    if not isinstance(facts, dict):
        return {}
    result = {}
    for key in ("quantity", "bag_count", "total_cents", "phone_price_cents", "base_price_cents", "http_status"):
        value = facts.get(key)
        if type(value) is int and 0 <= value <= (230_000 if key.endswith("cents") else 999):
            result[key] = value
    for key in ("applecare", "tax_included", "confirmed_empty", "pickup_confirmed", "session_ready", "store_confirmed", "slot_confirmed", "apple_response_confirmed"):
        if type(facts.get(key)) is bool:
            result[key] = facts[key]
    for key, pattern in (("part", r"M[A-Z0-9]{4}LL/A"), ("store_id", r"R\d{3}"), ("currency", r"USD")):
        value = facts.get(key)
        if isinstance(value, str) and re.fullmatch(pattern, value):
            result[key] = value
    host = facts.get("checkout_host")
    if isinstance(host, str) and re.fullmatch(r"(?:www\.apple\.com|secure\d*\.store\.apple\.com)", host):
        result["checkout_host"] = host
    path = facts.get("checkout_path")
    if isinstance(path, str) and re.fullmatch(r"/shop/(?:bag|signIn|checkout(?:/start)?)", path):
        result["checkout_path"] = path
    if facts.get("order_result") in ("accepted", "declined", "rejected", "unknown"):
        result["order_result"] = facts["order_result"]
    return result


def _order_outcome(outcome, facts):
    if facts.get("apple_response_confirmed") is not True:
        return "unknown"
    if outcome == "confirmed" and facts.get("order_result") == "accepted":
        return "confirmed"
    if outcome == "rejected" and facts.get("order_result") in ("declined", "rejected"):
        return "rejected"
    return "unknown"


class BetaError(ValueError):
    pass


class CheckoutBeta:
    def __init__(self, db, lock, stores, parts, buy_url):
        self.db, self.lock = db, lock
        self.stores, self.parts, self.buy_url = stores, parts, buy_url
        self.worker_run = None
        with self.lock, self.db:
            self.db.execute("""CREATE TABLE IF NOT EXISTS beta_runs (
                run_id TEXT PRIMARY KEY, status TEXT, is_test INTEGER,
                preferred_store TEXT, preferred_part TEXT, target_json TEXT,
                stage TEXT, message TEXT, created_at REAL, updated_at REAL,
                cancel_requested INTEGER DEFAULT 0, review_json TEXT)""")
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(beta_runs)")}
            for name, declaration in (("backend", "TEXT NOT NULL DEFAULT 'browser'"),
                                      ("submit_once", "INTEGER NOT NULL DEFAULT 0"),
                                      ("test_part", "TEXT NOT NULL DEFAULT 'MHRN4LL/A'"),
                                      ("result_json", "TEXT")):
                if name not in columns:
                    self.db.execute(f"ALTER TABLE beta_runs ADD COLUMN {name} {declaration}")
            self.db.execute("""CREATE TABLE IF NOT EXISTS beta_api_checkpoints (
                run_id TEXT PRIMARY KEY, phase TEXT NOT NULL, facts_json TEXT NOT NULL,
                updated_at REAL NOT NULL)""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS beta_api_mutations (
                run_id TEXT NOT NULL, mutation_key TEXT NOT NULL, phase TEXT NOT NULL,
                status TEXT NOT NULL, claim_token TEXT NOT NULL, facts_json TEXT NOT NULL,
                claimed_at REAL NOT NULL, updated_at REAL NOT NULL,
                PRIMARY KEY (run_id, mutation_key))""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS beta_failure_alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL, event_key TEXT NOT NULL,
                event_json TEXT NOT NULL, payload_json TEXT NOT NULL,
                delivery TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt REAL NOT NULL DEFAULT 0, claim_token TEXT,
                created_at REAL NOT NULL, updated_at REAL NOT NULL, message_id TEXT,
                UNIQUE (run_id, event_key))""")
            self.db.execute("""CREATE TABLE IF NOT EXISTS beta_order_authorizations (
                run_id TEXT PRIMARY KEY, review_json TEXT NOT NULL, authorized_at REAL NOT NULL,
                invalidated_at REAL, claimed_at REAL, claim_token TEXT)""")
            grant_columns = {row[1] for row in self.db.execute("PRAGMA table_info(beta_order_authorizations)")}
            if "review_observed_at" not in grant_columns:
                self.db.execute("ALTER TABLE beta_order_authorizations ADD COLUMN review_observed_at REAL NOT NULL DEFAULT 0")
            # An unspent authorization is not a promise that a restarted session's
            # current review is still the same. It must never auto-submit.
            self.db.execute("UPDATE beta_runs SET status='needs_attention',message='Order preparation restarted. Reconcile the current Apple review before proceeding.',updated_at=? WHERE run_id IN (SELECT run_id FROM beta_order_authorizations WHERE claimed_at IS NULL AND invalidated_at IS NULL)", (time.time(),))
            self.db.execute("UPDATE beta_order_authorizations SET invalidated_at=? WHERE claimed_at IS NULL AND invalidated_at IS NULL", (time.time(),))
            # An intent persisted before HTTP is never replayed after a lost worker.
            self.db.execute("UPDATE beta_api_mutations SET status='unknown',updated_at=? WHERE status='claimed'", (time.time(),))
            # A delivered webhook may have lost its acknowledgement. Never blind-resend.
            self.db.execute("UPDATE beta_failure_alerts SET delivery='uncertain',updated_at=? WHERE delivery='sending'", (time.time(),))
            self.db.execute("UPDATE beta_runs SET status='cancelled',message='Monitor restarted. Arm the beta again when ready.',updated_at=? WHERE status IN ('armed','checking_stock','checking_stock_running')", (time.time(),))
            self.db.execute("UPDATE beta_runs SET status='needs_attention',message='Monitor restarted during preparation. Inspect the beta bag before resetting.',updated_at=? WHERE status IN ('queued','preparing')", (time.time(),))

    def _current(self):
        return self.db.execute("SELECT * FROM beta_runs ORDER BY created_at DESC LIMIT 1").fetchone()

    def snapshot(self):
        with self.lock:
            current = self._current()
            result = {"status": "disarmed", "message": "Arm the beta when you want one phone prepared for review.",
                      "mode": "review_only", "max_total_cents": MAX_TOTAL_CENTS,
                      "quantity": 1, "applecare": False, "payment_submission": False,
                      "order_submission_authorized": False, "order_submission_attempted": False,
                      "pickup_slot_strategy": "earliest",
                      "test_product": TEST_PRODUCT, "can_arm": True}
            result.update(backend="api", submit_once=False, test_products=TEST_PRODUCTS)
            if current:
                target = json.loads(current["target_json"]) if current["target_json"] else None
                review = json.loads(current["review_json"]) if current["review_json"] else None
                result.update(run_id=current["run_id"], status=current["status"], is_test=bool(current["is_test"]),
                              stage=current["stage"], message=current["message"], target=target, review=review,
                              updated_at=current["updated_at"], can_arm=current["status"] not in ACTIVE and self.worker_run is None)
                result.update(backend=current["backend"], submit_once=bool(current["submit_once"]),
                              test_part=current["test_part"], result=json.loads(current["result_json"] or "{}"))
            result["worker_active"] = self.worker_run is not None
            if current:
                result["api"] = self.resume_info(current["run_id"])
                failures = self.db.execute("SELECT delivery,COUNT(*) AS count FROM beta_failure_alerts WHERE run_id=? GROUP BY delivery", (current["run_id"],)).fetchall()
                result["failure_alerts"] = {r["delivery"]: r["count"] for r in failures}
                grant = self.db.execute("SELECT invalidated_at,claimed_at,authorized_at,review_observed_at FROM beta_order_authorizations WHERE run_id=?", (current["run_id"],)).fetchone()
                if grant:
                    now = time.time()
                    result["order_submission_authorized"] = (grant["invalidated_at"] is None and grant["claimed_at"] is None and not current["cancel_requested"]
                                                               and 0 <= now - grant["authorized_at"] <= 30 and 0 <= now - grant["review_observed_at"] <= 30)
                    result["order_submission_attempted"] = grant["claimed_at"] is not None
                    result["payment_submission"] = result["order_submission_authorized"] or result["order_submission_attempted"]
            return result

    def arm(self, *, is_test=False, store_id=None, part=None, backend="browser", submit_once=False, test_part=TEST_PART):
        if backend not in ("api", "browser") or type(submit_once) is not bool or type(is_test) is not bool:
            raise BetaError("Choose a supported checkout mode and an explicit one-order submission option.")
        if submit_once and backend != "api":
            raise BetaError("One-order submission is supported only by the API beta.")
        if test_part not in TEST_PRODUCTS:
            raise BetaError("Choose a supported 256GB test phone.")
        if store_id is not None and store_id not in self.stores:
            raise BetaError("Choose a watched NJ or Manhattan store.")
        if part is not None and part not in self.parts:
            raise BetaError("Choose one of the two monitored 256GB colors.")
        with self.lock, self.db:
            current = self._current()
            if self.worker_run is not None or (current and current["status"] in ACTIVE):
                raise BetaError("A beta run is already active. Stop it and inspect the beta bag before resetting.")
            now = time.time()
            run_id = uuid.uuid4().hex
            status = "checking_stock" if is_test else "armed"
            message = f"Checking live pickup stock for the test {TEST_PRODUCTS[test_part]['model']}." if is_test else "Waiting for verified 256GB stock at a watched store."
            self.db.execute("INSERT INTO beta_runs (run_id,status,is_test,preferred_store,preferred_part,target_json,stage,message,created_at,updated_at,backend,submit_once,test_part) VALUES (?,?,?,?,?,NULL,?,?,?,?,?,?,?)",
                            (run_id, status, int(is_test), store_id, part, "waiting_for_stock", message, now, now, backend, int(submit_once), test_part))
        return self.snapshot()

    def _target(self, sid, part, *, is_test=False):
        store = self.stores[sid]
        product = TEST_PRODUCTS[part] if is_test else {"part": part, "model": "iPhone Duo", **self.parts[part], "buy_url": self.buy_url(part)}
        return {**product, "store_id": sid, "store_name": store["name"], "store_zip": store["zip"],
                "max_total_cents": MAX_TOTAL_CENTS, "quantity": 1, "applecare": False,
                "mode": "review_only", "is_test": is_test, "pickup_slot_strategy": "earliest"}

    def consider(self, observations):
        """Only current, verified Apple observations can claim a live run."""
        with self.lock, self.db:
            current = self._current()
            if not current or current["status"] != "armed":
                return
            for sid in self.stores:
                if current["preferred_store"] and current["preferred_store"] != sid:
                    continue
                for part in self.parts:
                    if current["preferred_part"] and current["preferred_part"] != part:
                        continue
                    observed = observations.get((sid, part), {})
                    if observed.get("status") == "available" and observed.get("verified"):
                        target = self._target(sid, part)
                        self.db.execute("UPDATE beta_runs SET status='queued',target_json=?,stage='stock_selected',message=?,updated_at=? WHERE run_id=? AND status='armed'",
                                        (json.dumps(target), f'Preparing one {target["model"]} at Apple {target["store_name"]}.', time.time(), current["run_id"]))
                        return

    def claim(self):
        with self.lock, self.db:
            current = self._current()
            if not current or current["status"] not in ("queued", "checking_stock"):
                return None
            next_status = "checking_stock_running" if current["status"] == "checking_stock" else "preparing"
            if next_status == "preparing":
                self.worker_run = current["run_id"]
            self.db.execute("UPDATE beta_runs SET status=?,updated_at=? WHERE run_id=?", (next_status, time.time(), current["run_id"]))
            return dict(current)

    def test_stock_checked(self, run_id, observations, errors=()):
        with self.lock, self.db:
            current = self._current()
            if not current or current["run_id"] != run_id or current["status"] != "checking_stock_running":
                return
            # Prefer the requested store, then World Trade Center, then catalog order.
            choices = [current["preferred_store"]] if current["preferred_store"] else ["R654", *self.stores]
            for sid in dict.fromkeys(choices):
                test_part = current["test_part"]
                observed = observations.get((sid, test_part), {})
                if observed.get("status") == "available" and observed.get("verified"):
                    target = self._target(sid, test_part, is_test=True)
                    self.db.execute("UPDATE beta_runs SET status='queued',target_json=?,stage='stock_selected',message=?,updated_at=? WHERE run_id=?",
                                    (json.dumps(target), f'Test phone available at Apple {target["store_name"]}. Preparing one for review.', time.time(), run_id))
                    return
            self.db.execute("UPDATE beta_runs SET status='blocked',stage='stock_check',message=?,updated_at=? WHERE run_id=?",
                            ("No verified pickup stock for the test phone at the selected watched store. " + ("The Apple check failed; try again later." if errors else "Choose another store or try again later."), time.time(), run_id))

    def cancelled(self, run_id):
        with self.lock:
            row = self.db.execute("SELECT cancel_requested FROM beta_runs WHERE run_id=?", (run_id,)).fetchone()
            return not row or bool(row[0])

    def progress(self, run_id, stage, message):
        try:
            stage = _phase(stage)
        except BetaError:
            stage = "unknown"
        with self.lock, self.db:
            self.db.execute("UPDATE beta_runs SET stage=?,message=?,updated_at=? WHERE run_id=? AND status='preparing' AND cancel_requested=0",
                            (stage, safe_text(message, 500), time.time(), run_id))

    def checkpoint(self, run_id, phase, facts=None):
        phase, facts = _phase(phase), _safe_facts(facts)
        with self.lock, self.db:
            row = self.db.execute("SELECT status,cancel_requested FROM beta_runs WHERE run_id=?", (run_id,)).fetchone()
            if not row or row["status"] != "preparing" or row["cancel_requested"]:
                raise BetaError("This beta run cannot advance an API checkpoint.")
            grant = self.db.execute("SELECT review_json FROM beta_order_authorizations WHERE run_id=? AND claimed_at IS NULL AND invalidated_at IS NULL", (run_id,)).fetchone()
            if grant and any(key in facts and facts[key] != value for key, value in json.loads(grant["review_json"]).items()):
                self.db.execute("UPDATE beta_order_authorizations SET invalidated_at=? WHERE run_id=?", (time.time(), run_id))
            self.db.execute("INSERT OR REPLACE INTO beta_api_checkpoints VALUES (?,?,?,?)", (run_id, phase, json.dumps(facts), time.time()))
        return self.resume_info(run_id)

    def authorize_order_once(self, run_id, reviewproof):
        """Record an explicit one-run instruction against the verified review.

        This method is not exposed as an automatic stock or public HTTP action.
        The caller must have the user's explicit instruction to submit this run.
        """
        with self.lock, self.db:
            current = self._current()
            if (not current or current["run_id"] != run_id or current["cancel_requested"]
                    or current["status"] not in ("preparing", "review_ready")):
                raise BetaError("This run has no current verified order review.")
            target = json.loads(current["target_json"] or "{}")
            proof = _verified_review(target, reviewproof)
            if proof is None:
                raise BetaError("Order authorization requires the exact one-phone, no-AppleCare, tax-inclusive review within $2,300.")
            saved = json.loads(current["review_json"] or "{}")
            if current["status"] == "preparing":
                checkpoint = self.db.execute("SELECT phase,facts_json,updated_at FROM beta_api_checkpoints WHERE run_id=?", (run_id,)).fetchone()
                saved = json.loads(checkpoint["facts_json"]) if checkpoint and checkpoint["phase"] == "review_ready" else {}
                observed_at = checkpoint["updated_at"] if checkpoint else 0
            else:
                observed_at = current["updated_at"]
            if not 0 <= time.time() - observed_at <= 30:
                raise BetaError("The verified review is stale. Fetch and validate a fresh Apple review before submission.")
            if _verified_review(target, saved) != proof:
                raise BetaError("Persist the matching verified review before authorizing its order.")
            if self.resume_info(run_id)["requires_reconciliation"]:
                raise BetaError("An earlier request still has an unknown outcome; reconcile it before authorizing an order.")
            existing = self.db.execute("SELECT * FROM beta_order_authorizations WHERE run_id=?", (run_id,)).fetchone()
            encoded = json.dumps(proof, sort_keys=True)
            if existing:
                if existing["claimed_at"] is not None or existing["invalidated_at"] is not None or existing["review_json"] != encoded:
                    raise BetaError("This run's one-order authorization was consumed, invalidated or bound to a different review.")
            else:
                self.db.execute("INSERT INTO beta_order_authorizations (run_id,review_json,authorized_at,review_observed_at) VALUES (?,?,?,?)", (run_id, encoded, time.time(), observed_at))
            self.db.execute("UPDATE beta_runs SET review_json=?,updated_at=? WHERE run_id=?", (encoded, time.time(), run_id))
        return self.snapshot()

    def claim_mutation(self, run_id, key, phase):
        """Persist intent before HTTP. Every mutation key is at most once per run."""
        key = _mutation_key(key)
        if key not in MUTATIONS and key != "place_order":
            raise BetaError("This beta mutation is not permitted.")
        phase = _phase(phase)
        with self.lock, self.db:
            current = self._current()
            grant = self.db.execute("SELECT * FROM beta_order_authorizations WHERE run_id=?", (run_id,)).fetchone()
            if key == "place_order":
                if (not current or current["run_id"] != run_id or current["cancel_requested"]
                        or current["status"] not in ("preparing", "review_ready")
                        or self.worker_run not in (None, run_id) or not grant
                        or grant["invalidated_at"] is not None or grant["claimed_at"] is not None):
                    raise BetaError("This run has no remaining one-order authorization.")
                proof = _verified_review(json.loads(current["target_json"] or "{}"), json.loads(current["review_json"] or "{}"))
                if proof is None or json.dumps(proof, sort_keys=True) != grant["review_json"]:
                    raise BetaError("The reviewed order changed after authorization.")
                observed_at = grant["review_observed_at"]
                now = time.time()
                if not 0 <= now - observed_at <= 30 or not 0 <= now - grant["authorized_at"] <= 30:
                    raise BetaError("The one-order authorization or Apple review expired. Recheck the current review before submitting.")
            elif grant and grant["claimed_at"] is not None:
                raise BetaError("An order was already attempted for this run; no further mutations are allowed.")
            elif (not current or current["run_id"] != run_id or current["status"] != "preparing"
                    or current["cancel_requested"] or self.worker_run != run_id):
                raise BetaError("This beta run has no active API worker.")
            if self.db.execute("SELECT 1 FROM beta_api_mutations WHERE run_id=? AND mutation_key=?", (run_id, key)).fetchone():
                raise BetaError("This API mutation was already attempted. Reconcile its result instead of retrying.")
            token, now = uuid.uuid4().hex, time.time()
            self.db.execute("INSERT INTO beta_api_mutations VALUES (?,?,?,?,?,?,?,?)", (run_id, key, phase, "claimed", token, "{}", now, now))
            if key == "place_order":
                self.db.execute("UPDATE beta_order_authorizations SET claimed_at=?,claim_token=? WHERE run_id=?", (now, token, run_id))
                self.db.execute("UPDATE beta_runs SET status='preparing',stage=?,updated_at=? WHERE run_id=?", (phase, now, run_id))
                self.worker_run = run_id
            elif grant and grant["invalidated_at"] is None:
                self.db.execute("UPDATE beta_order_authorizations SET invalidated_at=? WHERE run_id=?", (now, run_id))
            return token

    def complete_mutation(self, run_id, key, token, outcome, facts=None):
        key = _mutation_key(key)
        if outcome not in ("confirmed", "rejected", "unknown"):
            raise BetaError("Unknown API mutation outcome.")
        facts = _safe_facts(facts)
        with self.lock, self.db:
            row = self.db.execute("SELECT * FROM beta_api_mutations WHERE run_id=? AND mutation_key=?", (run_id, key)).fetchone()
            if not row or row["status"] != "claimed" or row["claim_token"] != token:
                return False
            if key == "add_to_cart" and outcome == "rejected" and not (facts.get("confirmed_empty") is True and facts.get("bag_count") == 0):
                outcome = "unknown"
            if key == "add_to_cart" and outcome == "confirmed":
                run = self.db.execute("SELECT target_json FROM beta_runs WHERE run_id=?", (run_id,)).fetchone()
                target = json.loads(run["target_json"] or "{}") if run else {}
                if not (facts.get("part") == target.get("part") and facts.get("quantity") == 1
                        and facts.get("bag_count") == 1 and facts.get("applecare") is False):
                    outcome = "unknown"
            if key == "place_order":
                outcome = _order_outcome(outcome, facts)
            self.db.execute("UPDATE beta_api_mutations SET status=?,facts_json=?,updated_at=? WHERE run_id=? AND mutation_key=?", (outcome, json.dumps(facts), time.time(), run_id, key))
            return True

    def reconcile_mutation(self, run_id, key, outcome, facts):
        """Resolve an unknown outcome from a fresh model, never from elapsed time."""
        key = _mutation_key(key)
        if outcome not in ("confirmed", "rejected"):
            raise BetaError("Reconciliation needs a confirmed or rejected result.")
        facts = _safe_facts(facts)
        with self.lock, self.db:
            run = self.db.execute("SELECT * FROM beta_runs WHERE run_id=?", (run_id,)).fetchone()
            intent = self.db.execute("SELECT * FROM beta_api_mutations WHERE run_id=? AND mutation_key=?", (run_id, key)).fetchone()
            if not run or not intent or intent["status"] != "unknown" or self.worker_run is not None:
                raise BetaError("This mutation is not awaiting stopped-worker reconciliation.")
            if key == "add_to_cart":
                target = json.loads(run["target_json"] or "{}")
                valid = (facts.get("confirmed_empty") is True and facts.get("bag_count") == 0) if outcome == "rejected" else (
                    facts.get("part") == target.get("part") and facts.get("quantity") == 1
                    and facts.get("bag_count") == 1 and facts.get("applecare") is False)
                if not valid:
                    raise BetaError("The fresh cart model did not prove the requested reconciliation.")
            if key == "place_order" and _order_outcome(outcome, facts) != outcome:
                raise BetaError("A fresh Apple order result is required to reconcile the order attempt.")
            self.db.execute("UPDATE beta_api_mutations SET status=?,facts_json=?,updated_at=? WHERE run_id=? AND mutation_key=?", (outcome, json.dumps(facts), time.time(), run_id, key))
        return self.resume_info(run_id)

    def resume_info(self, run_id):
        with self.lock:
            point = self.db.execute("SELECT phase,facts_json,updated_at FROM beta_api_checkpoints WHERE run_id=?", (run_id,)).fetchone()
            intents = self.db.execute("SELECT mutation_key,phase,status,updated_at FROM beta_api_mutations WHERE run_id=? ORDER BY claimed_at", (run_id,)).fetchall()
            return {"phase": point["phase"] if point else None,
                    "facts": json.loads(point["facts_json"]) if point else {},
                    "mutations": [dict(r) for r in intents],
                    "requires_reconciliation": any(r["status"] in ("claimed", "unknown") for r in intents)}

    def resume(self, run_id, *, reconciled=False):
        if reconciled is not True:
            raise BetaError("Inspect the current Apple state before resuming this run.")
        with self.lock, self.db:
            current = self._current()
            if (not current or current["run_id"] != run_id or current["status"] != "needs_attention"
                    or not current["target_json"] or self.worker_run is not None):
                raise BetaError("This beta run cannot be resumed.")
            if self.resume_info(run_id)["requires_reconciliation"]:
                raise BetaError("An API mutation still has an unknown outcome.")
            self.db.execute("UPDATE beta_runs SET status='queued',cancel_requested=0,message='Resuming the existing API session after reconciliation.',updated_at=? WHERE run_id=?", (time.time(), run_id))
        return self.snapshot()

    def record_failure(self, run_id, event):
        event = normalize_failure(event)
        with self.lock, self.db:
            run = self.db.execute("SELECT target_json FROM beta_runs WHERE run_id=?", (run_id,)).fetchone()
            if not run or not run["target_json"]:
                raise BetaError("A failure alert needs an existing selected beta target.")
            target = json.loads(run["target_json"])
            payload = checkout_failure_payload(event, target, self.stores.get(target.get("store_id"), {}))
            now = time.time()
            self.db.execute("INSERT OR IGNORE INTO beta_failure_alerts (run_id,event_key,event_json,payload_json,created_at,updated_at) VALUES (?,?,?,?,?,?)", (run_id, event["event_key"], json.dumps(event), json.dumps(payload), now, now))
            row = self.db.execute("SELECT id,delivery FROM beta_failure_alerts WHERE run_id=? AND event_key=?", (run_id, event["event_key"])).fetchone()
            return dict(row)

    def record_result(self, run_id, event):
        from beta.alerts import normalize_result, checkout_result_payload
        event = normalize_result(event)
        with self.lock, self.db:
            run = self.db.execute("SELECT target_json FROM beta_runs WHERE run_id=?", (run_id,)).fetchone()
            if not run or not run["target_json"]:
                raise BetaError("A checkout result needs an existing selected target.")
            target = json.loads(run["target_json"])
            payload = checkout_result_payload(event, target, self.stores.get(target.get("store_id"), {}))
            now = time.time()
            self.db.execute("INSERT OR IGNORE INTO beta_failure_alerts (run_id,event_key,event_json,payload_json,created_at,updated_at) VALUES (?,?,?,?,?,?)",
                            (run_id, event["event_key"], json.dumps(event), json.dumps(payload), now, now))
            return dict(self.db.execute("SELECT id,delivery FROM beta_failure_alerts WHERE run_id=? AND event_key=?", (run_id, event["event_key"])).fetchone())

    def claim_failure_alert(self):
        with self.lock, self.db:
            row = self.db.execute("SELECT * FROM beta_failure_alerts WHERE delivery='pending' AND next_attempt<=? ORDER BY id LIMIT 1", (time.time(),)).fetchone()
            if not row:
                return None
            token = uuid.uuid4().hex
            self.db.execute("UPDATE beta_failure_alerts SET delivery='sending',attempts=attempts+1,claim_token=?,updated_at=? WHERE id=? AND delivery='pending'", (token, time.time(), row["id"]))
            return {**dict(row), "delivery": "sending", "claim_token": token, "attempts": row["attempts"] + 1, "payload": json.loads(row["payload_json"])}

    def complete_failure_alert(self, event, *, message_id=None, failure=None):
        with self.lock, self.db:
            row = self.db.execute("SELECT * FROM beta_failure_alerts WHERE id=?", (event.get("id"),)).fetchone()
            if not row or row["delivery"] != "sending" or row["claim_token"] != event.get("claim_token"):
                return False
            if failure is None and message_id:
                delivery, delay = "delivered", 0
            elif failure is None or getattr(failure, "ambiguous", False):
                delivery, delay = "uncertain", 0
            elif getattr(failure, "terminal", False):
                delivery, delay = "failed", 0
            else:
                delivery = "pending"
                delay = max(float(getattr(failure, "retry_after", 0)), min(300, 5 * 2 ** min(row["attempts"], 6)))
            self.db.execute("UPDATE beta_failure_alerts SET delivery=?,next_attempt=?,updated_at=?,message_id=? WHERE id=?", (delivery, time.time() + delay, time.time(), str(message_id)[:100] if message_id else None, row["id"]))
            return True

    def finish(self, run_id, result):
        status = result.get("status")
        if status not in ("review_ready", "needs_attention", "blocked", "order_accepted"):
            status = "needs_attention"
        with self.lock, self.db:
            if self.worker_run == run_id:
                self.worker_run = None
            row = self.db.execute("SELECT * FROM beta_runs WHERE run_id=?", (run_id,)).fetchone()
            if not row or row["cancel_requested"] or row["status"] not in ("preparing", "checking_stock_running"):
                return
            review = result.get("review") or {}
            target = json.loads(row["target_json"] or "{}")
            if status == "order_accepted":
                order = self.db.execute("SELECT status,facts_json FROM beta_api_mutations WHERE run_id=? AND mutation_key='place_order'", (run_id,)).fetchone()
                if not order or order["status"] != "confirmed" or json.loads(order["facts_json"]).get("order_result") != "accepted":
                    status = "needs_attention"
                    result = {**result, "message": "Order acceptance could not be independently confirmed. Inspect this run before continuing."}
            if status == "review_ready":
                valid = (review.get("part") == target.get("part") and review.get("store_id") == target.get("store_id")
                         and type(review.get("quantity")) is int and review["quantity"] == 1 and review.get("applecare") is False
                         and review.get("currency") == "USD" and review.get("tax_included") is True
                         and type(review.get("total_cents")) is int and 0 < review["total_cents"] <= MAX_TOTAL_CENTS)
                if not valid:
                    status = "needs_attention"
                    result = {**result, "message": "Final review was incomplete or outside the beta limits. Review the Apple page manually."}
            # Only retain allowlisted review facts, never page text, tokens or contact data.
            safe_review = _safe_facts(review)
            allowed = {key: safe_review[key] for key in ("part", "store_id", "quantity", "applecare", "currency", "tax_included", "total_cents") if key in safe_review}
            may_have_cart = self.db.execute("SELECT 1 FROM beta_api_mutations WHERE run_id=? AND ((mutation_key='add_to_cart' AND status IN ('claimed','confirmed','unknown')) OR mutation_key='place_order')", (run_id,)).fetchone()
            if status == "blocked" and may_have_cart:
                status = "needs_attention"
            try:
                stage = _phase(result.get("stage", "review"))
            except BetaError:
                stage = "unknown"
            timings = result.get("timings_ms", {})
            safe_result = {"timings_ms": {k: v for k, v in timings.items() if isinstance(k, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", k) and type(v) is int and 0 <= v <= 86_400_000}} if isinstance(timings, dict) else {}
            safe_result.update(status=status, order_placed=status == "order_accepted")
            self.db.execute("UPDATE beta_runs SET status=?,stage=?,message=?,review_json=?,result_json=?,updated_at=? WHERE run_id=?",
                            (status, stage, safe_text(result.get("message", "Review the beta session."), 500), json.dumps(allowed), json.dumps(safe_result), time.time(), run_id))
        if isinstance(result.get("failure"), dict):
            self.record_failure(run_id, result["failure"])

    def stop(self):
        with self.lock, self.db:
            current = self._current()
            if current:
                self.db.execute("UPDATE beta_order_authorizations SET invalidated_at=? WHERE run_id=? AND claimed_at IS NULL AND invalidated_at IS NULL", (time.time(), current["run_id"]))
            if current and current["status"] in ACTIVE:
                started = current["status"] in ("preparing", "needs_attention", "review_ready")
                self.db.execute("UPDATE beta_runs SET status=?,cancel_requested=1,message=?,updated_at=? WHERE run_id=?",
                                ("needs_attention" if started else "cancelled", "Stopped. Inspect and clear the beta browser bag before resetting." if started else "Beta disarmed. Stock monitoring continues.", time.time(), current["run_id"]))
        return self.snapshot()

    def reset(self, bag_cleared=False):
        if bag_cleared is not True:
            raise BetaError("Confirm that the beta browser bag is empty before resetting.")
        with self.lock, self.db:
            current = self._current()
            if self.worker_run is not None or (current and current["status"] in ("preparing", "checking_stock_running", "queued")):
                raise BetaError("Stop the running beta before resetting.")
            if current and self.db.execute("SELECT 1 FROM beta_api_mutations WHERE run_id=? AND mutation_key='place_order' AND status IN ('claimed','unknown')", (current["run_id"],)).fetchone():
                raise BetaError("The order outcome is unknown. Verify Apple's order status before resetting, even if the bag is empty.")
            if current:
                self.db.execute("UPDATE beta_order_authorizations SET invalidated_at=? WHERE run_id=? AND claimed_at IS NULL AND invalidated_at IS NULL", (time.time(), current["run_id"]))
                self.db.execute("UPDATE beta_runs SET status='cancelled',cancel_requested=1,message='Beta reset. Arm again when ready.',updated_at=? WHERE run_id=?", (time.time(), current["run_id"]))
        return self.snapshot()


def run_browser(root, beta, job, stop):
    """Stream the local browser companion without coupling it to stock polling."""
    root = Path(root)
    try:
        node_dir = (root / ".runtime/node-path.txt").read_text(encoding="utf-8-sig").strip()
        node = Path(node_dir) / "node.exe"
        chrome = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
        if not node.is_file() or not chrome.is_file() or not (root / "beta/browser.mjs").is_file():
            raise OSError()
    except OSError:
        beta.finish(job["run_id"], {"status":"blocked", "stage":"browser_setup", "message":"The beta needs local Chrome and the portable Node runtime. Run Setup-Beta.cmd, then try again."})
        return
    plan = json.loads(job["target_json"])
    plan.update(run_id=job["run_id"], profile_dir=str(root / ".data/apple-beta-profile"), chrome_path=str(chrome))
    flags = subprocess.CREATE_NO_WINDOW if __import__("os").name == "nt" else 0
    process = subprocess.Popen([str(node), str(root / "beta/browser.mjs")], cwd=root, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", creationflags=flags)
    process.stdin.write(json.dumps(plan) + "\n")
    process.stdin.close()
    messages = queue.Queue()
    reader_done = threading.Event()
    def read_output():
        try:
            for line in process.stdout:
                try:
                    messages.put(json.loads(line))
                except ValueError:
                    pass
        finally:
            reader_done.set()
    # Discard raw browser diagnostics; they may contain session-specific URLs.
    def discard_errors():
        for _ in process.stderr:
            pass
    threading.Thread(target=read_output, daemon=True).start()
    threading.Thread(target=discard_errors, daemon=True).start()
    result = None
    deadline = time.monotonic() + 180
    try:
        while time.monotonic() < deadline:
            if stop.is_set() or beta.cancelled(job["run_id"]):
                break
            try:
                message = messages.get(timeout=0.25)
            except queue.Empty:
                if process.poll() is not None and reader_done.is_set():
                    break
                continue
            if message.get("type") == "progress":
                beta.progress(job["run_id"], message.get("stage", "preparing"), message.get("message", "Preparing the beta browser."))
            elif message.get("type") == "result":
                result = message
                break
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
    beta.finish(job["run_id"], result or {"status":"needs_attention", "stage":"interrupted", "message":"Browser automation stopped without a verified result. Inspect the beta browser bag before resetting."})
