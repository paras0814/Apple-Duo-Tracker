from contextlib import contextmanager
import copy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib import parse, request, error

import monitor as m


def config():
    return {"interval_seconds": 5, "request_timeout_seconds": 1,
            "monitor_connection": "proxy",  # Legacy pool fixtures opt in explicitly.
            "seed_zips": ["07470", "08837", "08002"], "proxies": [],
            "discord": {"mode": "webhook", "webhook_url": "https://discord.com/api/webhooks/123/local_test_secret"},
            "dashboard_port": 8765}


def payload(status="unavailable", ids=None):
    stores = []
    for sid in (ids or m.STORES):
        s = m.STORES[sid]
        stores.append({"storeNumber": sid, "state": s["state"], "partsAvailability": {
            part: {"partNumber": part, "pickupDisplay": status,
                   "messageTypes": {"regular": {"storePickupProductTitle":
                       f'iPhone Duo {model["storage"]} {model["color"]}'}}}
            for part, model in m.PARTS.items()}})
    return {"head": {"status": "200"}, "body": {"stores": stores}}


def observations(status):
    return m.parse_pickup(payload(status))


@contextmanager
def serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        worker.join()


class ProductTests(unittest.TestCase):
    def test_catalog_has_exact_two_256gb_parts_and_nj_nyc_allowlist(self):
        self.assertEqual(set(m.PARTS), {"MK1F4LL/A", "MK1E4LL/A"})
        self.assertTrue(all(p["storage"] == "256GB" for p in m.PARTS.values()))
        self.assertEqual(len(m.STORES), 19)
        self.assertEqual(sum(s["state"] == "NJ" for s in m.STORES.values()), 12)
        self.assertEqual(sum(s["state"] == "NY" for s in m.STORES.values()), 7)
        self.assertTrue({"R654", "R032", "R250", "R415"} <= set(m.STORES))
        self.assertFalse({"R753", "R612", "R152", "R594", "R715"} & set(m.STORES))

    def test_apple_product_titles_verify_both_colors(self):
        result = m.parse_pickup(payload("available"))
        self.assertEqual(len(result), 38)
        self.assertTrue(all(r["status"] == "available" and r["verified"] for r in result.values()))

    def test_wrong_storage_color_model_and_part_never_trigger_stock(self):
        for title in ("iPhone Duo 25GB Night Sky", "iPhone Duo 512GB Night Sky", "iPhone Duo 1256GB Night Sky",
                      "iPhone Duo 256GB Star White", "iPhone 256GB Night Sky", ""):
            with self.subTest(title=title):
                data = payload("available", ["R755"])
                item = data["body"]["stores"][0]["partsAvailability"]["MK1F4LL/A"]
                item["messageTypes"]["regular"]["storePickupProductTitle"] = title
                self.assertEqual(m.parse_pickup(data)[("R755", "MK1F4LL/A")]["status"], "unknown")
        data = payload("available", ["R755"])
        data["body"]["stores"][0]["partsAvailability"]["MK1F4LL/A"]["partNumber"] = "MK1H4LL/A"
        self.assertEqual(m.parse_pickup(data)[("R755", "MK1F4LL/A")]["status"], "unknown")

    def test_foreign_state_and_untracked_parts_are_excluded(self):
        data = payload("available")
        data["body"]["stores"][0]["state"] = "NY"
        data["body"]["stores"][1]["partsAvailability"]["MK1H4LL/A"] = {"pickupDisplay": "available"}
        result = m.parse_pickup(data)
        self.assertNotIn(("R755", "MK1F4LL/A"), result)
        self.assertEqual(len(result), 36)

    def test_diagnostic_product_never_enters_duo_rows_and_requires_its_own_storage(self):
        part = "MHRN4LL/A"
        data = {"body":{"stores":[{"storeNumber":"R654","state":"NY","partsAvailability":{
            part:{"partNumber":part,"pickupDisplay":"available","messageTypes":{"regular":{
                "storePickupProductTitle":"iPhone 17e 256GB Black"}}}}}]}}
        self.assertEqual(m.parse_pickup(data), {})
        rows = m.parse_pickup(data,parts={part:{"storage":"256GB","color":"Black"}},product_name="iPhone 17e")
        self.assertEqual(rows[("R654",part)]["status"],"available")
        rows = m.parse_pickup(data,parts={part:{"storage":"512GB","color":"Black"}},product_name="iPhone 17e")
        self.assertEqual(rows[("R654",part)]["status"],"unknown")

    def test_missing_and_unfamiliar_status_are_unknown(self):
        for display in (None, "ineligible", "Available", "", "availableSoon"):
            data = payload("available", ["R755"])
            data["body"]["stores"][0]["partsAvailability"]["MK1F4LL/A"]["pickupDisplay"] = display
            self.assertEqual(m.parse_pickup(data)[("R755", "MK1F4LL/A")]["status"], "unknown")
        with self.assertRaises(m.FetchError):
            m.parse_pickup({"body": {}})

    def test_buy_link_uses_reference_exact_unlocked_variant(self):
        url = parse.urlsplit(m.buy_url("MK1F4LL/A"))
        self.assertTrue(url.path.endswith("/7.6-inch-display-256gb-night-sky-unlocked"))
        self.assertEqual(parse.parse_qs(url.query)["purchaseOption"], ["fullPrice"])
        self.assertEqual(parse.parse_qs(url.query)["acpart"], ["none"])
        self.assertEqual(url.fragment, "MK1F4LL/A")

    def test_discord_alert_has_clickable_model_and_store_name(self):
        embed = m.discord_payload({"store_id":"R654", "part":"MK1F4LL/A", "kind":"restock", "started_at":m.iso()})["embeds"][0]
        self.assertIn("World Trade Center", embed["title"])
        self.assertEqual(embed["url"], m.buy_url("MK1F4LL/A"))
        self.assertIn("[Apple World Trade Center](https://www.apple.com/retail/worldtradecenter/)",embed["fields"][1]["value"])


class TransitionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "state.sqlite3"
        self.state = m.State(self.db, config())

    def tearDown(self):
        self.state.db.close()
        self.temp.cleanup()

    def test_initial_available_alerts_once_per_store_and_part(self):
        self.state.apply(observations("available"))
        self.state.apply(observations("available"))
        self.assertEqual(len(self.state.snapshot()["events"]), 38)
        event = self.state.claim_alert()
        self.assertEqual(event["kind"], "initial")
        self.state.complete_alert(event, message_id="123")
        for _ in range(37):
            other = self.state.claim_alert()
            self.state.complete_alert(other, message_id="456")
        self.assertIsNone(self.state.claim_alert())

    def test_unavailable_then_available_creates_restocks_without_poll_duplicates(self):
        self.state.apply(observations("unavailable"))
        self.assertEqual(self.state.snapshot()["events"], [])
        self.state.apply(observations("available"))
        self.state.apply(observations("available"))
        self.assertEqual(len(self.state.snapshot()["events"]), 38)
        self.assertTrue(all(e["kind"] == "restock" for e in self.state.snapshot()["events"]))
        self.state.apply(observations("unavailable"))
        self.assertTrue(all(e["ended_at"] and e["delivery"] == "expired" for e in self.state.snapshot()["events"]))
        self.state.apply(observations("available"))
        self.assertEqual(len(self.state.snapshot()["events"]), 76)

    def test_network_error_does_not_rearm_an_available_item(self):
        self.state.apply(observations("available"))
        self.state.apply({}, errors=["network failed"])
        snap = self.state.snapshot()
        self.assertTrue(all(r["status"] == "unknown" and r["last_known"] == "available" for r in snap["rows"]))
        self.assertIsNone(self.state.claim_alert())
        self.state.apply(observations("available"))
        self.assertEqual(len(self.state.snapshot()["events"]), 38)

    def test_restart_preserves_dedup_and_requires_fresh_confirmation(self):
        self.state.apply(observations("available"))
        event = self.state.claim_alert()
        self.state.complete_alert(event, message_id="123")
        self.state.db.close()
        self.state = m.State(self.db, config())
        self.assertIsNone(self.state.claim_alert())
        self.state.apply(observations("available"))
        self.assertEqual(len(self.state.snapshot()["events"]), 38)
        delivered = [e for e in self.state.snapshot()["events"] if e["delivery"] == "delivered"]
        self.assertEqual(len(delivered), 1)
        self.assertNotEqual(self.state.claim_alert()["id"], event["id"])

    def test_ambiguous_delivery_is_not_automatically_duplicated(self):
        single = {("R755", "MK1F4LL/A"): observations("available")[("R755", "MK1F4LL/A")]}
        self.state.apply(single)
        event = self.state.claim_alert()
        self.state.complete_alert(event, failure=m.FetchError("Discord timeout", ambiguous=True))
        self.assertIsNone(self.state.claim_alert())
        self.assertEqual(self.state.snapshot()["events"][0]["delivery"], "uncertain")

    def test_crash_during_delivery_is_retained_as_uncertain(self):
        self.state.apply({("R755", "MK1F4LL/A"): observations("available")[("R755", "MK1F4LL/A")]})
        self.state.claim_alert()
        self.state.db.close()
        self.state = m.State(self.db, config())
        self.state.apply(observations("available"))
        self.assertEqual(sum(e["delivery"] == "uncertain" for e in self.state.snapshot()["events"]), 1)

    def test_discord_429_schedules_retry_and_redacts_configuration(self):
        self.state.apply(observations("available"))
        event = self.state.claim_alert()
        self.state.complete_alert(event, failure=m.FetchError("Discord HTTP 429", retry_after=30))
        saved = self.state.db.execute("SELECT * FROM events WHERE id=?", (event["id"],)).fetchone()
        self.assertGreaterEqual(saved["next_attempt"], time.time() + 29)
        self.assertEqual(saved["delivery"], "pending")
        self.assertNotIn("local_test_secret", json.dumps(self.state.snapshot()))

    def test_recovered_coverage_retains_request_warning_without_false_outage(self):
        issue = "ZIP 08837: Apple network/TLS/timeout failure"
        self.state.apply(observations("unavailable"), errors=[issue])
        status = self.state.snapshot()["status"]
        self.assertEqual(status["errors"], [])
        self.assertEqual(status["warnings"], [issue])
        self.assertEqual(status["request_failures"], 1)
        self.assertEqual(status["failures"], 0)
        partial = observations("unavailable")
        partial.pop(("R755", "MK1F4LL/A"))
        self.state.apply(partial, errors=[issue])
        status = self.state.snapshot()["status"]
        self.assertEqual(status["errors"], [issue])
        self.assertEqual(status["warnings"], [])
        self.assertEqual(status["failures"], 1)


class NetworkTests(unittest.TestCase):
    def test_proxy_541_without_retry_after_fails_over_without_pausing_zip(self):
        cfg = config()
        cfg["seed_zips"] = ["07470"]
        cfg["proxies"] = ["http://proxy-a.example:10000", "http://proxy-b.example:10000"]
        calls = []
        def fetch(url, **kwargs):
            calls.append(kwargs["proxy"])
            if kwargs["proxy"] == cfg["proxies"][0]:
                raise m.FetchError("Apple HTTP 541", status_code=541)
            return payload()
        client = m.AppleClient(cfg, fetch)
        rows, errors, count = client.sweep()
        self.assertEqual((len(rows), errors, count), (38, [], 2))
        self.assertNotIn("07470", client.cooldowns)
        self.assertEqual(client.diagnostics["proxies_cooling_down"], 1)
        self.assertEqual(client.diagnostics["proxy_failovers"], 1)
        self.assertEqual(client.sweep()[2], 1)
        self.assertEqual(calls.count(cfg["proxies"][0]), 1)

    def test_transport_failure_uses_another_proxy_and_cools_down_failed_route(self):
        cfg = config()
        cfg["seed_zips"] = ["07470"]
        cfg["proxies"] = ["http://proxy-a.example:10000", "http://proxy-b.example:10000", "http://proxy-c.example:10000"]
        calls = []
        def fetch(url, **kwargs):
            calls.append(kwargs["proxy"])
            if kwargs["proxy"] == cfg["proxies"][0]:
                raise m.FetchError("Apple network/TLS/timeout failure", transport=True)
            return payload()
        client = m.AppleClient(cfg, fetch)
        rows, errors, count = client.sweep()
        self.assertEqual((len(rows), errors, count), (38, [], 2))
        self.assertEqual(client.diagnostics["proxy_failovers"], 1)
        self.assertEqual(client.diagnostics["proxies_cooling_down"], 1)
        self.assertEqual(client.diagnostics["proxies_healthy"], 2)
        client.sweep()
        self.assertEqual(calls.count(cfg["proxies"][0]), 1)
        self.assertEqual(calls[-1], cfg["proxies"][1])

    def test_rejected_proxy_cools_down_but_zip_retry_after_is_respected(self):
        cfg = config()
        cfg["seed_zips"] = ["07470"]
        cfg["proxies"] = ["http://proxy-a.example:10000", "http://proxy-b.example:10000"]
        calls = []
        def fetch(url, **kwargs):
            calls.append(kwargs["proxy"])
            if kwargs["proxy"] == cfg["proxies"][0]:
                raise m.FetchError("Apple HTTP 541", retry_after=60, status_code=541)
            return payload()
        client = m.AppleClient(cfg, fetch)
        self.assertEqual(client.sweep()[2], 1)
        self.assertEqual(client.diagnostics["proxies_cooling_down"], 1)
        self.assertEqual(client.sweep()[2], 0)
        self.assertEqual(len(client._query_with_failover("08837")), 38)
        self.assertEqual(calls, cfg["proxies"])

    def test_rate_limit_does_not_switch_proxy_to_evade_backoff(self):
        cfg = config()
        cfg["seed_zips"] = ["07470"]
        cfg["proxies"] = ["http://proxy-a.example:10000", "http://proxy-b.example:10000"]
        calls = []
        def fetch(url, **kwargs):
            calls.append(kwargs["proxy"])
            raise m.FetchError("Apple HTTP 429", retry_after=60)
        client = m.AppleClient(cfg, fetch)
        self.assertEqual(client.sweep()[2], 1)
        self.assertEqual(client.sweep()[2], 0)
        self.assertEqual(len(calls), 1)
        self.assertEqual(client.diagnostics["proxy_failovers"], 0)

    def test_all_bad_proxies_stop_without_direct_fallback_or_request_storm(self):
        cfg = config()
        cfg["seed_zips"] = ["07470"]
        cfg["proxies"] = ["http://proxy-a.example:10000", "http://proxy-b.example:10000"]
        calls = []
        def fetch(url, **kwargs):
            calls.append(kwargs["proxy"])
            raise m.FetchError("Apple network/TLS/timeout failure", transport=True)
        client = m.AppleClient(cfg, fetch)
        rows, errors, count = client.sweep()
        self.assertEqual((rows, count), ({}, 2))
        self.assertEqual(len(errors), 1)
        self.assertNotIn(None, calls)
        self.assertEqual(client.diagnostics["proxies_healthy"], 0)
        self.assertEqual(client.sweep()[2], 0)

    def test_live_http_fixture_checks_both_parts_then_delivers_one_confirmed_message(self):
        captured = []
        class Fixture(BaseHTTPRequestHandler):
            def do_GET(self):
                params = parse.parse_qs(parse.urlsplit(self.path).query)
                captured.append(params)
                body = json.dumps(payload("available")).encode()
                self.send_response(200); self.end_headers(); self.wfile.write(body)
            def do_POST(self):
                captured.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200); self.end_headers(); self.wfile.write(b'{"id":"123456"}')
            def log_message(self, *_): pass
        with serve(Fixture) as server:
            base = f"http://127.0.0.1:{server.server_port}"
            with patch.object(m, "APPLE_URL", base + "/pickup"):
                rows, errors, count = m.AppleClient(config()).sweep()
            self.assertEqual((len(rows), errors, count), (38, [], 3))
            for query in captured:
                self.assertEqual(query["parts.0"], ["MK1F4LL/A"])
                self.assertEqual(query["parts.1"], ["MK1E4LL/A"])
            cfg = config(); cfg["discord"]["webhook_url"] = base + "/webhook"
            state = m.State(":memory:", cfg)
            state.apply(rows)
            event = state.claim_alert()
            mid = m.send_discord(cfg, m.discord_payload(event))
            state.complete_alert(event, message_id=mid)
            self.assertEqual(mid, "123456")
            self.assertEqual(captured[-1]["allowed_mentions"], {"parse": []})
            self.assertIn("256GB", captured[-1]["embeds"][0]["description"])
            state.db.close()

    def test_missing_store_has_own_zip_fallback(self):
        calls = []
        def fetch(url, **_):
            zip_code = parse.parse_qs(parse.urlsplit(url).query)["location"][0]
            calls.append(zip_code)
            return payload(ids=["R088"] if zip_code == "08053" else [sid for sid in m.STORES if sid != "R088"])
        rows, errors, count = m.AppleClient(config(), fetch).sweep()
        self.assertEqual(len(rows), 38)
        self.assertEqual(count, 4)
        self.assertIn("08053", calls)

    def test_apple_rate_limit_is_not_retried_during_cooldown(self):
        count = 0
        def fetch(*_, **__):
            nonlocal count
            count += 1
            raise m.FetchError("Apple HTTP 429", retry_after=60)
        client = m.AppleClient(config(), fetch)
        client.sweep()
        first_count = count
        client.sweep()
        self.assertEqual(count, first_count)

    def test_authenticated_http_proxy_receives_connect_and_errors_hide_credentials(self):
        received = []
        class Proxy(BaseHTTPRequestHandler):
            def do_CONNECT(self):
                received.append((self.path, self.headers.get("Proxy-Authorization")))
                self.send_response(407); self.end_headers()
            def log_message(self, *_): pass
        with serve(Proxy) as proxy:
            with self.assertRaises(m.FetchError) as caught:
                m.http_json("https://example.invalid/test", proxy=f"http://alice:private%40password@127.0.0.1:{proxy.server_port}", timeout=1)
        self.assertEqual(received[0][0], "example.invalid:443")
        self.assertTrue(received[0][1].startswith("Basic "))
        self.assertNotIn("alice", str(caught.exception))
        self.assertNotIn("password", str(caught.exception))

    def test_discord_json_retry_after_and_invalid_json(self):
        class Fixture(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path == "/rate":
                    self.send_response(429); self.end_headers(); self.wfile.write(b'{"retry_after":12.5}')
                else:
                    self.send_response(200); self.end_headers(); self.wfile.write(b'<html>blocked</html>')
            def log_message(self, *_): pass
        with serve(Fixture) as server:
            base = f"http://127.0.0.1:{server.server_port}"
            with self.assertRaises(m.FetchError) as caught:
                m.http_json(base + "/rate", service="Discord")
            self.assertEqual(caught.exception.retry_after, 12.5)
            with self.assertRaises(m.FetchError):
                m.http_json(base + "/bad")

    def test_dashboard_never_serves_config_or_database(self):
        state = m.State(":memory:", config())
        with serve(m.handler_for(state)) as server:
            base = f"http://127.0.0.1:{server.server_port}"
            with request.urlopen(base + "/api/state") as response:
                self.assertNotIn(b"local_test_secret", response.read())
            for path in ("/config.local.json", "/.data/monitor.sqlite3", "/../config.local.json"):
                with self.assertRaises(error.HTTPError) as caught:
                    request.urlopen(base + path)
                self.assertEqual(caught.exception.code, 404)
                caught.exception.close()
        state.db.close()


class VerifiedProxyTests(unittest.TestCase):
    class Transport:
        def __init__(self): self.calls = []; self.closed = False
        def request(self, method, url, **kwargs): self.calls.append((url, kwargs)); return None
        def close(self): self.closed = True

    class Verified:
        def __init__(self, status=200):
            self._session = VerifiedProxyTests.Transport()
            self.status = status; self.verifies = 0; self.valid = False
        def verify_session(self, origin, client_profile):
            self.verifies += 1; self.valid = True
            self._session.request('GET', origin + '/bootstrap', proxy='', proxies={}, timeout=20)
        def verification_cookie_valid(self, origin): return self.valid
        def request(self, method, url):
            self._session.request(method, url, proxy='', proxies={}, timeout=20)
            code = self.status
            class Response:
                status = code
                headers = {}
                def json(self): return payload('unavailable')
            return Response()
        def close(self): self._session.close()

    def setup_client(self, statuses=(200,)):
        cfg = config(); cfg['seed_zips'] = ['07470']
        cfg['proxies'] = ['http://user:private-password@proxy.example:' + str(10000+i) for i in range(len(statuses))]
        clients = []; names = []
        def factory(root, run):
            client = self.Verified(statuses[len(clients)]); clients.append(client); names.append(run); return client
        return cfg, clients, names, m.AppleClient(cfg, verified_factory=factory)

    def test_proxy_is_applied_to_bootstrap_and_inventory_despite_checkout_direct_defaults(self):
        cfg, clients, names, client = self.setup_client()
        rows, errors, _ = client.sweep()
        self.assertEqual((len(rows), errors), (38, []))
        client.sweep()
        self.assertEqual(clients[0].verifies, 1)
        self.assertEqual(len(clients), 1)
        for _, kwargs in clients[0]._session._session.calls:
            self.assertEqual(kwargs['proxy'], cfg['proxies'][0])
            self.assertNotIn('proxies', kwargs)
            self.assertLessEqual(kwargs['timeout'], cfg['request_timeout_seconds'])
        self.assertNotIn('private-password', ''.join(names) + json.dumps(client.diagnostics))
        self.assertEqual(client.diagnostics['connection_mode'], 'verified_proxy')
        client.close(); self.assertTrue(clients[0]._session.closed)

    def test_rejected_verified_proxy_fails_over_and_reuses_the_good_session(self):
        cfg, clients, names, client = self.setup_client((541, 200))
        rows, errors, _ = client.sweep()
        self.assertEqual((len(rows), errors), (38, []))
        self.assertEqual(client.diagnostics['proxy_failovers'], 1)
        self.assertGreater(client.proxy_health[0][1] - time.monotonic(), 299)
        client.sweep()
        self.assertEqual([c.verifies for c in clients], [1, 1])
        self.assertEqual(len(clients), 2)
        self.assertEqual(len(clients[0]._session._session.calls), 2)
        self.assertEqual(len(clients[1]._session._session.calls), 3)

    def test_expiration_renews_only_the_selected_proxy_session(self):
        cfg, clients, names, client = self.setup_client()
        client.sweep(); clients[0].valid = False; client.sweep()
        self.assertEqual(clients[0].verifies, 2)
        self.assertEqual(len(clients), 1)


class VerifiedDirectTests(unittest.TestCase):
    class FakeVerified:
        def __init__(self, responses=None):
            self.responses=list(responses or []);self.valid=False;self.verifies=0;self.calls=[];self.closed=False
        def verify_session(self, origin, client_profile):
            self.verifies+=1;self.valid=True
        def verification_cookie_valid(self, origin):return self.valid
        def request(self, method, url, headers=None):
            self.calls.append((method,url,headers))
            code,extra=self.responses.pop(0) if self.responses else (200,{})
            class Response:
                status=code;headers=extra
                def json(self):return payload("unavailable")
            return Response()
        def close(self):self.closed=True

    def setup_client(self, responses=None):
        cfg=config();cfg.pop('monitor_connection')
        cfg['proxies']=[f'http://proxy-{i}.example:10000' for i in range(17)]
        fake=self.FakeVerified(responses);created=[]
        def factory(root,run):created.append(run);return fake
        def forbidden(*args,**kwargs):raise AssertionError('The configured proxy pool must not be used')
        return cfg,fake,created,m.AppleClient(cfg,forbidden,verified_factory=factory)

    def test_default_direct_ignores_all_configured_proxies_and_reuses_one_session(self):
        cfg,fake,created,client=self.setup_client()
        rows,errors,count=client.sweep()
        self.assertEqual((len(rows),errors,count),(38,[],3))
        client.sweep()
        self.assertEqual((fake.verifies,len(created)),(1,1))
        self.assertEqual(client.diagnostics['proxy_count'],0)
        self.assertEqual(client.diagnostics['configured_proxy_count'],17)
        self.assertEqual(client.diagnostics['connection_mode'],'verified_direct')
        client.close();self.assertTrue(fake.closed)

    def test_one_541_renews_once_and_recovers_without_zip_blackout(self):
        cfg,fake,created,client=self.setup_client([(541,{}),(200,{})])
        rows,errors,count=client.sweep()
        self.assertEqual((len(rows),errors,count),(38,[],4))
        self.assertEqual(fake.verifies,2)
        self.assertEqual(client.cooldowns,{})

    def test_repeated_541_is_bounded_to_one_renewal_and_five_second_retry(self):
        cfg,fake,created,client=self.setup_client([(541,{})]*4)
        rows,errors,count=client.sweep()
        self.assertEqual((len(rows),len(errors),count),(0,3,4))
        self.assertEqual(fake.verifies,2)
        for _,until in client.cooldowns.values():self.assertLessEqual(until-time.time(),5.1)

    def test_rate_limit_and_explicit_retry_after_do_not_trigger_challenge_retry(self):
        for code,delay in ((429,120),(541,90)):
            cfg,fake,created,client=self.setup_client([(code,{'retry-after':str(delay)})])
            cfg['seed_zips']=['07470']
            rows,errors,count=client.sweep()
            self.assertEqual((rows,len(errors),count),({},1,1))
            self.assertEqual(fake.verifies,1)
            self.assertGreater(client.cooldowns['07470'][1]-time.time(),delay-1)

    def test_expired_session_is_renewed_in_place(self):
        cfg,fake,created,client=self.setup_client();client.sweep();fake.valid=False;client.sweep()
        self.assertEqual((fake.verifies,len(created)),(2,1))

    def test_missing_optional_transport_falls_back_direct_without_credentials_or_boot_crash(self):
        cfg=config();cfg['monitor_connection']='direct';cfg['proxies']=['http://unused.example:10000'];calls=[]
        def missing(*args):raise OSError('PRIVATE_SETUP_DETAIL')
        def direct(url,**kwargs):calls.append(kwargs['proxy']);return payload()
        client=m.AppleClient(cfg,direct,verified_factory=missing)
        rows,errors,_=client.sweep()
        self.assertEqual((len(rows),errors),(38,[]))
        self.assertTrue(all(proxy is None for proxy in calls))
        self.assertEqual(client.diagnostics['proxy_count'],0)
        self.assertEqual(client.diagnostics['connection_mode'],'direct_unverified')
        self.assertNotIn('PRIVATE',json.dumps(client.diagnostics))

    def test_configuration_and_dashboard_report_selected_route_not_saved_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg=config();cfg.pop('monitor_connection');cfg['proxies']=['http://unused.example:10000']
            path=Path(tmp)/'config.json';path.write_text(json.dumps(cfg),encoding='utf8')
            loaded=m.load_config(path);self.assertEqual(loaded['monitor_connection'],'direct')
            state=m.State(Path(tmp)/'state.sqlite3',loaded)
            try:
                status=state.snapshot()['status'];self.assertEqual(status['proxy_count'],0);self.assertEqual(status['configured_proxy_count'],1)
            finally:state.db.close()
            cfg['monitor_connection']='invalid';path.write_text(json.dumps(cfg),encoding='utf8')
            with self.assertRaises(ValueError):m.load_config(path)


if __name__ == "__main__":
    unittest.main()
