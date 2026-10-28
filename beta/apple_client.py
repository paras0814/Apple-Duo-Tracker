"""Private, direct Apple API sessions and strict cart validation.

No automatic request retries, order submission, profile or payment storage.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
import http.cookiejar
import importlib
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit

MAX_BODY = 8 * 1024 * 1024
MAX_TOTAL_CENTS = 230_000
PARTS = {"MK1F4LL/A": ("iPhone Duo", "256GB", "Night Sky"),
         "MK1E4LL/A": ("iPhone Duo", "256GB", "Star White"),
         "MHRN4LL/A": ("iPhone 17e", "256GB", "Black"),
         "MG474LL/A": ("iPhone 17", "256GB", "White")}
BASE_HEADERS = {
    "user-agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
    "sec-ch-ua":'"Chromium";v="152", "Google Chrome";v="152", "Not_A Brand";v="99"',
    "sec-ch-ua-platform":'"Windows"', "sec-ch-ua-mobile":"?0",
    "accept-encoding":"gzip, deflate, br, zstd", "accept-language":"en-US,en;q=0.9"}
_IMPORT_LOCK = threading.Lock()


class APIError(ValueError):
    def __init__(self, stage, message, status=None, *, ambiguous=False):
        super().__init__(message)
        self.stage, self.status, self.ambiguous = stage, status, ambiguous


@dataclass(frozen=True)
class Response:
    status: int
    url: str = field(repr=False)
    content: bytes = field(repr=False)
    headers: dict = field(default_factory=dict, repr=False)

    def json(self):
        try:
            return json.loads(self.content)
        except (ValueError, UnicodeError, RecursionError):
            raise APIError("response", "Apple did not return valid JSON.", self.status) from None


def safe_url(url):
    """Only known shopping hosts, HTTPS and the default HTTPS port."""
    if not isinstance(url, str) or len(url) > 32768 or re.search(r"[\x00-\x20\\]", url):
        raise APIError("url", "The Apple URL is invalid.")
    try:
        p = urlsplit(url)
        allowed = p.hostname == "www.apple.com" or bool(re.fullmatch(r"secure\d{0,3}\.store\.apple\.com", p.hostname or ""))
        valid = (p.scheme == "https" and allowed and not p.username and not p.password
                 and p.port in (None, 443))
    except ValueError:
        valid = False
    if not valid:
        raise APIError("url", "The request or redirect is outside the allowed Apple shopping hosts.")
    return urlunsplit((p.scheme, p.netloc, p.path, p.query, ""))


def _mutating(method, url):
    p = urlsplit(url)
    return (method not in ("GET", "HEAD") or p.path.rstrip("/") == "/shop/bag/add"
            or any(k == "add-to-cart" for k, _ in parse_qsl(p.query)))


def _new_session(root, jar):
    libraries = (root / ".runtime/api-libs").resolve()
    if not (libraries / ".pins.json").is_file():
        raise APIError("transport_setup", "Run Setup-API.cmd to install the private API transport.")
    with _IMPORT_LOCK:
        for name in ("curl_cffi", "cffi", "certifi"):
            loaded = sys.modules.get(name)
            if loaded and not Path(getattr(loaded, "__file__", "")).resolve().is_relative_to(libraries):
                raise APIError("transport_setup", "Restart the monitor to load its private API transport.")
        sys.path.insert(0, str(libraries))
        try:
            requests = importlib.import_module("curl_cffi.requests")
            certifi = importlib.import_module("certifi")
            return requests.Session(cookies=jar, headers=BASE_HEADERS, default_headers=False,
                                    impersonate="chrome152", trust_env=False,
                                    proxies={}, allow_redirects=False, timeout=20,
                                    verify=certifi.where(), retry=0, debug=False)
        except Exception:
            raise APIError("transport_setup", "The private API transport could not load. Run Setup-API.cmd.") from None
        finally:
            sys.path.remove(str(libraries))


class AppleClient:
    """One run's cookie jar; all requests bypass environment/system HTTP proxies."""
    def __init__(self, root, run_id, *, session_factory=None):
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", run_id):
            raise APIError("session", "Invalid API run identity.")
        self.root = Path(root).resolve()
        folder = self.root / ".data/api-runs"
        if not folder.resolve().is_relative_to(self.root):
            raise APIError("session", "The private session directory is outside this project.")
        folder.mkdir(parents=True, exist_ok=True)
        self.cookie_file = folder / (run_id + ".cookies")
        if self.cookie_file.is_symlink():
            raise APIError("session", "The private session file is not a regular local file.")
        self._jar = http.cookiejar.MozillaCookieJar(str(self.cookie_file))
        if self.cookie_file.exists():
            try:
                self._jar.load(ignore_discard=True, ignore_expires=False)
            except (OSError, ValueError, http.cookiejar.LoadError):
                raise APIError("session", "The private API session could not be loaded.") from None
        self._lock = threading.RLock()
        self.timing_file = folder / (run_id + ".timings.jsonl")
        if session_factory is None:
            self._session = _new_session(self.root, self._jar)
        else:
            # Explicit dependency injection for offline fixtures only.
            self._session = session_factory(cookies=self._jar, headers=BASE_HEADERS, default_headers=False,
                                            impersonate="chrome152", trust_env=False,
                                            proxies={}, allow_redirects=False, timeout=20,
                                            verify=True, retry=0, debug=False)

    def _save(self):
        tmp = self.cookie_file.with_suffix(".cookies.tmp")
        if tmp.is_symlink():
            raise APIError("session", "The private session file could not be saved.")
        try:
            # Save the CURRENT jar snapshot; never merge an old persisted jar
            # back into it, which would resurrect deleted/rotated cookies.
            self._jar.clear_expired_cookies()
            self._jar.save(str(tmp), ignore_discard=True, ignore_expires=False)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.cookie_file)
        except OSError:
            raise APIError("session", "The private API session could not be saved.") from None

    @staticmethod
    def _headers(headers):
        if headers is None:
            return {}
        if not isinstance(headers, dict) or len(headers) > 64:
            raise APIError("headers", "Invalid Apple request headers.")
        result = {}
        for name, value in headers.items():
            if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+", name)
                    or not isinstance(value, str) or len(value) > 32768 or re.search(r"[\x00-\x1f\x7f]", value)
                    or name.lower() in {"host", "cookie", "authorization", "proxy-authorization", "content-length", "connection"}):
                raise APIError("headers", "Invalid or forbidden Apple request header.")
            if name.lower() in {"origin", "referer"}:
                safe_url(value)
            result[name.lower()] = value
        return result

    @staticmethod
    def _request_headers(method, url, provided):
        path = urlsplit(url).path
        beacon = path == "/shop/beacon/atb"
        api = (method == "POST" or "/shop/shld/" in path or path.rstrip("/") == "/shop/bag/add"
               or path.startswith(("/shop/retail/pickup-message", "/shop/bagx", "/shop/checkoutx", "/shop/signInx"))
               or any(k == "_a" for k, _ in parse_qsl(urlsplit(url).query))
               or "application/json" in provided.get("accept", ""))
        result = {**BASE_HEADERS, "sec-fetch-site":"same-origin"}
        if beacon:
            result.update({"sec-fetch-mode":"no-cors", "sec-fetch-dest":"image",
                           "accept":"image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8"})
        elif api:
            result.update({"sec-fetch-mode":"cors", "sec-fetch-dest":"empty", "priority":"u=1, i",
                           "accept":"application/json, text/plain, */*"})
        else:
            result.update({"sec-fetch-mode":"navigate", "sec-fetch-dest":"document", "sec-fetch-user":"?1",
                           "upgrade-insecure-requests":"1",
                           "accept":"text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"})
        result.update(provided)
        if beacon or api:
            result.pop("sec-fetch-user", None)
            result.pop("upgrade-insecure-requests", None)
        return result

    def request(self, method, url, headers=None, fields=None):
        method = str(method).upper()
        if method not in {"GET", "POST", "HEAD"}:
            raise APIError("request", "Unsupported Apple request method.")
        url = safe_url(url)
        headers = self._headers(headers)
        body = None
        if fields is not None:
            if isinstance(fields, dict):
                if len(fields) > 128 or not all(isinstance(k, str) and isinstance(v, (str, int)) and not isinstance(v, bool)
                                                for k, v in fields.items()):
                    raise APIError("request", "Invalid Apple form fields.")
                body = urlencode(fields).encode("utf8")
            elif isinstance(fields, bytes) and method == "POST":
                body = fields
            else:
                raise APIError("request", "Invalid Apple request body.")
            if len(body) > 131072:
                raise APIError("request", "The Apple form is too large.")
            if method in {"GET", "HEAD"}:
                p = urlsplit(url)
                query = p.query + ("&" if p.query and body else "") + body.decode("ascii")
                url = safe_url(urlunsplit((p.scheme, p.netloc, p.path, query, "")))
                body = None
            else:
                if not any(k.lower() == "content-type" for k in headers):
                    headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
        with self._lock:
            mutated = _mutating(method, url)
            for hop in range(6):
                try:
                    network_started = time.monotonic()
                    raw = self._session.request(method, url, headers=self._request_headers(method, url, headers), data=body,
                                                allow_redirects=False, proxy="", proxies={},
                                                timeout=20, verify=self._session.verify)
                except Exception:
                    raise APIError("request", "The Apple request did not complete. It was not retried.",
                                   ambiguous=mutated) from None
                try:
                    content = bytes(raw.content)
                    status = int(raw.status_code)
                    response_url = safe_url(str(raw.url))
                    if len(content) > MAX_BODY:
                        raise APIError("response", "The Apple response exceeded the size limit.", status, ambiguous=mutated)
                    response_headers = {str(k).lower(): str(v) for k, v in raw.headers.items()
                                        if str(k).lower() not in {"set-cookie", "cookie", "authorization"}}
                    self._save()
                    try:
                        elapsed = getattr(raw, "elapsed", None)
                        action = dict(parse_qsl(urlsplit(url).query)).get("_a", "")
                        timing = {"method": method, "path": urlsplit(url).path,
                                  "action": action if re.fullmatch(r"[A-Za-z0-9_]{0,80}", action) else "",
                                  "status": status, "client_ms": round((time.monotonic()-network_started)*1000),
                                  "network_ms": round(elapsed.total_seconds()*1000) if hasattr(elapsed, "total_seconds") else None}
                        with self.timing_file.open("a", encoding="utf8") as trace:
                            trace.write(json.dumps(timing) + "\n")
                    except Exception:
                        pass
                except APIError as exc:
                    exc.ambiguous = exc.ambiguous or mutated
                    raise
                except Exception:
                    raise APIError("response", "The Apple response could not be read.", ambiguous=mutated) from None
                location = response_headers.get("location")
                if status not in {301, 302, 303, 307, 308} or not location:
                    return Response(status, response_url, content, response_headers)
                if hop == 5:
                    raise APIError("redirect", "Apple returned too many redirects.", status, ambiguous=mutated)
                try:
                    destination = safe_url(urljoin(response_url, location))
                except APIError:
                    raise APIError("redirect", "Apple redirected outside the allowed shopping hosts.", status,
                                   ambiguous=mutated) from None
                if ((status in {307, 308} and method not in {"GET", "HEAD"})
                        or _mutating("GET", destination)):
                    raise APIError("redirect", "The redirect would repeat a cart or form mutation; inspect the run.",
                                   status, ambiguous=mutated)
                if status == 303 or (status in {301, 302} and method == "POST"):
                    method, body = "GET", None
                    headers = {k:v for k,v in headers.items() if k.lower() != "content-type"}
                if urlsplit(destination).hostname != urlsplit(url).hostname:
                    # Do not carry opaque form/API tokens to a different host.
                    headers = {k:v for k,v in headers.items() if k.lower() == "accept"}
                url = destination
            raise APIError("redirect", "The Apple redirect chain could not be completed.", ambiguous=mutated)

    def get_cookie(self, name, url="https://www.apple.com/shop/bag"):
        safe_url(url)
        p = urlsplit(url)
        matches = []
        with self._lock:
            for cookie in self._jar:
                domain = cookie.domain.lstrip(".")
                domain_match = p.hostname == domain or (cookie.domain_specified and p.hostname.endswith("." + domain))
                cp = cookie.path or "/"
                path_match = p.path == cp or (p.path.startswith(cp) and (cp.endswith("/") or p.path[len(cp):].startswith("/")))
                if (cookie.name == name and domain_match and path_match
                        and not cookie.is_expired() and domain in {"apple.com", "store.apple.com", "www.apple.com", p.hostname}):
                    matches.append(cookie)
            if not matches:
                return None
            matches.sort(key=lambda c: (len(c.path or "/"), c.domain.lstrip(".") == p.hostname), reverse=True)
            return matches[0].value

    def atb_token(self):
        raw = self.get_cookie("as_atb")
        pieces = unquote(raw or "").split("|")
        if len(pieces) < 3 or not pieces[2] or len(pieces[2]) > 1024:
            raise APIError("cart_token", "Apple did not provide an add-to-bag token.")
        return pieces[2]

    def verification_cookie_valid(self, origin="https://www.apple.com"):
        value = self.get_cookie("shld_bt_ck", safe_url(origin.rstrip("/") + "/shop/bag"))
        pieces = (value or "").split("|")
        try:
            return len(pieces) >= 2 and bool(pieces[0]) and int(pieces[1]) > time.time()
        except ValueError:
            return False

    def verify_session(self, origin="https://www.apple.com", client_profile="native_compatible_emulation"):
        # Import lazily so orchestration may import this module without a cycle.
        from beta.api import DirectAppleSession, PreflightError
        origin = safe_url(origin.rstrip("/"))
        parsed = urlsplit(origin)
        if parsed.path or parsed.query or client_profile not in {"native_compatible_emulation", "public_v3_proof"}:
            raise APIError("verification_profile", "Choose a supported verification profile and Apple origin.")
        # Checkout hosts renew their descriptor in the existing cookie jar. The
        # image beacon belongs to initial www bootstrap, not checkout renewal.
        if parsed.hostname == "www.apple.com":
            beacon = self.request("GET", origin + "/shop/beacon/atb", headers={
                "Accept":"image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
                "Referer":origin + "/", "Sec-Fetch-Site":"same-origin",
                "Sec-Fetch-Mode":"no-cors", "Sec-Fetch-Dest":"image"})
            if beacon.status != 200 or not beacon.headers.get("content-type", "").startswith("image/"):
                raise APIError("session_beacon", "Apple did not accept the session beacon.", beacon.status)
        endpoint = origin + "/shop/shld/work/s3/q?jr=30"
        descriptor = self.request("GET", endpoint, headers={
            "Accept":"application/json, text/plain, */*", "Referer":origin + "/shop/bag"})
        if descriptor.status != 200:
            raise APIError("verification_descriptor", "Apple did not provide a verification descriptor.", descriptor.status)
        challenge = descriptor.json()
        if (not isinstance(challenge, dict) or type(challenge.get("challenge_version")) is not int
                or challenge["challenge_version"] != 3 or challenge.get("algorithm") != "SHA-384"
                or not all(isinstance(challenge.get(k), str) and challenge[k]
                                                     for k in ("salt", "challenge", "signature", "X"))):
            raise APIError("verification_descriptor", "Apple provided an incomplete verification descriptor.")
        try:
            proof = DirectAppleSession.solve_descriptor(challenge)
        except (PreflightError, TypeError, ValueError):
            raise APIError("verification_proof", "The bounded public verification could not be completed.") from None
        payload = {**challenge, **proof}
        if client_profile == "native_compatible_emulation":
            # Examined client-profile emulation. These are explicit fixed profile
            # fields, not claimed measurements from a real browser or device.
            payload.update(flagskv={"patSkip":True},
                           jsa={"s":30, "f":["P"], "d":{"u":"c", "p":1, "r":"oxxxxxo"}})
        posted = self.request("POST", endpoint, headers={
            "Content-Type":"text/plain;charset=UTF-8", "Accept":"application/json, text/plain, */*",
            "Origin":origin, "Referer":origin + "/shop/bag"},
            fields=json.dumps(payload, separators=(",", ":")).encode("utf8"))
        if posted.status != 200 or not self.verification_cookie_valid(origin):
            raise APIError("verification_cookie", "Apple did not issue a live verification cookie.", posted.status)
        return {"connection":"direct_no_proxy", "verification_cookie_valid":True,
                "origin":origin, "client_profile":client_profile,
                "cart_mutations":0, "payment_submitted":False}

    def discard_session(self):
        """Forget a rejected private session before replacing its transport."""
        with self._lock:
            self._jar.clear()
            try:
                self._save()
            finally:
                self._session.close()

    def close(self):
        with self._lock:
            try:
                self._save()
            finally:
                self._session.close()


def parse_init_data(content):
    if not isinstance(content, bytes) or len(content) > MAX_BODY:
        raise APIError("model", "The Apple page is missing or too large.")
    class ModelParser(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.active = False
            self.models = []
            self.parts = []
        def handle_starttag(self, tag, attrs):
            if tag == "script" and dict(attrs).get("id") == "init_data":
                self.active = True
                self.parts = []
        def handle_data(self, text):
            if self.active:
                self.parts.append(text)
        def handle_endtag(self, tag):
            if tag == "script" and self.active:
                self.active = False
                self.models.append("".join(self.parts))
    try:
        parser = ModelParser()
        parser.feed(content.decode("utf8"))
        parser.close()
        if parser.active or len(parser.models) != 1:
            raise ValueError()
        model = json.loads(parser.models[0])
        if not isinstance(model, dict):
            raise ValueError()
        return model
    except (ValueError, UnicodeError, RecursionError):
        raise APIError("model", "Apple did not provide one valid page model.") from None


def _cart(model):
    if not isinstance(model, dict) or not isinstance(model.get("shoppingCart"), dict):
        raise APIError("bag", "The Apple bag model is missing.")
    cart = model["shoppingCart"]
    if not isinstance(cart.get("d"), dict) or not isinstance(cart.get("items"), dict):
        raise APIError("bag", "The Apple bag model is incomplete.")
    items = cart["items"]
    if not isinstance(items.get("d"), dict) or type(items["d"].get("bagCount")) is not int:
        raise APIError("bag", "Apple did not confirm the bag count.")
    return cart, items


def assert_empty_bag(model):
    cart, items = _cart(model)
    if (cart["d"].get("hasItems") is not False or items["d"]["bagCount"] != 0
            or items.get("c", []) != []
            or any(isinstance(v, dict) and isinstance(v.get("d"), dict) and v["d"].get("sku")
                   for v in items.values())):
        raise APIError("bag_preflight", "The API bag is not confirmed empty. Inspect it before a new add attempt.")
    return {"bag_count":0, "confirmed_empty":True}


def _usd(value):
    if not isinstance(value, str) or not re.fullmatch(r"\$(?:\d+|\d{1,3}(?:,\d{3})+)\.\d{2}", value):
        raise APIError("bag_price", "Apple did not confirm an exact USD item price.")
    try:
        cents = int(Decimal(value[1:].replace(",", "")) * 100)
    except (InvalidOperation, ValueError):
        raise APIError("bag_price", "The Apple item price could not be verified.") from None
    if cents <= 0:
        raise APIError("bag_price", "The Apple item price is invalid.")
    return cents


def validate_bag(model, part, model_name, storage, color, max_total_cents=MAX_TOTAL_CENTS):
    if (PARTS.get(part) != (model_name, storage, color) or type(max_total_cents) is not int
            or not 0 < max_total_cents <= MAX_TOTAL_CENTS):
        raise APIError("bag", "The requested phone or spending limit is invalid.")
    cart, items = _cart(model)
    children = items.get("c")
    if (cart["d"].get("hasItems") is not True or items["d"]["bagCount"] != 1
            or not isinstance(children, list) or len(children) != 1 or not isinstance(children[0], str)):
        raise APIError("bag", "The API bag must contain exactly one phone.")
    item = items.get(children[0])
    if not isinstance(item, dict) or not isinstance(item.get("d"), dict):
        raise APIError("bag", "The Apple bag item is missing.")
    data = item["d"]
    sku_lines = [v for v in items.values() if isinstance(v, dict)
                 and isinstance(v.get("d"), dict) and v["d"].get("sku")]
    quantity_model = item.get("itemQuantity")
    item_children = item.get("c")
    if (not isinstance(quantity_model, dict) or not isinstance(quantity_model.get("d"), dict)
            or not isinstance(item_children, list) or not all(isinstance(c, str) for c in item_children)):
        raise APIError("bag", "The Apple bag item options are incomplete.")
    quantity = quantity_model["d"].get("quantity")
    expected = re.compile(r"^\s*" + re.escape(model_name) + r"\s+" + re.escape(storage)
                          + r"\s+" + re.escape(color) + r"\s*$", re.I)
    if (len(sku_lines) != 1 or data.get("sku") != part or type(quantity) is not int or quantity != 1
            or not isinstance(data.get("name"), str) or not expected.fullmatch(data["name"])):
        raise APIError("bag", "Apple did not confirm the exact one-phone configuration.")
    if (data.get("hasAppleCareInstallment") is not False
            or any(data.get(key) is not False for key in ("hasAppleCare", "hasAppleCarePlus") if key in data)
            or any("applecare" in str(key).lower() for key in item_children)
            or any("applecare" in str(key).lower() and isinstance(value, dict)
                   for key, value in item.items() if key not in {"d", "b", "f", "t", "a"})):
        raise APIError("bag_applecare", "The API bag does not confirm no AppleCare.")
    unit, total = _usd(data.get("unitPrice")), _usd(data.get("totalPrice"))
    if unit != total or total > max_total_cents:
        raise APIError("bag_price", "The Apple item total is inconsistent or exceeds the spending limit.")
    return {"part":part, "model":model_name, "storage":storage, "color":color,
            "quantity":1, "bag_count":1, "applecare":False, "currency":"USD",
            "unit_price_cents":unit, "base_price_cents":total,
            "subtotal_cents":total, "tax_included":False}
