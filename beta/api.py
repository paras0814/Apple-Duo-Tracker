"""Apple session diagnostics. This module cannot add items or place orders."""
from __future__ import annotations

import http.cookiejar
import hashlib
import json
import re
import ssl
import time
import urllib.error
import urllib.request


class PreflightError(ValueError):
    def __init__(self, stage, message, status=None):
        super().__init__(message)
        self.stage, self.status = stage, status


class DirectAppleSession:
    """One anonymous, direct connection context with an in-memory cookie jar."""

    def __init__(self):
        self.cookies = http.cookiejar.CookieJar()
        self._challenge = None
        self._opener = urllib.request.build_opener(
            # Explicitly ignore environment and system HTTP proxy settings.
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPCookieProcessor(self.cookies),
            urllib.request.HTTPSHandler(context=ssl.create_default_context()),
        )

    def _get(self, stage, path, accept):
        req = urllib.request.Request("https://www.apple.com" + path, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36",
            "Accept": accept, "Referer": "https://www.apple.com/",
        })
        try:
            with self._opener.open(req, timeout=15) as response:
                body = response.read(1_048_577)
                if response.status != 200:
                    raise PreflightError(stage, "Apple rejected this preflight stage.", response.status)
                if len(body) > 1_048_576:
                    raise PreflightError(stage, "The Apple response exceeded the preflight limit.")
                return response.headers.get_content_type(), body
        except urllib.error.HTTPError as exc:
            raise PreflightError(stage, "Apple rejected this preflight stage; no cart request was attempted.", exc.code) from None
        except (OSError, urllib.error.URLError):
            raise PreflightError(stage, "The Apple connection failed; no cart request was attempted.") from None

    def preflight(self):
        self._challenge = None
        report = {"connection": "direct_no_proxy", "browser_required": False,
                  "cart_mutations": 0, "payment_submitted": False, "checkout_ready": False,
                  "verification_completed": False, "stages": []}
        try:
            media, _ = self._get("session_beacon", "/shop/beacon/atb",
                                 "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8")
            if not media.startswith("image/"):
                raise PreflightError("session_beacon", "Apple did not return the expected image beacon.")
            report["stages"].append({"stage": "session_beacon", "http_status": 200})
            media, body = self._get("verification_acquisition", "/shop/shld/work/s3/q?jr=30",
                                    "application/json, text/plain, */*")
            try:
                challenge = json.loads(body) if media == "application/json" else None
            except (ValueError, UnicodeError):
                challenge = None
            if not isinstance(challenge, dict):
                raise PreflightError("verification_acquisition", "Apple did not return a JSON verification descriptor.")
            version, algorithm = challenge.get("challenge_version"), challenge.get("algorithm")
            if type(version) is not int or version != 3 or algorithm != "SHA-384":
                raise PreflightError("verification_acquisition", "The verification descriptor differs from the observed version-3 SHA-384 contract.")
            if not all(isinstance(challenge.get(key), str) and challenge[key]
                       for key in ("salt", "challenge", "signature", "X")):
                raise PreflightError("verification_acquisition", "The verification descriptor is incomplete.")
            if type(challenge.get("timeout")) is not int or challenge["timeout"] <= 0:
                raise PreflightError("verification_acquisition", "The verification descriptor has no valid lifetime.")
            # Keep neither descriptor values nor authentication material in the report.
            report["stages"].append({"stage": "verification_acquisition", "http_status": 200,
                                     "version": version, "algorithm": algorithm})
            self._challenge = challenge
            report.update(status="verification_pending", message="Session beacon and descriptor acquired. Verification, cart creation and checkout are not implemented in this preflight.")
        except PreflightError as exc:
            report.update(status="blocked", stage=exc.stage, message=str(exc))
            if exc.status is not None:
                report["http_status"] = exc.status
        return report

    @staticmethod
    def solve_descriptor(challenge):
        """Implement the hash contract observed in Apple's public verify.js."""
        if (challenge.get("challenge_version") != 3 or challenge.get("algorithm") != "SHA-384"
                or not isinstance(challenge.get("salt"), str) or len(challenge["salt"]) > 512
                or not re.fullmatch(r"[0-9a-f]{96}", str(challenge.get("challenge", "")))
                or type(challenge.get("timeout")) is not int or challenge["timeout"] <= 0):
            raise PreflightError("verification_proof", "Unsupported verification descriptor.")
        started = time.monotonic()
        deadline = started + min(challenge["timeout"] / 1000, 10)
        for number in range(1_000_000):
            if number % 128 == 0 and time.monotonic() >= deadline:
                break
            digest = hashlib.sha384((challenge["salt"] + str(number)).encode("utf-8")).digest()
            if (digest[:-4] + digest[-4:][::-1]).hex() == challenge["challenge"]:
                return {"number": number, "took": round((time.monotonic() - started) * 1000)}
        raise PreflightError("verification_proof", "No proof was found within the bounded descriptor lifetime.")

    def _submit_proof(self, body):
        req = urllib.request.Request("https://www.apple.com/shop/shld/work/s3/q?jr=30",
            data=json.dumps(body).encode("utf-8"), method="POST", headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36",
                "Content-Type": "text/plain;charset=UTF-8", "Accept": "application/json, text/plain, */*",
                "Referer": "https://www.apple.com/"})
        try:
            with self._opener.open(req, timeout=15) as response:
                if response.status != 200:
                    raise PreflightError("verification_submission", "Apple rejected the proof submission.", response.status)
        except urllib.error.HTTPError as exc:
            raise PreflightError("verification_submission", "Apple rejected the proof submission.", exc.code) from None
        except (OSError, urllib.error.URLError):
            raise PreflightError("verification_submission", "The proof submission outcome is uncertain; it will not be retried.") from None

    def verification_cookie_valid(self):
        for cookie in self.cookies:
            if cookie.name != "shld_bt_ck":
                continue
            if cookie.domain.lstrip(".") not in ("apple.com", "www.apple.com"):
                continue
            parts = cookie.value.split("|")
            try:
                if len(parts) >= 2 and parts[0] and int(parts[1]) > time.time():
                    return True
            except ValueError:
                pass
        return False

    def verify_descriptor(self):
        report = self.preflight()
        if report["status"] == "blocked":
            return report
        try:
            proof = self.solve_descriptor(self._challenge)
            # Echo the server's descriptor, including unmodeled signature fields.
            # No browser telemetry, user credentials or payment fields are fabricated.
            self._submit_proof({**self._challenge, **proof})
            if not self.verification_cookie_valid():
                raise PreflightError("verification_cookie", "Apple did not issue a cookie with a valid future expiration.")
            report.update(status="verification_cookie_received", verification_post_accepted=True,
                          verification_cookie_valid=True, solve_ms=proof["took"],
                          message="Apple acknowledged the proof and issued a live cookie. Protected cart clearance and checkout remain unverified; no cart request was made.")
        except PreflightError as exc:
            report.update(status="blocked", stage=exc.stage, message=str(exc))
            if exc.status is not None:
                report["http_status"] = exc.status
        finally:
            self._challenge = None
        return report


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-session", action="store_true", help="Submit one public verification proof; never add a phone or submit payment.")
    args = parser.parse_args()
    session = DirectAppleSession()
    result = session.verify_descriptor() if args.verify_session else session.preflight()
    print(json.dumps(result, indent=2))
    return 2 if result["status"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())
