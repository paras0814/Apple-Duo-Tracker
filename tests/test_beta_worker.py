"""Offline worker, warm-session, receipt and one-run authorization fixtures."""
import contextlib
import io
import json
import sqlite3
from pathlib import Path
import tempfile
import threading
import unittest

from beta.alerts import checkout_result_payload, normalize_result
from beta.worker import APIBetaWorker


def plan():
    return {'part': 'MG474LL/A', 'model': 'iPhone 17', 'storage': '256GB', 'color': 'White',
        'store_id': 'R654', 'store_name': 'World Trade Center', 'store_zip': '10007',
        'quantity': 1, 'applecare': False, 'mode': 'review_only', 'is_test': True,
        'max_total_cents': 230000, 'buy_url': 'https://www.apple.com/shop/buy-iphone/iphone-17/6.3-inch-display-256gb-white-unlocked'}


def review():
    return {'part': 'MG474LL/A', 'store_id': 'R654', 'quantity': 1, 'applecare': False,
            'currency': 'USD', 'tax_included': True, 'total_cents': 86911}


def review_result():
    return {'status': 'review_ready', 'stage': 'review_ready', 'review': review(),
        'order_placed': False, 'timings_ms': {'total': 2634, 'stock': 100, 'PRIVATE_TOKEN': 1},
        'pickup_slot': {'store_id': 'R654', 'date': '2026-10-29', 'start': '09:15 AM',
                        'end': '09:30 AM', 'time_zone': 'America/New_York', 'signKey': 'PRIVATE_SIGNATURE'}}


class FakeBeta:
    def __init__(self, *, status='queued', submit_once=False, backend='api'):
        self.current = {'run_id': 'fixture-run', 'status': status, 'backend': backend, 'submit_once': submit_once}
        self.finished, self.records, self.outbox, self.grants, self.stock = [], [], [], [], []
        self.claims = 0
    def snapshot(self): return dict(self.current)
    def cancelled(self, run_id): return self.current['status'] == 'cancelled'
    def claim(self):
        if self.current['status'] not in ('queued', 'checking_stock'): return None
        self.claims += 1
        result = {**self.current, 'target_json': json.dumps(plan()), 'test_part': 'MG474LL/A'}
        self.current['status'] = 'preparing' if self.current['status'] == 'queued' else 'checking_stock_running'
        return result
    def finish(self, run_id, result):
        self.finished.append((run_id, result));self.current['status'] = result['status']
    def test_stock_checked(self, run_id, rows, errors):
        self.stock.append((run_id, rows, errors));self.current['status'] = 'queued' if rows else 'blocked'
    def authorize_order_once(self, run_id, proof): self.grants.append((run_id, proof))
    def record_result(self, run_id, event):
        event = normalize_result(event)
        if any(r[0] == run_id and r[1]['event_key'] == event['event_key'] for r in self.records): return
        self.records.append((run_id, event))
        self.outbox.append({'id': len(self.outbox)+1, 'attempts': 0, 'delivery': 'pending',
            'payload': checkout_result_payload(event, plan(), {'name': 'World Trade Center',
                'retail_url': 'https://www.apple.com/retail/worldtradecenter/'})})
    record_failure = record_result
    def claim_failure_alert(self):
        for event in self.outbox:
            if event['delivery'] == 'pending':
                event['delivery'] = 'sending';event['attempts'] += 1
                return dict(event)
    def complete_failure_alert(self, event, *, message_id=None, failure=None):
        current = self.outbox[event['id']-1]
        current['delivery'] = ('delivered' if message_id else 'uncertain' if failure.ambiguous
                               else 'failed' if failure.terminal else 'pending')
        current['failure'] = failure


class FakeClient:
    def __init__(self, root, run_id):
        self.root, self.run_id = root, run_id
        self.verifications, self.closed, self.gets = [], 0, []
        self.valid = False
    def verify_session(self, origin, *, client_profile):
        self.verifications.append((origin, client_profile));self.valid = True
    def verification_cookie_valid(self, origin): return self.valid
    def request(self, method, url, **kwargs):
        self.gets.append((method, url))
        empty = {'shoppingCart': {'d': {'hasItems': False}, 'items': {'d': {'bagCount': 0}}}}
        return type('Response', (), {'status': 200, 'content': (
            '<script id="init_data">' + json.dumps(empty) + '</script>').encode()})()
    def close(self): self.closed += 1


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.stop = threading.Event()
        self.clients = []
    def factory(self, root, run_id):
        client = FakeClient(root, run_id);self.clients.append(client);return client
    def worker(self, beta, **kwargs):
        return APIBetaWorker(self.tmp.name, beta, self.stop, client_factory=self.factory, **kwargs)

    def test_prewarm_before_stock_and_reuse_exact_client_on_trigger(self):
        beta = FakeBeta(status='armed');observed = []
        def flow(root, selected, guard, **kwargs):
            observed.append(kwargs['prepared_client'])
            kwargs['prepared_client'].close()
            return review_result()
        worker = self.worker(beta, flow_runner=flow)
        self.assertFalse(worker.step())
        self.assertEqual(self.clients[0].verifications, [('https://www.apple.com', 'public_v3_proof')])
        self.assertEqual(self.clients[0].gets, [('GET', 'https://www.apple.com/shop/bag')])
        self.assertFalse(worker.step())
        self.assertEqual(len(self.clients), 1)
        beta.current['status'] = 'queued'
        self.assertTrue(worker.step())
        self.assertIs(observed[0], self.clients[0])
        self.assertEqual(beta.finished[0][1]['status'], 'review_ready')
        self.assertEqual(beta.records[0][1]['classification'], 'review_ready')
        self.assertFalse(beta.finished[0][1]['order_placed'])
        self.assertEqual(len(beta.outbox), 1)

    def test_default_review_only_and_two_conditions_for_final_grant(self):
        for job_flag, grant, expected in [(False, True, False), (True, False, False),
                                          ('true', True, False), (1, True, True)]:
            beta = FakeBeta(submit_once=job_flag);calls = []
            def flow(root, selected, guard, **kwargs):
                calls.append(kwargs['allow_submit_once'])
                if expected:
                    self.assertTrue(kwargs['authorize_final'](review()))
                    self.assertFalse(kwargs['authorize_final'](review()))
                return review_result()
            worker = self.worker(beta, flow_runner=flow, final_grant=lambda run_id: grant)
            worker.step()
            self.assertEqual(calls, [expected])
            self.assertEqual(len(beta.grants), int(expected))

    def test_wrong_run_or_stopped_worker_cannot_authorize_final(self):
        beta = FakeBeta(submit_once=True)
        def flow(root, selected, guard, **kwargs):
            beta.current['run_id'] = 'other-run'
            self.assertFalse(kwargs['authorize_final'](review()))
            self.stop.set()
            self.assertFalse(kwargs['authorize_final'](review()))
            return review_result()
        self.worker(beta, flow_runner=flow, final_grant=lambda _: True).step()
        self.assertFalse(beta.grants)

    def test_all_private_output_is_dropped_and_exact_catalog_links_remain(self):
        beta = FakeBeta()
        raw = {**review_result(), 'message': 'PRIVATE_NAME 123 Private Street buyer@example.com 4242424242424242 CVV=123',
               'profile': 'PRIVATE_PROFILE', 'cookies': 'PRIVATE_COOKIE'}
        output = io.StringIO()
        worker = self.worker(beta, flow_runner=lambda *a, **k: raw, profile_alias='buyer@example.com')
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):worker.step()
        serialized = json.dumps({'finished': beta.finished, 'records': beta.records, 'outbox': beta.outbox})
        for secret in ('PRIVATE_', 'buyer@example.com', '4242424242424242', 'CVV=123'):
            self.assertNotIn(secret, serialized + output.getvalue())
        self.assertIn('https://www.apple.com/retail/worldtradecenter/', serialized)
        self.assertIn(plan()['buy_url'], serialized)
        self.assertIn('local-beta', serialized)
        self.assertIn('2.634s', serialized)

    def test_accepted_requires_all_observed_flags_and_exact_review(self):
        for proof in (False, True):
            beta = FakeBeta()
            raw = {**review_result(), 'status': 'order_accepted', 'order_placed': True,
                   'order_result': 'accepted', 'apple_response_confirmed': proof}
            self.worker(beta, flow_runner=lambda *a, **k: raw).step()
            self.assertEqual(beta.records[0][1]['classification'], 'order_accepted' if proof else 'unknown_error')
            self.assertEqual(beta.finished[0][1]['status'], 'order_accepted' if proof else 'needs_attention')
            if not proof:self.assertEqual(beta.records[0][1]['order_result'], 'unknown')

    def test_state_journal_rejects_success_receipt_without_durable_acceptance(self):
        beta = FakeBeta()
        def finish(run_id, result): beta.current['status'] = 'needs_attention'
        beta.finish = finish
        raw = {**review_result(), 'status': 'order_accepted', 'order_placed': True,
               'order_result': 'accepted', 'apple_response_confirmed': True}
        self.worker(beta, flow_runner=lambda *a, **k: raw).step()
        self.assertEqual(beta.records[0][1]['classification'], 'unknown_error')
        self.assertEqual(beta.records[0][1]['order_result'], 'unknown')

    def test_actual_decline_is_preserved_and_unproved_decline_downgraded(self):
        for actual in (False, True):
            beta = FakeBeta()
            raw = {'status': 'needs_attention', 'stage': 'place_order', 'order_result': 'declined',
                'failure': {'classification': 'payment_declined', 'source': 'apple', 'apple_decline': actual,
                            'reason': 'PRIVATE_APPLE_ECHO', 'http_status': 200}}
            self.worker(beta, flow_runner=lambda *a, **k: raw).step()
            self.assertEqual(beta.records[0][1]['classification'], 'payment_declined' if actual else 'unknown_error')
            self.assertNotIn('PRIVATE_', json.dumps(beta.records))

    def test_durable_outbox_delivers_once_and_does_not_resend_ambiguous_failure(self):
        beta = FakeBeta();sent = []
        def send(payload): sent.append(payload);return '123456789'
        worker = self.worker(beta, flow_runner=lambda *a, **k: review_result(), send_webhook=send)
        worker.step();worker.step()
        self.assertEqual(len(sent), 1)
        self.assertEqual(beta.outbox[0]['delivery'], 'delivered')
        beta.record_result('another-run', {'classification': 'blocked', 'stage': 'stock'})
        worker.send_webhook = lambda _: (_ for _ in ()).throw(TimeoutError('PRIVATE_WEBHOOK_URL'))
        worker.deliver_result_once();worker.deliver_result_once()
        self.assertEqual(beta.outbox[1]['delivery'], 'uncertain')
        self.assertNotIn('PRIVATE_', str(beta.outbox[1]['failure']))

    def test_definite_webhook_failures_stop_at_bounded_attempts(self):
        beta = FakeBeta();beta.record_result('fixture-run', {'classification': 'blocked'})
        class Rejected(Exception): ambiguous=False;retry_after=2
        worker = self.worker(beta, send_webhook=lambda _: (_ for _ in ()).throw(Rejected()), max_alert_attempts=3)
        for _ in range(5):worker.deliver_result_once()
        self.assertEqual(beta.outbox[0]['attempts'], 3)
        self.assertEqual(beta.outbox[0]['delivery'], 'failed')

    def test_browser_jobs_and_stopped_workers_do_not_start_api(self):
        beta = FakeBeta(backend='browser')
        worker = self.worker(beta, flow_runner=lambda *a, **k: self.fail('API flow must not start'))
        self.assertFalse(worker.step());self.assertFalse(self.clients);self.assertEqual(beta.claims, 0)
        beta.current['backend'] = 'api';self.stop.set()
        self.assertFalse(worker.step());self.assertFalse(self.clients)

    def test_verification_failure_uses_backoff_without_mutating(self):
        beta = FakeBeta(status='armed');clock = [0]
        def failed_factory(*args):raise ValueError('PRIVATE_FAILURE')
        worker = APIBetaWorker(self.tmp.name, beta, self.stop, client_factory=failed_factory, clock=lambda:clock[0])
        worker.step();worker.step()
        self.assertEqual(worker._warm_failures, 1)
        self.assertEqual(worker.snapshot()['warm_status'], 'unavailable')
        clock[0] = 16;worker.step()
        self.assertEqual(worker._warm_failures, 2)

    def test_stock_probe_is_delegated_and_does_not_run_checkout_same_claim(self):
        beta = FakeBeta(status='checking_stock');calls=[]
        worker = self.worker(beta, stock_probe=lambda job: ({('R654', 'MG474LL/A'): {'verified': True}}, []),
            flow_runner=lambda *a, **k: calls.append(True))
        self.assertTrue(worker.step());self.assertEqual(len(beta.stock), 1);self.assertFalse(calls)

    def test_unexpected_callbacks_never_escape_step_or_expose_exception(self):
        beta = FakeBeta()
        worker = self.worker(beta, flow_runner=lambda *a, **k: (_ for _ in ()).throw(RuntimeError('PRIVATE_PROFILE_CARD')))
        self.assertTrue(worker.step())
        self.assertEqual(beta.finished[0][1]['status'], 'needs_attention')
        self.assertNotIn('PRIVATE_', json.dumps(beta.finished))
        beta.snapshot = lambda: (_ for _ in ()).throw(RuntimeError('PRIVATE_STATE'))
        self.assertFalse(worker.step())

    def test_real_durable_state_worker_result_outbox_interoperability(self):
        from checkout_beta import CheckoutBeta
        db = sqlite3.connect(':memory:');db.row_factory = sqlite3.Row
        self.addCleanup(db.close)
        beta = CheckoutBeta(db, threading.RLock(), {'R654': {'name': 'World Trade Center',
            'zip': '10007', 'retail_url': 'https://www.apple.com/retail/worldtradecenter/'}},
            {'MK1F4LL/A': {'storage': '256GB', 'color': 'Night Sky'}}, lambda _: '')
        beta.arm(is_test=True, backend='api', store_id='R654', test_part='MG474LL/A')
        sent = []
        worker = self.worker(beta, stock_probe=lambda _: ({('R654', 'MG474LL/A'):
            {'status': 'available', 'verified': True}}, []),
            flow_runner=lambda *a, **k: review_result(), send_webhook=lambda payload: sent.append(payload) or '123456789')
        worker.step();worker.step();worker.step()
        self.assertEqual(beta.snapshot()['status'], 'review_ready')
        self.assertEqual(len(sent), 1)
        row = db.execute('SELECT * FROM beta_failure_alerts').fetchone()
        self.assertEqual(row['delivery'], 'delivered')
        beta.record_result(beta.snapshot()['run_id'], json.loads(row['event_json']))
        self.assertEqual(db.execute('SELECT COUNT(*) FROM beta_failure_alerts').fetchone()[0], 1)
        worker.close()


if __name__ == '__main__': unittest.main()
