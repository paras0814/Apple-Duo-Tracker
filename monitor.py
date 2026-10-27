"""Local NJ + Manhattan pickup monitor. Python 3.11+, standard library only."""
from __future__ import annotations

import argparse
import concurrent.futures
import copy
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
            "seed_zips": list(dict.fromkeys(zips)), "proxies": proxies,
            "discord": {"mode": "webhook", "webhook_url": webhook}, "dashboard_port": port}


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class FetchError(Exception):
    def __init__(self, message, retry_after=0, ambiguous=False, transport=False, status_code=None):
        super().__init__(message)
        self.retry_after = retry_after
        self.ambiguous = ambiguous
        self.transport = transport
        self.status_code = status_code


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
        if service == "Apple" and status_code in (403, 429, 541):
            delay = max(delay, 60)
        exc.close()
        raise FetchError(f"{service} HTTP {status_code}", retry_after=delay, status_code=status_code) from None
    except (error.URLError, TimeoutError, OSError):
        # Exception text can contain a proxy URL. Never include it in logs/API.
        raise FetchError(f"{service} network/TLS/timeout failure", ambiguous=data is not None, transport=True) from None


def parse_pickup(payload):
    """Strict product and catalog/state filtering; absent fields stay unknown."""
    if not isinstance(payload, dict):
        raise FetchError("Apple response is not an object")
    if str(payload.get("head", {}).get("status", "200")) != "200":
        raise FetchError("Apple reported an unsuccessful response")
    stores = payload.get("body", {}).get("stores")
    if not isinstance(stores, list):
        raise FetchError("Apple response missing body.stores")
    rows = {}
    for store in stores:
        if not isinstance(store, dict):
            continue
        sid = store.get("storeNumber")
        if sid not in STORES or store.get("state", "").upper() != STORES[sid]["state"]:
            continue
        availability = store.get("partsAvailability")
        if not isinstance(availability, dict):
            continue
        for part, model in PARTS.items():
            item = availability.get(part)
            if not isinstance(item, dict):
                continue
            regular = (item.get("messageTypes") or {}).get("regular") or {}
            title = html.unescape(str(regular.get("storePickupProductTitle", "")))
            title = re.sub(r"<[^>]*>", "", title)
            verified = bool(re.search(r"\biPhone\s+Duo\b", title, re.I)
                            and re.search(r"(?<!\d)256\s*GB\b", title, re.I)
                            and re.search(r"\b" + re.escape(model["color"]) + r"\b", title, re.I))
            explicit_part = item.get("partNumber", part)
            display = item.get("pickupDisplay")
            valid = verified and explicit_part == part and display in ("available", "unavailable")
            rows[(sid, part)] = {"status": display if valid else "unknown", "title": title[:200],
                                 "verified": verified and explicit_part == part,
                                 "reason": None if valid else "Apple did not confirm the product or pickup status"}
    return rows


class AppleClient:
    def __init__(self, cfg, fetch=http_json):
        self.cfg, self.fetch = cfg, fetch
        self.cooldowns = {}
        self.last_errors = {}
        self.rotation = 0
        self.proxy_health = {}
        self.preferred_proxies = {}
        self.network_lock = threading.Lock()
        self.request_count = 0
        self.failover_count = 0
        self.diagnostics = {}

    def _query(self, zip_code, proxy, timeout=None):
        params = {"pl": "true", "location": zip_code,
                  **{f"parts.{i}": part for i, part in enumerate(PARTS)}}
        with self.network_lock:
            self.request_count += 1
        payload = self.fetch(APPLE_URL + "?" + parse.urlencode(params), proxy=proxy,
                             timeout=timeout or self.cfg["request_timeout_seconds"])
        return parse_pickup(payload)

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
            raise FetchError("Configured HTTP proxies are cooling down", retry_after=min(remaining, default=5))

    def _query_with_failover(self, zip_code):
        if not self.cfg["proxies"]:
            return self._query(zip_code, None)
        deadline = time.monotonic() + self.cfg["request_timeout_seconds"]
        excluded = set()
        last_error = None
        for attempt in range(min(2, len(self.cfg["proxies"]))):
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
                # HTTP rate limits and application errors retain ZIP backoff.
                # Only transport failures can switch routes within this check.
                if exc.status_code in (403, 541):
                    with self.network_lock:
                        failures = self.proxy_health.get(index, (0, 0))[0] + 1
                        self.proxy_health[index] = (failures, time.monotonic() + max(300, exc.retry_after))
                if not exc.transport or exc.retry_after:
                    raise
                last_error = exc
                excluded.add(index)
                with self.network_lock:
                    failures = self.proxy_health.get(index, (0, 0))[0] + 1
                    self.proxy_health[index] = (failures, time.monotonic() + min(300, 30 * 2 ** min(failures - 1, 4)))
        raise last_error or FetchError("Apple request budget exhausted", transport=True)

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
                    delay = max(min(300, 5 * 2 ** min(failures - 1, 6)), getattr(exc, "retry_after", 0))
                    self.cooldowns[zip_code] = (failures, time.time() + delay)
                    message = str(exc) if isinstance(exc, FetchError) else "Invalid Apple response"
                    self.last_errors[zip_code] = message
                    errors.append(f"ZIP {zip_code}: {message}")
        return rows, errors, self.request_count - before

    def sweep(self):
        previous_failovers = self.failover_count
        rows, errors, requests = self._batch(self.cfg["seed_zips"])
        # Regional ZIPs cover NJ and NYC. Query any missing store's own ZIP
        # when Apple changes its radius/list; missing never means sold out.
        missing = [s["zip"] for sid, s in STORES.items()
                   if any((sid, part) not in rows or rows[(sid, part)]["status"] == "unknown" for part in PARTS)]
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
            self.diagnostics = {"proxy_count": len(self.cfg["proxies"]),
                                "proxies_healthy": len(self.cfg["proxies"]) - paused,
                                "proxies_cooling_down": paused,
                                "proxy_failovers": self.failover_count - previous_failovers}
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
                    "status": {**self.status, "stores_seen": len({r["store_id"] for r in rows if r["status"] != "unknown"}),
                               "products_verified": sum(r["verified"] for r in rows),
                               "stores_expected": len(STORES), "products_expected": len(STORES) * len(PARTS),
                               "interval_seconds": self.cfg["interval_seconds"], "proxy_count": len(self.cfg["proxies"]),
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
        stop.wait(0.5)


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


def handler_for(state, shutdown=None):
    assets = {"/": ("index.html", "text/html; charset=utf-8"),
              "/app.js": ("app.js", "text/javascript; charset=utf-8"),
              "/bookmarklet.js": ("bookmarklet.js", "text/javascript; charset=utf-8"),
              "/style.css": ("style.css", "text/css; charset=utf-8")}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            origin = self.headers.get("Origin")
            expected_origin = f"http://127.0.0.1:{self.server.server_port}"
            if (self.path != "/api/stop" or shutdown is None
                    or self.headers.get("Content-Type") != "application/json"
                    or origin not in (None, expected_origin)):
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
        threading.Thread(target=server.shutdown, daemon=True).start()
    # Bind before starting workers: a second process cannot send duplicate alerts.
    try:
        server = ThreadingHTTPServer(("127.0.0.1", cfg["dashboard_port"]), handler_for(state, shutdown))
    except OSError:
        parser.exit(1, "Dashboard port is already in use. The monitor may already be running.\n")
    (data_dir / "monitor.pid").write_text(str(__import__("os").getpid()))
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    workers = [threading.Thread(target=fn, args=(state, stop), daemon=True) for fn in (poll_loop, alert_loop)]
    for worker in workers:
        worker.start()
    LOG.info("NJ + Manhattan Duo Radar: http://127.0.0.1:%d (keep this process running)", cfg["dashboard_port"])
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        stop.set()
        server.server_close()
        for worker in workers:
            worker.join(timeout=12)
        (data_dir / "monitor.pid").unlink(missing_ok=True)


if __name__ == "__main__":
    main()
