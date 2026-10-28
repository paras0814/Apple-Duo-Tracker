"""Local NJ + Manhattan pickup monitor. Python 3.11+ with private verified HTTP transport."""
from __future__ import annotations

import argparse
import concurrent.futures
import copy
import hashlib
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from pathlib import Path
import re
import signal
import sqlite3
import threading
import time
from urllib import error, parse, request
import uuid
from checkout_beta import CheckoutBeta, BetaError, TEST_PART, TEST_PRODUCTS, run_browser

ROOT = Path(__file__).resolve().parent
CATALOG = json.loads((ROOT / "catalog.json").read_text(encoding="utf-8"))
PARTS = CATALOG["parts"]
STORES = {s["id"]: s for s in CATALOG["stores"]}
APPLE_URL = "https://www.apple.com/shop/retail/pickup-message"
LOG = logging.getLogger("duo")


def iso(ts=None):
    return datetime.fromtimestamp(time.time() if ts is None else ts, timezone.utc).isoformat()


def buy_url(part):
    model = PARTS[part]
    variant = f'{model["storage"].lower()}-{model["color"].lower().replace(" ", "-")}'
    return (f"https://www.apple.com/shop/buy-iphone/iphone-duo/7.6-inch-display-{variant}-unlocked"
            f"?purchaseOption=fullPrice&acpart=none&skipul=1#{part}")


def load_config(path):
    cfg = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    interval = cfg.get("interval_seconds", 5)
    if not isinstance(interval, (int, float)) or isinstance(interval, bool) or interval < 5:
        raise ValueError("interval_seconds must be at least 5")
    timeout = cfg.get("request_timeout_seconds", 4)
    if not isinstance(timeout, (int, float)) or not 0 < timeout <= 30:
        raise ValueError("request_timeout_seconds must be between 0 and 30")
    zips = cfg.get("seed_zips", ["07470", "08837", "08002"])
    if not isinstance(zips, list) or not zips or any(not isinstance(z, str) or not re.fullmatch(r"\d{5}", z) for z in zips):
        raise ValueError("seed_zips must be a nonempty list of five-digit ZIP strings")
    proxies = cfg.get("proxies", [])
    connection = cfg.get("monitor_connection", "direct")
    if connection not in ("direct", "proxy"):
        raise ValueError("monitor_connection must be direct or proxy")
    if not isinstance(proxies, list):
        raise ValueError("proxies must be a list of HTTP proxy URLs")
    for proxy in proxies:
        try:
            p = parse.urlsplit(proxy)
            if p.scheme != "http" or not p.hostname or not p.port or p.path not in ("", "/") or p.query or p.fragment:
                raise ValueError()
        except (TypeError, ValueError):
            raise ValueError("Each proxy must be http://[user:password@]host:port; percent-encode credentials") from None
    discord = cfg.get("discord", {})
    if discord.get("mode", "webhook") != "webhook":
        raise ValueError("This monitor uses a channel webhook. A true bot DM requires a bot integration.")
    webhook = discord.get("webhook_url", "")
    if webhook and not re.fullmatch(r"https://(?:discord\.com|discordapp\.com)/api(?:/v\d+)?/webhooks/\d+/[A-Za-z0-9_-]+", webhook):
        raise ValueError("webhook_url must be an HTTPS Discord incoming webhook URL")
    port = cfg.get("dashboard_port", 8765)
    if not isinstance(port, int) or not 1024 <= port <= 65535:
        raise ValueError("dashboard_port must be an integer between 1024 and 65535")
    return {**cfg, "interval_seconds": interval, "request_timeout_seconds": timeout,
            "monitor_connection": connection,
            "seed_zips": list(dict.fromkeys(zips)), "proxies": proxies,
            "discord": {"mode": "webhook", "webhook_url": webhook}, "dashboard_port": port}


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class FetchError(Exception):
    def __init__(self, message, retry_after=0, ambiguous=False, transport=False, status_code=None, route_failure=False):
        super().__init__(message)
        self.retry_after = retry_after
        self.ambiguous = ambiguous
        self.transport = transport
        self.status_code = status_code
        self.route_failure = route_failure


class ProxySession:
    """Bind every request in one verified session to the same configured route."""
    def __init__(self, session, proxy, timeout):
        self._session = session
        self._proxy = proxy
        self.timeout = timeout

    def __getattr__(self, name):
        return getattr(self._session, name)

    def request(self, *args, **kwargs):
        # The checkout client explicitly disables proxies on each request.
        # Override that only inside this monitor-owned adapter, including its
        # bootstrap requests. Checkout keeps its original direct transport.
        kwargs.pop("proxies", None)
        kwargs["proxy"] = self._proxy
        kwargs["timeout"] = min(float(kwargs.get("timeout", self.timeout)), self.timeout)
        return self._session.request(*args, **kwargs)


def retry_delay(value, now=None):
    now = time.time() if now is None else now
    try:
        return max(0, float(value))
    except (TypeError, ValueError):
        try:
            return max(0, parsedate_to_datetime(value).timestamp() - now)
        except (TypeError, ValueError, OverflowError):
            return 0


def http_json(url, *, proxy=None, payload=None, timeout=4, service="Apple"):
    # Explicit ProxyHandler({}) disables inherited environment/system proxies.
    opener = request.build_opener(request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {}), NoRedirect())
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) NJ-Duo-Monitor/1.0",
               "Accept": "application/json", "Cache-Control": "no-cache"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload).encode()
    req = request.Request(url, data=data, headers=headers)
    try:
        with opener.open(req, timeout=timeout) as response:
            body = response.read(4 * 1024 * 1024 + 1)
            if len(body) > 4 * 1024 * 1024:
                raise FetchError(f"{service} response exceeded size limit", ambiguous=data is not None)
            try:
                return json.loads(body)
            except (ValueError, UnicodeError):
                raise FetchError(f"{service} returned invalid JSON", ambiguous=data is not None) from None
    except error.HTTPError as exc:
        status_code = exc.code
        delay = retry_delay(exc.headers.get("Retry-After"))
        if exc.code == 429:
            try:
                delay = max(delay, float(json.loads(exc.read(4096)).get("retry_after", 0)))
            except (ValueError, TypeError, AttributeError):
                pass
        if service == "Apple" and (status_code == 429 or (status_code in (403, 541) and not proxy)):
            delay = max(delay, 60)
        exc.close()
        raise FetchError(f"{service} HTTP {status_code}", retry_after=delay, status_code=status_code) from None
    except (error.URLError, TimeoutError, OSError):
        # Exception text can contain a proxy URL. Never include it in logs/API.
        raise FetchError(f"{service} network/TLS/timeout failure", ambiguous=data is not None, transport=True) from None


def parse_pickup(payload, *, parts=None, product_name="iPhone Duo"):
    """Strict product and catalog/state filtering; absent fields stay unknown."""
    if not isinstance(payload, dict):
        raise FetchError("Apple response is not an object")
    if str(payload.get("head", {}).get("status", "200")) != "200":
        raise FetchError("Apple reported an unsuccessful response")
    stores = payload.get("body", {}).get("stores")
    if not isinstance(stores, list):
        raise FetchError("Apple response missing body.stores")
    rows = {}
    parts = PARTS if parts is None else parts
    for store in stores:
        if not isinstance(store, dict):
            continue
        sid = store.get("storeNumber")
        if sid not in STORES or store.get("state", "").upper() != STORES[sid]["state"]:
            continue
        availability = store.get("partsAvailability")
        if not isinstance(availability, dict):
            continue
        for part, model in parts.items():
            item = availability.get(part)
            if not isinstance(item, dict):
                continue
            regular = (item.get("messageTypes") or {}).get("regular") or {}
            title = html.unescape(str(regular.get("storePickupProductTitle", "")))
            title = re.sub(r"<[^>]*>", "", title)
            family = r"\b" + r"\s+".join(re.escape(word) for word in product_name.split()) + r"\b"
            capacity = re.fullmatch(r"(\d+)(GB|TB)", model["storage"], re.I)
            storage_pattern = r"(?<!\d)" + capacity[1] + r"\s*" + capacity[2] + r"\b" if capacity else r"(?!)"
            verified = bool(re.search(family, title, re.I)
                            and re.search(storage_pattern, title, re.I)
                            and re.search(r"\b" + re.escape(model["color"]) + r"\b", title, re.I))
            explicit_part = item.get("partNumber", part)
            display = item.get("pickupDisplay")
            valid = verified and explicit_part == part and display in ("available", "unavailable")
            rows[(sid, part)] = {"status": display if valid else "unknown", "title": title[:200],
                                 "verified": verified and explicit_part == part,
                                 "reason": None if valid else "Apple did not confirm the product or pickup status"}
    return rows


class AppleClient:
    def __init__(self, cfg, fetch=http_json, *, parts=None, product_name="iPhone Duo", verified_factory=None):
        self.cfg, self.fetch = cfg, fetch
        self.connection = cfg.get("monitor_connection", "direct")
        self.parts = PARTS if parts is None else parts
        self.product_name = product_name
        self.cooldowns = {}
        self.last_errors = {}
        self.rotation = 0
        self.proxy_health = {}
        self.preferred_proxies = {}
        self.network_lock = threading.Lock()
        self.request_count = 0
        self.failover_count = 0
        self.diagnostics = {}
        self.direct_lock = threading.RLock()
        self.verified_factory = verified_factory
        self.verified_client = None
        self.verified_setup_failed = False
        self.verified_started = False
        self.direct_renewals = 0
        self.direct_recovery_used = False
        self.proxy_clients = {}
        self.proxy_locks = {}
        self.proxy_started = set()
        self.proxy_session_resets = 0

    def _verified(self):
        if self.verified_client is not None or self.verified_setup_failed:
            return self.verified_client
        # Fixtures with a custom HTTP function remain fully offline unless they
        # explicitly inject the verified transport too.
        if self.verified_factory is None and self.fetch is not http_json:
            return None
        try:
            if self.verified_factory is None:
                from beta.apple_client import AppleClient as VerifiedClient
                factory = VerifiedClient
            else:
                factory = self.verified_factory
            run = "stock-monitor" if set(self.parts) == set(PARTS) else "stock-monitor-diagnostic"
            self.verified_client = factory(ROOT, run)
            # Bound each underlying network operation by the configured monitor
            # timeout, including bootstrap. The API client defaults to 20s.
            session = getattr(self.verified_client, "_session", None)
            if session is not None:
                budget = self.cfg["request_timeout_seconds"]
                class BudgetSession:
                    def __getattr__(self, name): return getattr(session, name)
                    def request(self, *args, **kwargs):
                        kwargs["timeout"] = min(float(kwargs.get("timeout", budget)), budget)
                        return session.request(*args, **kwargs)
                self.verified_client._session = BudgetSession()
        except Exception:
            # Optional transport setup cannot prevent the core monitor starting.
            # This fallback is still explicitly direct, never the supplied pool.
            self.verified_setup_failed = True
            self.verified_client = None
        return self.verified_client

    def _verify_direct(self, client):
        try:
            client.verify_session("https://www.apple.com", client_profile="native_compatible_emulation")
            self.verified_started = True
            self.direct_renewals += 1
        except Exception as exc:
            status = getattr(exc, "status", None)
            raise FetchError("Apple direct session verification failed", status_code=status,
                             retry_after=60 if status == 429 else 0) from None

    def _query_verified_direct(self, zip_code, url):
        with self.direct_lock:
            client = self._verified()
            if client is None:
                with self.network_lock: self.request_count += 1
                return self.fetch(url, proxy=None, timeout=self.cfg["request_timeout_seconds"])
            if not self.verified_started or not client.verification_cookie_valid("https://www.apple.com"):
                self._verify_direct(client)
            for attempt in range(2):
                with self.network_lock: self.request_count += 1
                try:
                    response = client.request("GET", url, headers={"Accept":"application/json", "Referer":"https://www.apple.com/shop/buy-iphone/iphone-duo", "Sec-Fetch-Site":"same-origin", "Sec-Fetch-Mode":"cors", "Sec-Fetch-Dest":"empty"})
                except Exception as exc:
                    raise FetchError("Apple direct network/TLS/timeout failure", transport=True,
                                     status_code=getattr(exc, "status", None)) from None
                delay = retry_delay(response.headers.get("retry-after"))
                if response.status == 200:
                    try: return response.json()
                    except Exception: raise FetchError("Apple returned invalid JSON") from None
                if response.status == 429:
                    raise FetchError("Apple HTTP 429", retry_after=max(delay, 60), status_code=429)
                if (response.status in (403, 541) and not delay and not attempt
                        and not self.direct_recovery_used):
                    self.direct_recovery_used = True
                    self._verify_direct(client)
                    continue
                raise FetchError(f"Apple HTTP {response.status}", retry_after=delay, status_code=response.status)

    def close(self):
        if self.verified_client is not None:
            self.verified_client.close()
        for client in self.proxy_clients.values():
            client.close()

    def _discard_proxy_session(self, proxy):
        # Called while holding this route's lock, so another ZIP cannot use the
        # rejected handle while it is being closed or clear a replacement jar.
        client = self.proxy_clients.pop(proxy, None)
        self.proxy_started.discard(proxy)
        if client is not None:
            try:
                discard = getattr(client, "discard_session", client.close)
                discard()
            except Exception:
                # A failed cookie write must not keep a rejected live handle.
                try: client.close()
                except Exception: pass
            self.proxy_session_resets += 1

    def _query_verified_proxy(self, proxy, url, timeout):
        # Keep injected legacy HTTP fixtures offline, as with direct mode.
        if self.verified_factory is None and self.fetch is not http_json:
            with self.network_lock: self.request_count += 1
            return self.fetch(url, proxy=proxy, timeout=timeout)
        with self.network_lock:
            route_lock = self.proxy_locks.setdefault(proxy, threading.RLock())
        with route_lock:
            client = self.proxy_clients.get(proxy)
            if client is None:
                try:
                    from beta.apple_client import AppleClient as VerifiedClient
                    factory = self.verified_factory or VerifiedClient
                    identity = hashlib.sha256(proxy.encode()).hexdigest()[:20]
                    family = "stock-monitor" if set(self.parts) == set(PARTS) else "stock-diagnostic"
                    client = factory(ROOT, family + "-proxy-" + identity)
                    client._session = ProxySession(client._session, proxy, timeout)
                    self.proxy_clients[proxy] = client
                except Exception:
                    raise FetchError("Verified proxy transport unavailable; run Setup-API.cmd", retry_after=60) from None
            client._session.timeout = timeout
            try:
                if proxy not in self.proxy_started or not client.verification_cookie_valid("https://www.apple.com"):
                    client.verify_session("https://www.apple.com", client_profile="public_v3_proof")
                    self.proxy_started.add(proxy)
                with self.network_lock: self.request_count += 1
                response = client.request("GET", url)
            except Exception as exc:
                status = getattr(exc, "status", None)
                if status is None or status in (403, 541):
                    self._discard_proxy_session(proxy)
                raise FetchError("Apple proxy session/network/TLS/timeout failure", transport=status is None,
                                 status_code=status, retry_after=60 if status == 429 else 0) from None
            delay = retry_delay(response.headers.get("retry-after"))
            if response.status == 200:
                try: return response.json()
                except Exception: raise FetchError("Apple returned invalid JSON") from None
            if response.status == 429:
                delay = max(delay, 60)
            if response.status in (403, 541):
                self._discard_proxy_session(proxy)
            raise FetchError(f"Apple HTTP {response.status}", retry_after=delay, status_code=response.status)

    def _query(self, zip_code, proxy, timeout=None):
        params = {"pl": "true", "location": zip_code,
                  **{f"parts.{i}": part for i, part in enumerate(self.parts)}}
        url = APPLE_URL + "?" + parse.urlencode(params)
        if self.connection == "direct":
            payload = self._query_verified_direct(zip_code, url)
            return parse_pickup(payload, parts=self.parts, product_name=self.product_name)
        if proxy:
            payload = self._query_verified_proxy(proxy, url, timeout or self.cfg["request_timeout_seconds"])
        else:
            with self.network_lock: self.request_count += 1
            payload = self.fetch(url, proxy=None, timeout=timeout or self.cfg["request_timeout_seconds"])
        return parse_pickup(payload, parts=self.parts, product_name=self.product_name)

    def _next_proxy(self, excluded, preferred=None):
        with self.network_lock:
            proxies = self.cfg["proxies"]
            now = time.monotonic()
            if preferred is not None and preferred not in excluded and self.proxy_health.get(preferred, (0, 0))[1] <= now:
                return preferred, proxies[preferred]
            for _ in proxies:
                index = self.rotation % len(proxies)
                self.rotation += 1
                _, until = self.proxy_health.get(index, (0, 0))
                if index not in excluded and until <= now:
                    return index, proxies[index]
            remaining = [until - now for _, until in self.proxy_health.values() if until > now]
            raise FetchError("Configured HTTP proxies are cooling down", retry_after=min(remaining, default=5), route_failure=True)

    def _query_with_failover(self, zip_code):
        if self.connection == "direct" or not self.cfg["proxies"]:
            return self._query(zip_code, None)
        deadline = time.monotonic() + self.cfg["request_timeout_seconds"]
        excluded = set()
        last_error = None
        for attempt in range(min(3, len(self.cfg["proxies"]))):
            try:
                index, proxy = self._next_proxy(excluded, self.preferred_proxies.get(zip_code) if attempt == 0 else None)
            except FetchError:
                if last_error:
                    raise last_error
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # Reserve part of the normal request budget for one alternate route.
            timeout = min(remaining, self.cfg["request_timeout_seconds"] / 2) if len(self.cfg["proxies"]) > 1 else remaining
            try:
                result = self._query(zip_code, proxy, timeout=max(0.1, timeout))
                with self.network_lock:
                    self.proxy_health.pop(index, None)
                    self.preferred_proxies[zip_code] = index
                    if attempt:
                        self.failover_count += 1
                return result
            except FetchError as exc:
                # A proxy-specific rejection is different from an explicit
                # upstream rate limit. Never manufacture ZIP Retry-After for it.
                rejected_route = exc.status_code in (403, 541)
                if rejected_route or exc.transport:
                    with self.network_lock:
                        failures = self.proxy_health.get(index, (0, 0))[0] + 1
                        delay = max(300, exc.retry_after) if rejected_route else min(300, 30 * 2 ** min(failures - 1, 4))
                        self.proxy_health[index] = (failures, time.monotonic() + delay)
                if (not exc.transport and not rejected_route) or exc.retry_after:
                    raise
                exc.route_failure = True
                last_error = exc
                excluded.add(index)
        raise last_error or FetchError("Apple request budget exhausted", transport=True, route_failure=True)

    def _batch(self, zips):
        rows, errors = {}, []
        before = self.request_count
        jobs = {}
        # Bound concurrency, but keep independent ZIP requests off the poll thread.
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            for zip_code in zips:
                failures, until = self.cooldowns.get(zip_code, (0, 0))
                if time.time() < until:
                    remaining = max(1, round(until - time.time()))
                    errors.append(f"ZIP {zip_code}: {self.last_errors.get(zip_code, 'Apple error')}; retry in {remaining}s")
                    continue
                jobs[pool.submit(self._query_with_failover, zip_code)] = zip_code
            # Iterate submission order so overlapping stores resolve deterministically.
            for future, zip_code in jobs.items():
                try:
                    result = future.result()
                    self.cooldowns.pop(zip_code, None)
                    self.last_errors.pop(zip_code, None)
                    for key, value in result.items():
                        # First valid observation wins; unknown never overwrites valid data.
                        if key not in rows or rows[key]["status"] == "unknown":
                            rows[key] = value
                except Exception as exc:
                    failures = self.cooldowns.get(zip_code, (0, 0))[0] + 1
                    route_failure = isinstance(exc, FetchError) and exc.route_failure
                    local_delay = self.cfg["interval_seconds"] if self.connection == "direct" or route_failure else min(300, 5 * 2 ** min(failures - 1, 6))
                    # Route availability is checked every normal sweep. It can
                    # recover when another ZIP finds a usable proxy; do not add
                    # a separate exponential ZIP blackout to proxy cooldowns.
                    delay = local_delay if route_failure else max(local_delay, getattr(exc, "retry_after", 0))
                    self.cooldowns[zip_code] = (failures, time.time() + delay)
                    message = str(exc) if isinstance(exc, FetchError) else "Invalid Apple response"
                    self.last_errors[zip_code] = message
                    errors.append(f"ZIP {zip_code}: {message}")
        return rows, errors, self.request_count - before

    def sweep(self):
        self.direct_recovery_used = False
        previous_failovers = self.failover_count
        rows, errors, requests = self._batch(self.cfg["seed_zips"])
        # Regional ZIPs cover NJ and NYC. Query any missing store's own ZIP
        # when Apple changes its radius/list; missing never means sold out.
        missing = [s["zip"] for sid, s in STORES.items()
                   if any((sid, part) not in rows or rows[(sid, part)]["status"] == "unknown" for part in self.parts)]
        fallback = list(dict.fromkeys(z for z in missing if z not in self.cfg["seed_zips"]))
        if fallback and rows:
            more, issues, count = self._batch(fallback)
            for key, value in more.items():
                if key not in rows or rows[key]["status"] == "unknown":
                    rows[key] = value
            errors.extend(issues)
            requests += count
        with self.network_lock:
            paused = sum(until > time.monotonic() for _, until in self.proxy_health.values())
            active = len(self.cfg["proxies"]) if self.connection == "proxy" else 0
            mode = ("verified_proxy" if self.proxy_clients else "proxy") if self.connection == "proxy" else "verified_direct" if self.verified_client is not None else "direct_unverified"
            self.diagnostics = {"proxy_count": active, "configured_proxy_count": len(self.cfg["proxies"]),
                                "monitor_connection": self.connection, "connection_mode": mode,
                                "proxies_healthy": active - paused if active else 0,
                                "proxies_cooling_down": paused if active else 0,
                                "proxy_failovers": self.failover_count - previous_failovers,
                                "direct_session_renewals": self.direct_renewals,
                                "proxy_session_resets": self.proxy_session_resets,
                                "transport_warning": "Run Setup-API.cmd for verified direct monitoring." if self.verified_setup_failed else None}
        return rows, errors, requests


class State:
    def __init__(self, db_path, cfg):
        self.cfg = cfg
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(db_path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS stock (
              store_id TEXT, part TEXT, status TEXT, changed_at TEXT, checked_at TEXT,
              title TEXT, event_id TEXT, PRIMARY KEY(store_id, part));
            CREATE TABLE IF NOT EXISTS events (
              id TEXT PRIMARY KEY, store_id TEXT, part TEXT, kind TEXT,
              started_at TEXT, ended_at TEXT, delivery TEXT DEFAULT 'pending',
              attempts INTEGER DEFAULT 0, next_attempt REAL DEFAULT 0,
              message_id TEXT, delivery_error TEXT);
        """)
        # A crashed send has an ambiguous outcome: avoid automatically duplicating it.
        self.db.execute("UPDATE events SET delivery='uncertain', delivery_error='Monitor stopped during delivery' WHERE delivery='sending'")
        self.db.commit()
        self.observed = {}
        self.status = {"last_sweep_at": None, "last_ok_at": None, "requests": 0,
                       "failures": 0, "duration_ms": 0, "errors": [], "sweeps": 0,
                       "warnings": [], "request_failures": 0, "running": True, "discord_last_error": None}
        self.stock_alert_wake = threading.Event()
        self.beta_wake = threading.Event()
        self.beta = CheckoutBeta(self.db, self.lock, STORES, PARTS, buy_url)

    def apply(self, observations, *, errors=(), requests=0, duration_ms=0, diagnostics=None):
        now = iso()
        with self.lock, self.db:
            self.observed = copy.deepcopy(observations)
            for (sid, part), observation in observations.items():
                if sid not in STORES or part not in PARTS or observation["status"] not in ("available", "unavailable"):
                    continue
                previous = self.db.execute("SELECT * FROM stock WHERE store_id=? AND part=?", (sid, part)).fetchone()
                old_status = previous["status"] if previous else None
                event_id = previous["event_id"] if previous else None
                changed = previous["changed_at"] if previous and old_status == observation["status"] else now
                if old_status != "available" and observation["status"] == "available":
                    event_id = uuid.uuid4().hex
                    kind = "initial" if previous is None else "restock"
                    self.db.execute("INSERT INTO events (id,store_id,part,kind,started_at) VALUES (?,?,?,?,?)",
                                    (event_id, sid, part, kind, now))
                    LOG.info("%s: %s / %s 256GB", kind, STORES[sid]["name"], PARTS[part]["color"])
                elif old_status == "available" and observation["status"] == "unavailable" and event_id:
                    self.db.execute("UPDATE events SET ended_at=?,delivery=CASE WHEN delivery='pending' THEN 'expired' ELSE delivery END WHERE id=?", (now, event_id))
                    event_id = None
                self.db.execute("INSERT OR REPLACE INTO stock VALUES (?,?,?,?,?,?,?)",
                                (sid, part, observation["status"], changed, now, observation["title"], event_id))
            known = sum(v["status"] in ("available", "unavailable") for v in observations.values())
            complete = known == len(STORES) * len(PARTS)
            self.status.update(last_sweep_at=now, requests=requests, request_failures=len(errors),
                               failures=0 if complete else len(errors), errors=[] if complete else list(errors),
                               warnings=list(errors) if complete else [],
                               duration_ms=round(duration_ms), sweeps=self.status["sweeps"] + 1)
            self.status.update(diagnostics or {})
            if known == len(STORES) * len(PARTS):
                self.status["last_ok_at"] = now
        self.beta.consider(observations)
        # Wake consumers only after stock and beta-selection transactions commit.
        self.stock_alert_wake.set()
        self.beta_wake.set()

    def snapshot(self):
        with self.lock:
            saved = {(r["store_id"], r["part"]): dict(r) for r in self.db.execute("SELECT * FROM stock")}
            rows = []
            for sid in STORES:
                for part in PARTS:
                    key = (sid, part)
                    previous = saved.get(key, {})
                    observed = self.observed.get(key, {})
                    rows.append({"store_id": sid, "part": part, "status": observed.get("status", "unknown"),
                                 "verified": observed.get("verified", False),
                                 "title": observed.get("title", previous.get("title", "")),
                                 "last_known": previous.get("status"), "last_checked": previous.get("checked_at"),
                                 "changed_at": previous.get("changed_at"),
                                 "reason": observed.get("reason") if observed else "Store or product missing from the latest successful responses",
                                 "buy_url": buy_url(part)})
            events = [dict(r) for r in self.db.execute("SELECT * FROM events ORDER BY started_at DESC, id DESC LIMIT 100")]
            return {"stores": list(STORES.values()), "parts": PARTS, "rows": rows, "events": events,
                    "beta": self.beta.snapshot(),
                    "status": {**self.status, "stores_seen": len({r["store_id"] for r in rows if r["status"] != "unknown"}),
                               "products_verified": sum(r["verified"] for r in rows),
                               "stores_expected": len(STORES), "products_expected": len(STORES) * len(PARTS),
                               "interval_seconds": self.cfg["interval_seconds"], "proxy_count": len(self.cfg["proxies"]) if self.cfg.get("monitor_connection", "direct") == "proxy" else 0,
                               "configured_proxy_count": len(self.cfg["proxies"]),
                               "monitor_connection": self.cfg.get("monitor_connection", "direct"),
                               "discord_configured": bool(self.cfg["discord"]["webhook_url"]),
                               "discord_mode": "private-channel webhook", "server_time": iso()}}

    def claim_alert(self):
        with self.lock, self.db:
            # Only alert for stock verified in the most recent sweep, including after restart.
            for row in self.db.execute("SELECT * FROM events WHERE delivery='pending' AND ended_at IS NULL AND next_attempt<=? ORDER BY started_at", (time.time(),)):
                key = (row["store_id"], row["part"])
                if self.observed.get(key, {}).get("status") != "available":
                    continue
                self.db.execute("UPDATE events SET delivery='sending',attempts=attempts+1 WHERE id=?", (row["id"],))
                return dict(row)
        return None

    def complete_alert(self, event, message_id=None, failure=None):
        with self.lock, self.db:
            if failure:
                delivery = "uncertain" if failure.ambiguous else "pending"
                # Stop retrying invalid/revoked webhooks until config is repaired/restarted.
                delay = max(failure.retry_after, min(300, 5 * 2 ** min(event["attempts"], 6)))
                self.db.execute("UPDATE events SET delivery=?,next_attempt=?,delivery_error=? WHERE id=?",
                                (delivery, time.time() + delay, str(failure), event["id"]))
                self.status["discord_last_error"] = str(failure)
            else:
                self.db.execute("UPDATE events SET delivery='delivered',message_id=?,delivery_error=NULL WHERE id=?", (message_id, event["id"]))
                self.status["discord_last_error"] = None


def discord_payload(event):
    sid, part = event["store_id"], event["part"]
    store, model = STORES[sid], PARTS[part]
    store_link = store.get("retail_url", "https://www.apple.com/retail/storelist/")
    product_link = buy_url(part)
    test = event["kind"] == "test"
    heading = "Connection test — not a stock alert." if test else ("Initial stock available" if event["kind"] == "initial" else "Restock detected")
    return {"username": "Duo Pickup Radar", "allowed_mentions": {"parse": []}, "embeds": [{
        "title": f'{"TEST — " if test else ""}Apple {store["name"]} · {model["storage"]} {model["color"]}',
        "description": f'**Apple {store["name"]}** · {store["city"]}, {store["state"]}\n'
                       f'iPhone Duo **{model["storage"]} · {model["color"]}**\n'
                       f'**{heading}**\nSelect this store for pickup when ordering.',
        "url": product_link, "color": 0xB4D9A2,
        "fields": [{"name": "Buy this model", "value": f'[Open iPhone Duo {model["storage"]} {model["color"]} on Apple ↗]({product_link})'},
                   {"name": "Apple store", "value": f'[Apple {store["name"]}]({store_link}) · {sid} · {store["zip"]}'},
                   {"name": "Part", "value": part, "inline": True}],
        "timestamp": event["started_at"], "footer": {"text": "Apple-confirmed 256GB · NJ + Manhattan Duo Radar"}}]}


def send_discord(cfg, payload, fetch=http_json):
    url = cfg["discord"]["webhook_url"]
    if not url:
        raise FetchError("Discord webhook is not configured")
    result = fetch(url + "?wait=true", payload=payload, timeout=8, service="Discord")
    if not isinstance(result, dict) or not result.get("id"):
        raise FetchError("Discord did not confirm a message ID", ambiguous=True)
    return str(result["id"])


def alert_loop(state, stop):
    while not stop.is_set():
        # Clear before checking the durable queue: a concurrent committed event
        # then either appears in this claim or leaves a wake pending for wait().
        state.stock_alert_wake.clear()
        if state.cfg["discord"]["webhook_url"]:
            event = state.claim_alert()
            if event:
                try:
                    mid = send_discord(state.cfg, discord_payload(event))
                    state.complete_alert(event, message_id=mid)
                    LOG.info("Discord alert delivered for %s / %s", event["store_id"], event["part"])
                except FetchError as exc:
                    state.complete_alert(event, failure=exc)
                    LOG.warning("%s", exc)
                except Exception:
                    state.complete_alert(event, failure=FetchError("Unexpected Discord delivery error", ambiguous=True))
                stop.wait(0.5)
                continue
        state.stock_alert_wake.wait(0.5)


def poll_once(state, client):
    start = time.monotonic()
    try:
        rows, errors, count = client.sweep()
    except Exception:
        rows, errors, count = {}, ["Unexpected Apple polling error"], 0
    state.apply(rows, errors=errors, requests=count, duration_ms=(time.monotonic() - start) * 1000,
                diagnostics=client.diagnostics)
    known = sum(v["status"] != "unknown" for v in rows.values())
    LOG.info("Sweep %d: %d/%d products checked; %d request(s); %d error(s)", state.status["sweeps"], known, len(STORES) * len(PARTS), count, len(errors))


def poll_loop(state, stop):
    client = AppleClient(state.cfg)
    while not stop.is_set():
        start = time.monotonic()
        poll_once(state, client)
        # Start-to-start cadence. Never overlap sweeps or burst to catch up.
        stop.wait(max(0, state.cfg["interval_seconds"] - (time.monotonic() - start)))


def beta_loop(state, stop):
    from beta.worker import APIBetaWorker

    def stock_probe(job):
        product = TEST_PRODUCTS[job.get("test_part", TEST_PART)]
        prepared = api_worker._warm if api_worker._warm_run == job["run_id"] else None
        if prepared is not None:
            try:
                response = prepared.request("GET", APPLE_URL, headers={"Accept": "application/json"},
                                            fields={"pl": "true", "location": STORES[job["preferred_store"]]["zip"] if job["preferred_store"] else "10017",
                                                    "parts.0": product["part"]})
                if response.status == 200:
                    return parse_pickup(response.json(), parts={product["part"]: {"storage": product["storage"], "color": product["color"]}}, product_name=product["model"]), []
            except Exception:
                pass
        cfg = {**state.cfg, "seed_zips": [STORES[job["preferred_store"]]["zip"] if job["preferred_store"] else "10017"]}
        client = AppleClient(cfg, parts={product["part"]: {"storage": product["storage"], "color": product["color"]}}, product_name=product["model"])
        try:
            rows, errors, _ = client._batch(cfg["seed_zips"])
            return rows, errors
        finally:
            client.close()

    def final_grant(run_id):
        with state.lock:
            row = state.beta._current()
            return bool(row and row["run_id"] == run_id and row["backend"] == "api" and row["submit_once"]
                        and not row["cancel_requested"] and row["status"] == "preparing")

    api_worker = APIBetaWorker(ROOT, state.beta, stop, stock_probe=stock_probe,
                               profile_path=ROOT / ".data/checkout-profile.local.json",
                               profile_alias="local-beta", final_grant=final_grant,
                               send_webhook=lambda payload: send_discord(state.cfg, payload))
    while not stop.is_set():
        state.beta_wake.clear()
        if state.beta.snapshot().get("backend") == "api":
            if api_worker.step():
                continue
            state.beta_wake.wait(0.05)
            continue
        job = state.beta.claim()
        if job:
            try:
                if job["status"] == "checking_stock":
                    cfg = {**state.cfg, "seed_zips": [STORES[job["preferred_store"]]["zip"] if job["preferred_store"] else "10017"]}
                    client = AppleClient(cfg, parts={TEST_PART: {"storage":"256GB", "color":"Black"}}, product_name="iPhone 17e")
                    rows, errors, _ = client._batch(cfg["seed_zips"])
                    state.beta.test_stock_checked(job["run_id"], rows, errors)
                else:
                    run_browser(ROOT, state.beta, job, stop)
            except Exception:
                state.beta.finish(job["run_id"], {"status":"needs_attention", "stage":"interrupted", "message":"The beta stopped unexpectedly. Inspect the beta browser bag before resetting; stock monitoring continues."})
                LOG.warning("Checkout beta stopped; run %s needs attention", job["run_id"])
        state.beta_wake.wait(0.05)


def handler_for(state, shutdown=None):
    assets = {"/": ("index.html", "text/html; charset=utf-8"),
              "/app.js": ("app.js", "text/javascript; charset=utf-8"),
              "/bookmarklet.js": ("bookmarklet.js", "text/javascript; charset=utf-8"),
              "/style.css": ("style.css", "text/css; charset=utf-8")}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            origin = self.headers.get("Origin")
            expected_origin = f"http://127.0.0.1:{self.server.server_port}"
            beta_routes = {"/api/beta/arm", "/api/beta/test", "/api/beta/stop", "/api/beta/reset"}
            if (self.path not in beta_routes | {"/api/stop"}
                    or self.headers.get("Content-Type") != "application/json"
                    or origin not in (None, expected_origin)):
                self.send_error(403)
                return
            if self.path in beta_routes:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 4096:
                        raise ValueError()
                    body = json.loads(self.rfile.read(length))
                    if not isinstance(body, dict):
                        raise ValueError()
                    if self.path in ("/api/beta/arm", "/api/beta/test"):
                        result = state.beta.arm(is_test=self.path.endswith("/test"), store_id=body.get("store_id"), part=body.get("part"),
                                                backend=body.get("backend", "api"), submit_once=body.get("submit_once", False),
                                                test_part=body.get("test_part", TEST_PART))
                    elif self.path.endswith("/stop"):
                        result = state.beta.stop()
                    else:
                        result = state.beta.reset(body.get("bag_cleared"))
                    data = json.dumps(result).encode()
                    status_code = 200
                    state.beta_wake.set()
                except BetaError as exc:
                    data = json.dumps({"error":str(exc)}).encode()
                    status_code = 409
                except (ValueError, TypeError):
                    data = b'{"error":"Invalid beta request"}'
                    status_code = 400
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                return
            if shutdown is None:
                self.send_error(403)
                return
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
            shutdown()

        def do_GET(self):
            path = parse.urlsplit(self.path).path
            if path in ("/api/state", "/api/history", "/api/health"):
                snapshot = state.snapshot()
                value = snapshot if path == "/api/state" else ({"events": snapshot["events"]} if path == "/api/history" else snapshot["status"])
                body = json.dumps(value).encode()
                mime = "application/json"
            elif path in assets:
                name, mime = assets[path]
                body = (ROOT / "web" / name).read_bytes()
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config.local.json")
    parser.add_argument("--once", action="store_true", help="One Apple sweep without sending Discord alerts")
    parser.add_argument("--test-discord", action="store_true", help="Send one clearly labeled connection-test message")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        cfg = load_config(args.config)
    except (ValueError, OSError):
        # JSON decoder errors can echo credential-bearing lines; keep this generic.
        parser.exit(2, "Invalid or missing local configuration. Check config.local.json against config.example.json.\n")
    if args.test_discord:
        try:
            mid = send_discord(cfg, discord_payload({"kind": "test", "store_id": "R654", "part": "MK1F4LL/A", "started_at": iso()}))
            LOG.info("Discord connection test delivered (message %s)", mid)
            return
        except FetchError as exc:
            parser.exit(1, str(exc) + "\n")
    data_dir = ROOT / ".data"
    data_dir.mkdir(exist_ok=True)
    if args.once:
        state = State(":memory:", cfg)
        poll_once(state, AppleClient(cfg))
        snapshot = state.snapshot()
        print(json.dumps(snapshot, indent=2))
        return
    state = State(data_dir / "monitor.sqlite3", cfg)
    stop = threading.Event()
    def shutdown(*_):
        stop.set()
        state.stock_alert_wake.set()
        state.beta_wake.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    # Bind before starting workers: a second process cannot send duplicate alerts.
    try:
        server = ThreadingHTTPServer(("127.0.0.1", cfg["dashboard_port"]), handler_for(state, shutdown))
    except OSError:
        parser.exit(1, "Dashboard port is already in use. The monitor may already be running.\n")
    (data_dir / "monitor.pid").write_text(str(__import__("os").getpid()))
    # Keep this monitor's Windows thread awake while running. The display may
    # turn off normally; stopping the process releases this nonpersistent lease.
    awake = False
    if __import__("os").name == "nt":
        try:
            import ctypes
            awake = bool(ctypes.windll.kernel32.SetThreadExecutionState(0x80000001))
        except (AttributeError, OSError):
            pass
    state.status["sleep_inhibited"] = awake
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    workers = [threading.Thread(target=fn, args=(state, stop), daemon=True) for fn in (poll_loop, alert_loop, beta_loop)]
    for worker in workers:
        worker.start()
    LOG.info("NJ + Manhattan Duo Radar: http://127.0.0.1:%d (keep this process running)", cfg["dashboard_port"])
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        stop.set()
        state.stock_alert_wake.set()
        state.beta_wake.set()
        server.server_close()
        for worker in workers:
            worker.join(timeout=12)
        if awake:
            ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
        (data_dir / "monitor.pid").unlink(missing_ok=True)


if __name__ == "__main__":
    main()
