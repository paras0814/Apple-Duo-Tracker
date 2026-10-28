import json
import unittest
from unittest.mock import patch

from beta.api import DirectAppleSession, PreflightError


class ApiPreflightTests(unittest.TestCase):
    def descriptor(self, **changes):
        return {"challenge_version": 3, "algorithm": "SHA-384", "timeout": 30,
                "salt": "PRIVATE_SALT", "challenge": "PRIVATE_CHALLENGE",
                "signature": "PRIVATE_SIGNATURE", "X": "PRIVATE_X", **changes}

    def run_fixture(self, descriptor=None, beacon_media="image/png"):
        session = DirectAppleSession()
        descriptor = self.descriptor() if descriptor is None else descriptor
        calls = []
        def get(stage, path, accept):
            calls.append(path)
            if stage == "session_beacon":
                return beacon_media, b"fixture"
            return "application/json", json.dumps(descriptor).encode()
        with patch.object(session, "_get", side_effect=get):
            return session.preflight(), calls

    def test_acquisition_does_not_claim_verified_checkout_or_disclose_descriptor(self):
        result, calls = self.run_fixture()
        self.assertEqual(calls, ["/shop/beacon/atb", "/shop/shld/work/s3/q?jr=30"])
        self.assertEqual(result["status"], "verification_pending")
        self.assertFalse(result["checkout_ready"])
        self.assertFalse(result["verification_completed"])
        self.assertFalse(result["payment_submitted"])
        self.assertEqual(result["cart_mutations"], 0)
        self.assertNotIn("PRIVATE_", json.dumps(result))

    def test_html_beacon_stops_before_next_stage(self):
        result, calls = self.run_fixture(beacon_media="text/html")
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(len(calls), 1)

    def test_changed_or_incomplete_contract_is_blocked_and_redacted(self):
        for change in [{"challenge_version": True}, {"challenge_version": 4},
                       {"algorithm": "PRIVATE_UNKNOWN"}, {"signature": ""}, {"timeout": 0}]:
            with self.subTest(change=change):
                result, _ = self.run_fixture(self.descriptor(**change))
                self.assertEqual(result["status"], "blocked")
                self.assertNotIn("PRIVATE_", json.dumps(result))

    def test_beacon_rejection_is_reported_without_retrying_or_adding(self):
        session = DirectAppleSession()
        with patch.object(session, "_get", side_effect=PreflightError("session_beacon", "Rejected", 541)) as get:
            result = session.preflight()
        self.assertEqual(get.call_count, 1)
        self.assertEqual(result["http_status"], 541)
        self.assertEqual(result["stage"], "session_beacon")
        self.assertEqual(result["cart_mutations"], 0)

    def test_session_explicitly_disables_environment_proxies(self):
        import urllib.request
        with patch("beta.api.urllib.request.build_opener") as build:
            DirectAppleSession()
        proxy = next(h for h in build.call_args.args if isinstance(h, urllib.request.ProxyHandler))
        self.assertEqual(proxy.proxies, {})

    def test_public_version_three_hash_contract_uses_final_four_byte_reversal(self):
        # Independent Node crypto vector, salt='fixture-salt', counter=7.
        challenge = self.descriptor(salt="fixture-salt", timeout=1000,
            challenge="6ab617ab616c61832541d7cf8e509f88f5b5743c9088dc952de5e4be8d4461b231c9484aa347f487b0fc5050fb61807a")
        solution = DirectAppleSession.solve_descriptor(challenge)
        self.assertEqual(solution["number"], 7)
        self.assertIs(type(solution["took"]), int)
        self.assertNotIn("salt", solution)

    def test_expired_solver_budget_does_not_submit_or_retry(self):
        challenge = self.descriptor(challenge="0" * 96, timeout=1)
        with patch("beta.api.time.monotonic", side_effect=[0, 2]):
            with self.assertRaises(PreflightError):
                DirectAppleSession.solve_descriptor(challenge)

    def test_cookie_expiration_and_domain_are_required(self):
        import http.cookiejar
        session = DirectAppleSession()
        def cookie(value, domain=".apple.com"):
            return http.cookiejar.Cookie(0, "shld_bt_ck", value, None, False, domain,
                True, True, "/", True, True, None, True, None, None, {}, False)
        with patch("beta.api.time.time", return_value=100):
            for value, domain in [("opaque|99", ".apple.com"), ("opaque|bad", ".apple.com"),
                                  ("opaque", ".apple.com"), ("|101", ".apple.com"), ("opaque|101", ".example.com")]:
                session.cookies.clear();session.cookies.set_cookie(cookie(value, domain))
                self.assertFalse(session.verification_cookie_valid())
            session.cookies.clear();session.cookies.set_cookie(cookie("opaque|101"))
            self.assertTrue(session.verification_cookie_valid())


if __name__ == "__main__":
    unittest.main()
