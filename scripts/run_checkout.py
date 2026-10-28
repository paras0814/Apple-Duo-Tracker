"""Start one checkout beta run through the local monitor and print safe timings."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time
from urllib import error, request

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--test', action='store_true', help='Test the approved in-stock iPhone 17 White 256GB')
    parser.add_argument('--test-part', choices=('MG474LL/A', 'MHRN4LL/A'), default='MG474LL/A')
    parser.add_argument('--store', help='A watched Apple store ID; default: any watched store')
    parser.add_argument('--color', choices=('night-sky', 'star-white'))
    parser.add_argument('--submit-once', action='store_true', help='Submit ONE order using the local profile, within $2,300 including tax')
    parser.add_argument('--wait', action='store_true', help='Also wait for a Duo restock run; test runs always wait')
    parser.add_argument('--timeout', type=int, default=120)
    args = parser.parse_args()
    try:
        config = json.loads((ROOT / 'config.local.json').read_text(encoding='utf-8-sig'))
        port = config.get('dashboard_port', 8765)
        if type(port) is not int or not 1024 <= port <= 65535 or not 1 <= args.timeout <= 86400:
            raise ValueError()
    except (OSError, ValueError):
        parser.exit(2, 'Check config.local.json and the requested timeout.\n')
    base = f'http://127.0.0.1:{port}'
    opener = request.build_opener(request.ProxyHandler({}))

    def call(path, body=None):
        req = request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                              headers={'Content-Type': 'application/json', 'Origin': base})
        with opener.open(req, timeout=8) as response:
            return json.load(response)

    started = time.monotonic()
    try:
        state = call('/api/state')['beta']
        if not state.get('can_arm'):
            print(json.dumps({'status': state.get('status'), 'message': 'A previous run is active or needs inspection. Do not start another order.'}))
            return 2
        body = {'backend': 'api', 'submit_once': args.submit_once, 'store_id': args.store,
                'test_part': args.test_part, 'part': {'night-sky': 'MK1F4LL/A', 'star-white': 'MK1E4LL/A'}.get(args.color)}
        state = call('/api/beta/test' if args.test else '/api/beta/arm', body)
        run_id = state['run_id']
        print(json.dumps({'run_id': run_id, 'status': state['status'], 'submit_once': args.submit_once}), flush=True)
        if not args.test and not args.wait:
            return 0
        previous = None
        while time.monotonic() - started < args.timeout:
            state = call('/api/state')['beta']
            if state.get('run_id') != run_id:
                print(json.dumps({'status': 'inspection_required', 'message': 'The active run changed; no second command was sent.'}))
                return 2
            marker = (state.get('status'), state.get('stage'))
            if marker != previous:
                print(json.dumps({'status': marker[0], 'stage': marker[1], 'elapsed_ms': round((time.monotonic() - started) * 1000)}), flush=True)
                previous = marker
            if marker[0] not in {'armed', 'checking_stock', 'checking_stock_running', 'queued', 'preparing'}:
                alerts = state.get('failure_alerts', {})
                if (not alerts and state.get('target')) or alerts.get('pending', 0) or alerts.get('sending', 0):
                    time.sleep(0.1)
                    continue
                print(json.dumps({'run_id': run_id, 'status': state.get('status'), 'stage': state.get('stage'),
                                  'elapsed_ms': round((time.monotonic() - started) * 1000),
                                  'timings_ms': state.get('result', {}).get('timings_ms', {}),
                                  'result_webhook': alerts, 'order_submission_attempted': state.get('order_submission_attempted')}), flush=True)
                return 0 if marker[0] in {'order_accepted', 'review_ready'} else 1
            time.sleep(0.05)
        print(json.dumps({'run_id': run_id, 'status': 'still_running', 'message': 'Timed out waiting. Inspect the dashboard; do not repeat the checkout command.'}))
        return 2
    except error.HTTPError as exc:
        print(json.dumps({'status': 'local_request_rejected', 'http_status': exc.code, 'message': 'Inspect the dashboard. No automatic command retry was performed.'}))
        exc.close()
        return 2
    except (OSError, ValueError, KeyError):
        print(json.dumps({'status': 'local_connection_unknown', 'message': 'Inspect the dashboard before retrying; an order request may still be running.'}))
        return 2


if __name__ == '__main__':
    sys.exit(main())
