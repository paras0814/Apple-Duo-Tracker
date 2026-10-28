"""Background API preparation and durable, sanitized result notifications.

This service has no dashboard authorization route. Review-only is the default;
final submission additionally needs a trusted one-run grant and the state journal.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
import re
import time

from beta.alerts import normalize_result, sanitize_timings
from beta.apple_client import AppleClient, assert_empty_bag, parse_init_data
from beta.flow import BetaGuard, run_flow


WWW = 'https://www.apple.com'


class _DeliveryFailure(Exception):
    def __init__(self, *, ambiguous=False, terminal=False, retry_after=0):
        super().__init__('The result webhook delivery was not confirmed.')
        self.ambiguous, self.terminal, self.retry_after = ambiguous, terminal, retry_after


def _enabled(value):
    # SQLite stores the explicit boolean as integer 0/1. Strings are not grants.
    return value is True or type(value) is int and value == 1


def _safe_review(value, plan):
    if not isinstance(value, dict):
        return {}
    if not (value.get('part') == plan.get('part') and value.get('store_id') == plan.get('store_id')
            and type(value.get('quantity')) is int and value['quantity'] == 1
            and value.get('applecare') is False and value.get('currency') == 'USD'
            and value.get('tax_included') is True and type(value.get('total_cents')) is int
            and 0 < value['total_cents'] <= 230_000):
        return {}
    return {key: value[key] for key in ('part', 'store_id', 'quantity', 'applecare',
                                       'currency', 'tax_included', 'total_cents')}


class APIBetaWorker:
    def __init__(self, root, beta, stop, *, stock_probe=None, profile_path=None,
                 profile_alias='local-beta', send_webhook=None, final_grant=None,
                 flow_runner=run_flow, client_factory=AppleClient,
                 warm_refresh_seconds=180, clock=time.monotonic, max_alert_attempts=5):
        self.root, self.beta, self.stop = Path(root).resolve(), beta, stop
        self.stock_probe, self.profile_path = stock_probe, profile_path
        self.profile_alias = profile_alias if isinstance(profile_alias, str) and re.fullmatch(
            r'[A-Za-z][A-Za-z0-9_-]{0,31}', profile_alias) else 'local-beta'
        self.send_webhook, self.final_grant = send_webhook, final_grant
        self.flow_runner, self.client_factory, self.clock = flow_runner, client_factory, clock
        self.warm_refresh_seconds = min(600, max(30, warm_refresh_seconds))
        self.max_alert_attempts = min(10, max(1, max_alert_attempts))
        self._warm = None
        self._warm_run = None
        self._warm_at = 0
        self._warm_retry_at = 0
        self._warm_failures = 0
        self._warm_status = 'idle'
        self._warm_ms = 0

    def snapshot(self):
        return {'connection': 'direct_no_proxy', 'warm_status': self._warm_status,
                'run_id': self._warm_run, 'warm_ms': self._warm_ms,
                'review_only_default': True}

    def _discard_warm(self):
        client, self._warm = self._warm, None
        self._warm_run = None
        self._warm_status = 'idle'
        self._warm_ms = 0
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    def prepare_warm_session(self):
        """Acquire/renew only anonymous verification; never add or submit data."""
        current = self.beta.snapshot()
        run_id = current.get('run_id')
        if (self.stop.is_set() or current.get('backend', 'browser') != 'api'
                or current.get('status') not in {'armed', 'checking_stock', 'queued'}
                or not isinstance(run_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', run_id)):
            self._discard_warm()
            return False
        if self._warm_run != run_id:
            self._discard_warm()
            self._warm_retry_at = 0
            self._warm_failures = 0
        now = self.clock()
        if now < self._warm_retry_at:
            return False
        if self._warm is not None:
            try:
                if now - self._warm_at < self.warm_refresh_seconds and self._warm.verification_cookie_valid(WWW):
                    return True
            except Exception:
                self._discard_warm()
        started = self.clock()
        try:
            if self._warm is None:
                self._warm = self.client_factory(self.root, run_id)
                self._warm_run = run_id
            self._warm.verify_session(WWW, client_profile='native_compatible_emulation')
            if not self._warm.verification_cookie_valid(WWW):
                raise ValueError()
            bag = self._warm.request('GET', WWW + '/shop/bag', headers={'Accept': 'text/html', 'Referer': WWW + '/'})
            if bag.status != 200:
                raise ValueError()
            assert_empty_bag(parse_init_data(bag.content))
            self._warm_at = self.clock()
            self._warm_ms = max(0, round((self._warm_at - started) * 1000))
            self._warm_status, self._warm_failures = 'ready', 0
            return True
        except Exception:
            self._discard_warm()
            # Remember the run across failures so an ordinary next loop cannot
            # reset its bounded backoff and hammer verification every 250ms.
            self._warm_run = run_id
            self._warm_failures += 1
            self._warm_retry_at = self.clock() + min(300, 15 * 2 ** min(self._warm_failures - 1, 5))
            self._warm_status = 'unavailable'
            return False

    def _result(self, raw, plan):
        raw = raw if isinstance(raw, dict) else {}
        review = _safe_review(raw.get('review'), plan)
        stage = raw.get('stage', 'unknown')
        if not isinstance(stage, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', stage):
            stage = 'unknown'
        status = raw.get('status')
        accepted = (status == 'order_accepted' and raw.get('order_result') == 'accepted'
                    and raw.get('apple_response_confirmed') is True and raw.get('order_placed') is True and bool(review))
        status = 'order_accepted' if accepted else 'review_ready' if status == 'review_ready' and review else 'blocked' if status == 'blocked' else 'needs_attention'
        message = {'order_accepted': 'Apple confirmed the one-phone order.',
                   'review_ready': 'One phone reached verified review; no order was submitted.',
                   'blocked': 'The API beta stopped before a verified result.',
                   'needs_attention': 'The API beta needs inspection; no checkout request will be automatically replayed.'}[status]
        result = {'status': status, 'stage': stage, 'message': message, 'review': review,
                  'order_placed': accepted, 'connection': 'direct_no_proxy', 'browser_required': False,
                  'timings_ms': sanitize_timings(raw.get('timings_ms'))}
        if self._warm_ms:
            result['timings_ms']['warm_session'] = self._warm_ms
        if accepted:
            result.update(order_result='accepted', apple_response_confirmed=True)
        elif raw.get('order_result') in ('declined', 'rejected', 'unknown', 'not_submitted'):
            result['order_result'] = raw['order_result']
        elif raw.get('status') == 'order_accepted' or raw.get('order_placed') is True or stage in {
                'place_order', 'order_submit', 'order_submission', 'order_processing', 'order_poll', 'order_confirmation', 'final_order'}:
            result['order_result'] = 'unknown'
        slot = raw.get('pickup_slot')
        if (isinstance(slot, dict) and slot.get('store_id') == plan.get('store_id')
                and slot.get('time_zone') == 'America/New_York'
                and isinstance(slot.get('date'), str) and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}', slot['date'])
                and all(isinstance(slot.get(key), str) and re.fullmatch(r'(?:0?[1-9]|1[0-2]):[0-5][0-9]\s*(?:AM|PM)', slot[key], re.I) for key in ('start', 'end'))):
            result['pickup_slot'] = {key: slot[key] for key in ('store_id', 'date', 'start', 'end', 'time_zone')}
        failure = raw.get('failure') if isinstance(raw.get('failure'), dict) else {}
        classification = 'order_accepted' if accepted else 'review_ready' if status == 'review_ready' else 'blocked' if status == 'blocked' else failure.get('classification', failure.get('kind', 'unknown_error'))
        event = {'classification': classification, 'source': 'apple' if accepted or status == 'review_ready' else failure.get('source', 'local'),
                 'stage': stage, 'profile_alias': self.profile_alias, 'timings_ms': result['timings_ms'],
                 'review_confirmed': status == 'review_ready', 'order_result': result.get('order_result', 'not_submitted'),
                 'order_placed': accepted, 'apple_response_confirmed': accepted,
                 'apple_decline': failure.get('apple_decline') is True}
        if 'http_status' in failure:
            event['http_status'] = failure['http_status']
        if review:
            event.update(total_cents=review['total_cents'], tax_included=True)
        return result, normalize_result(event)

    def process_job(self, job):
        """Process an already-claimed API job; raw flow output never gets logged."""
        run_id = job.get('run_id')
        if job.get('backend', 'browser') != 'api':
            return False
        if job.get('status') == 'checking_stock':
            try:
                if self.stock_probe is None:
                    raise ValueError()
                rows, errors, *_ = self.stock_probe(job)
                self.beta.test_stock_checked(run_id, rows, errors)
            except Exception:
                self.beta.test_stock_checked(run_id, {}, ['The API test stock check did not complete.'])
            return True
        try:
            plan = json.loads(job['target_json'])
            if not isinstance(plan, dict):
                raise ValueError()
            plan['run_id'] = run_id
        except (KeyError, ValueError, TypeError):
            self.beta.finish(run_id, {'status': 'blocked', 'stage': 'plan',
                'message': 'The selected API plan is unavailable.'})
            return True
        allowed = False
        if _enabled(job.get('submit_once')) and callable(self.final_grant) and not self.stop.is_set():
            try:
                allowed = self.final_grant(run_id) is True
            except Exception:
                pass
        authorized = False
        def authorize_final(review):
            nonlocal authorized
            current = self.beta.snapshot()
            if (authorized or not allowed or self.stop.is_set() or self.beta.cancelled(run_id)
                    or current.get('run_id') != run_id or current.get('status') != 'preparing'
                    or current.get('backend') != 'api' or not _enabled(current.get('submit_once'))
                    or not _safe_review(review, plan)):
                return False
            self.beta.authorize_order_once(run_id, review)
            authorized = True
            return True
        client = self._warm if self._warm_run == run_id else None
        if client is not None:
            self._warm, self._warm_run, self._warm_status = None, None, 'consumed'
        guard = BetaGuard(self.beta, run_id, self.stop)
        try:
            raw = self.flow_runner(self.root, plan, guard, profile_path=self.profile_path,
                allow_submit_once=allowed, authorize_final=authorize_final, prepared_client=client)
        except Exception:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass
            raw = {'status': 'needs_attention', 'stage': 'interrupted',
                   'order_result': 'unknown',
                   'failure': {'classification': 'unknown_error', 'source': 'local'}}
        result, event = self._result(raw, plan)
        # No failure member is passed: this service records one result receipt,
        # avoiding a second failure-only alert from CheckoutBeta.finish.
        self.beta.finish(run_id, result)
        if result['status'] == 'order_accepted':
            # CheckoutBeta independently checks the durable final-order journal.
            # A flow return alone must never produce a success notification.
            saved = self.beta.snapshot()
            if saved.get('run_id') != run_id or saved.get('status') != 'order_accepted':
                event = normalize_result({**event, 'classification': 'unknown_error',
                    'order_result': 'unknown', 'order_placed': None, 'apple_response_confirmed': False})
        record = getattr(self.beta, 'record_result', self.beta.record_failure)
        record(run_id, event)
        return True

    def deliver_result_once(self):
        if not callable(self.send_webhook):
            return False
        event = self.beta.claim_failure_alert()
        if not event:
            return False
        try:
            message_id = self.send_webhook(event['payload'])
            if not isinstance(message_id, str) or not re.fullmatch(r'[0-9]{1,30}', message_id):
                raise _DeliveryFailure(ambiguous=True)
            self.beta.complete_failure_alert(event, message_id=message_id)
        except Exception as exc:
            ambiguous = getattr(exc, 'ambiguous', True) is not False
            retry = getattr(exc, 'retry_after', 0)
            retry = retry if type(retry) in (int, float) and math.isfinite(retry) and retry >= 0 else 0
            self.beta.complete_failure_alert(event, failure=_DeliveryFailure(ambiguous=ambiguous,
                terminal=not ambiguous and event.get('attempts', 1) >= self.max_alert_attempts,
                retry_after=min(3600, retry)))
        return True

    def step(self):
        if self.stop.is_set():
            self._discard_warm()
            return False
        job = None
        try:
            self.prepare_warm_session()
            current = self.beta.snapshot()
            worked = False
            if current.get('backend', 'browser') == 'api':
                job = self.beta.claim()
                if job:
                    worked = self.process_job(job)
            return self.deliver_result_once() or worked
        except Exception:
            # step() is also embedded in monitor's service thread. A malformed
            # callback or private exception must never terminate that thread.
            if isinstance(job, dict) and job.get('run_id'):
                run_id = job['run_id']
                try:
                    self.beta.finish(run_id, {'status': 'needs_attention', 'stage': 'interrupted',
                        'message': 'The API beta stopped for inspection; requests will not be replayed.'})
                    record = getattr(self.beta, 'record_result', None)
                    if record:
                        record(run_id, normalize_result({'classification': 'unknown_error', 'source': 'local',
                            'stage': 'interrupted', 'order_result': 'unknown', 'profile_alias': self.profile_alias}))
                except Exception:
                    pass
            return False

    def close(self):
        self._discard_warm()

    def run(self):
        try:
            while not self.stop.is_set():
                try:
                    self.step()
                except Exception:
                    # Durable intents/outbox remain the recovery authority. Never
                    # log arbitrary exceptions that may contain private inputs.
                    pass
                self.stop.wait(0.25)
        finally:
            self._discard_warm()
