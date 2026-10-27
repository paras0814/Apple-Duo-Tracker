"""Durable, one-phone checkout beta. Browser automation always stops for review."""
from __future__ import annotations

import json
from pathlib import Path
import queue
import subprocess
import threading
import time
import uuid

MAX_TOTAL_CENTS = 230_000
TEST_PART = "MHRN4LL/A"
TEST_PRODUCT = {
    "part": TEST_PART, "model": "iPhone 17e", "storage": "256GB", "color": "Black",
    "buy_url": "https://www.apple.com/shop/buy-iphone/iphone-17e/6.1-inch-display-256gb-black-unlocked",
}
ACTIVE = {"armed", "checking_stock", "checking_stock_running", "queued", "preparing", "needs_attention", "review_ready"}


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
                      "test_product": TEST_PRODUCT, "can_arm": True}
            if current:
                target = json.loads(current["target_json"]) if current["target_json"] else None
                review = json.loads(current["review_json"]) if current["review_json"] else None
                result.update(run_id=current["run_id"], status=current["status"], is_test=bool(current["is_test"]),
                              stage=current["stage"], message=current["message"], target=target, review=review,
                              updated_at=current["updated_at"], can_arm=current["status"] not in ACTIVE and self.worker_run is None)
            result["worker_active"] = self.worker_run is not None
            return result

    def arm(self, *, is_test=False, store_id=None, part=None):
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
            message = "Checking live pickup stock for the test iPhone 17e." if is_test else "Waiting for verified 256GB stock at a watched store."
            self.db.execute("INSERT INTO beta_runs (run_id,status,is_test,preferred_store,preferred_part,target_json,stage,message,created_at,updated_at) VALUES (?,?,?,?,?,NULL,?,?,?,?)",
                            (run_id, status, int(is_test), store_id, part, "waiting_for_stock", message, now, now))
        return self.snapshot()

    def _target(self, sid, part, *, is_test=False):
        store = self.stores[sid]
        product = TEST_PRODUCT if is_test else {"part": part, "model": "iPhone Duo", **self.parts[part], "buy_url": self.buy_url(part)}
        return {**product, "store_id": sid, "store_name": store["name"], "store_zip": store["zip"],
                "max_total_cents": MAX_TOTAL_CENTS, "quantity": 1, "applecare": False,
                "mode": "review_only", "is_test": is_test}

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
                observed = observations.get((sid, TEST_PART), {})
                if observed.get("status") == "available" and observed.get("verified"):
                    target = self._target(sid, TEST_PART, is_test=True)
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
        with self.lock, self.db:
            self.db.execute("UPDATE beta_runs SET stage=?,message=?,updated_at=? WHERE run_id=? AND status='preparing' AND cancel_requested=0",
                            (str(stage)[:80], str(message)[:500], time.time(), run_id))

    def finish(self, run_id, result):
        status = result.get("status")
        if status not in ("review_ready", "needs_attention", "blocked"):
            status = "needs_attention"
        with self.lock, self.db:
            if self.worker_run == run_id:
                self.worker_run = None
            row = self.db.execute("SELECT * FROM beta_runs WHERE run_id=?", (run_id,)).fetchone()
            if not row or row["cancel_requested"]:
                return
            review = result.get("review") or {}
            target = json.loads(row["target_json"] or "{}")
            if status == "review_ready":
                valid = (review.get("part") == target.get("part") and review.get("store_id") == target.get("store_id")
                         and type(review.get("quantity")) is int and review["quantity"] == 1 and review.get("applecare") is False
                         and review.get("currency") == "USD" and review.get("tax_included") is True
                         and type(review.get("total_cents")) is int and 0 < review["total_cents"] <= MAX_TOTAL_CENTS)
                if not valid:
                    status = "needs_attention"
                    result = {**result, "message": "Final review was incomplete or outside the beta limits. Review the Apple page manually."}
            # Only retain allowlisted review facts, never page text, tokens or contact data.
            allowed = {key: review[key] for key in ("part", "store_id", "quantity", "applecare", "currency", "tax_included", "total_cents") if key in review}
            self.db.execute("UPDATE beta_runs SET status=?,stage=?,message=?,review_json=?,updated_at=? WHERE run_id=?",
                            (status, str(result.get("stage", "review"))[:80], str(result.get("message", "Review the beta browser."))[:500], json.dumps(allowed), time.time(), run_id))

    def stop(self):
        with self.lock, self.db:
            current = self._current()
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
            if current:
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
