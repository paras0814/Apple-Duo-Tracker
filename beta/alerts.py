"""Sanitized failure events for the private-channel checkout beta webhook."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from urllib.parse import urlsplit


_LABELS = {
    "payment_declined": "Apple reported a payment decline",
    "validation_failed": "Apple rejected checkout input",
    "session_error": "Apple checkout session failed",
    "network_error": "Checkout connection failed or timed out",
    "unknown_error": "Checkout stopped without a verified result",
}
_KINDS = {"field_validation": "validation_failed", "http_error": "session_error",
          "checkout_error": "unknown_error", "unconfirmed": "unknown_error"}
_SAFE_REASONS = frozenset(_LABELS.values()) | frozenset({
    "Billing submission outcome is uncertain; automatic retry is disabled.",
    "Apple rejected the billing request; no order was placed.",
    "Apple reported a payment decline during billing validation.",
    "Apple reported a billing or card validation error.",
    "Apple reported a checkout error; no order was placed.",
    "Apple billing acceptance is unconfirmed; no order was submitted.",
})


def safe_text(value, limit=180):
    """Remove likely payment/contact/session values before display or storage."""
    if not isinstance(value, str):
        return ""
    text = re.sub(r"[\x00-\x1f\x7f]", " ", value)
    text = re.sub(r"https?://\S+", "[URL removed]", text, flags=re.I)
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[email removed]", text)
    text = re.sub(r"(?<!\w)(?:\+?\d[\d ()-]{7,}\d)(?!\w)", "[number removed]", text)
    text = re.sub(r"(?i)\b(card[\s_-]*number|pan|cvv|cvc|security[\s_-]*code|token|cookie|signature|authorization|password|api[\s_-]*key)\b\s*[=:]?\s*[\"']?[^\s,;}]+", r"\1=[redacted]", text)
    text = re.sub(r"\b[A-Za-z0-9_+/=-]{32,}\b", "[value removed]", text)
    return " ".join(text.split())[:limit]


def normalize_failure(event):
    """Never infer an issuer decline from local validation or network failures."""
    event = event if isinstance(event, dict) else {}
    classification = event.get("classification", event.get("kind", "unknown_error"))
    if not isinstance(classification, str):
        classification = "unknown_error"
    classification = _KINDS.get(classification, classification)
    if classification not in _LABELS:
        classification = "unknown_error"
    source = event.get("source")
    if classification == "payment_declined" and not (source == "apple" and event.get("apple_decline") is True):
        classification = "validation_failed" if source in ("apple", "local") else "unknown_error"
    stage = event.get("stage", "checkout")
    if not isinstance(stage, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", stage):
        stage = "checkout"
    code = event.get("code", "")
    if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,79}", code):
        code = ""
    code = safe_text(code, 80)
    reason = event.get("reason")
    # Apple error text can echo names, addresses, card or authentication inputs.
    # Display only fixed adapter descriptions and separately validated error codes.
    result = {"classification": classification, "source": "apple" if source == "apple" else "local",
              "stage": stage, "code": code,
              "reason": reason if isinstance(reason, str) and reason in _SAFE_REASONS else _LABELS[classification]}
    if classification == "payment_declined":
        result["apple_decline"] = True
    total = event.get("total_cents")
    if type(total) is int and 0 < total <= 230_000:
        result["total_cents"] = total
        result["tax_included"] = event.get("tax_included") is True
    profile = safe_text(event.get("profile_name"), 64)
    if profile:
        result["profile_name"] = profile
    status = event.get("http_status")
    if type(status) is int and 100 <= status <= 599:
        result["http_status"] = status
    # Event identity excludes free text, which can contain variable server IDs.
    identity = {key: result[key] for key in ("classification", "source", "stage", "code")}
    result["event_key"] = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return result


def _apple_link(value, fallback, *, retail=False):
    if not isinstance(value, str):
        return fallback
    try:
        url = urlsplit(value)
        if (url.scheme == "https" and url.hostname == "www.apple.com" and not url.username and not url.password
                and url.path.startswith("/retail/" if retail else "/shop/buy-iphone/")
                and not re.search(r"(?i)atbtoken|signature|token|apikey|auth", url.query)
                and (not url.fragment or (not retail and re.fullmatch(r"M[A-Z0-9]{4}LL/A", url.fragment)))):
            return value
    except ValueError:
        pass
    return fallback


def checkout_failure_payload(event, target, store):
    """Build a failure receipt, without payment data, contact details, or order IDs."""
    event = normalize_failure(event)
    target = target if isinstance(target, dict) else {}
    store = store if isinstance(store, dict) else {}
    product_link = _apple_link(target.get("buy_url"), "https://www.apple.com/shop/buy-iphone")
    store_link = _apple_link(store.get("retail_url"), "https://www.apple.com/retail/storelist/", retail=True)
    model = " ".join(safe_text(target.get(key), 60) for key in ("model", "storage", "color")).strip() or "Selected iPhone"
    store_name = safe_text(store.get("name") or target.get("store_name"), 80) or "Selected pickup store"
    label = _LABELS[event["classification"]]
    reason = event["reason"]
    if event["code"]:
        reason += " (" + event["code"] + ")"
    fields = [
        {"name": "Product", "value": f"[{model}]({product_link})"},
        {"name": "Store", "value": f"[Apple {store_name}]({store_link})", "inline": True},
        {"name": "Stage", "value": event["stage"].replace("_", " "), "inline": True},
        {"name": "Result", "value": label},
        {"name": "Reason", "value": reason[:1024]},
    ]
    if "total_cents" in event:
        total = event["total_cents"]
        suffix = " incl. tax" if event["tax_included"] else " (phone; tax unverified)"
        fields.append({"name": "Price", "value": f"${total / 100:,.2f}" + suffix, "inline": True})
    if event.get("profile_name"):
        fields.append({"name": "Profile", "value": event["profile_name"], "inline": True})
    if event.get("http_status"):
        fields.append({"name": "Apple HTTP status", "value": str(event["http_status"]), "inline": True})
    return {"username": "Duo Pickup Radar", "allowed_mentions": {"parse": []}, "embeds": [{
        "title": "Beta checkout failed | Apple US", "url": product_link,
        "color": 0xED6A5A if event["classification"] == "payment_declined" else 0xE8A64A,
        "fields": fields, "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "Apple Duo Tracker • Beta • One phone • No AppleCare"},
    }]}


_RESULT_LABELS = {**_LABELS,
    "review_ready": "Phone prepared for review; no order submitted",
    "order_accepted": "Apple confirmed the order",
    "blocked": "Checkout stopped before a verified result",
}
_TIMING_STAGES = frozenset({"plan", "session_bootstrap", "session_ready", "bag_preflight", "stock",
    "product_quote", "adding", "bag_reconciliation", "cart_reconciliation", "checkout_entry",
    "checkout_verification", "guest_model", "guest_login", "checkout_start", "checkout_reconciliation",
    "pickup_mode", "store_selection", "pickup_slot", "pickup_contact", "credit_option", "zip_lookup",
    "billing_validation", "billing_validated", "review", "review_ready", "place_order", "order_submit", "order_submission",
    "order_processing", "order_poll", "order_confirmation", "final_order", "total", "warm_session"})


def sanitize_timings(value):
    """Keep fixed phase names and bounded durations, never arbitrary labels."""
    if not isinstance(value, dict):
        return {}
    return {key: amount for key, amount in value.items() if key in _TIMING_STAGES
            and type(amount) is int and 0 <= amount <= 86_400_000}


def normalize_result(event):
    """Sanitize every beta receipt and require evidence for acceptance/decline."""
    event = event if isinstance(event, dict) else {}
    kind = event.get('classification', event.get('kind', 'unknown_error'))
    kind = _KINDS.get(kind, kind) if isinstance(kind, str) else 'unknown_error'
    if kind not in _RESULT_LABELS:
        kind = 'unknown_error'
    if kind == 'order_accepted' and not (event.get('source') == 'apple'
            and event.get('order_result') == 'accepted' and event.get('apple_response_confirmed') is True
            and event.get('order_placed') is True):
        kind = 'unknown_error'
    if kind == 'review_ready' and event.get('review_confirmed') is not True:
        kind = 'unknown_error'
    if kind == 'payment_declined' and not (event.get('source') == 'apple' and event.get('apple_decline') is True):
        kind = 'unknown_error'
    base = normalize_failure({**event, 'classification': kind})
    base.update(classification=kind, reason=_RESULT_LABELS[kind],
                code='BETA_' + {'unknown_error': 'UNKNOWN_RESULT'}.get(kind, kind.upper()))
    alias = event.get('profile_alias', event.get('profile_name'))
    base.pop('profile_name', None)
    if isinstance(alias, str) and re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,31}', alias):
        base['profile_alias'] = alias
    base['timings_ms'] = sanitize_timings(event.get('timings_ms'))
    if kind == 'order_accepted':
        base.update(order_result='accepted', order_placed=True, apple_response_confirmed=True)
    elif kind == 'payment_declined':
        base.update(order_result='declined', order_placed=False, apple_decline=True)
    elif kind == 'review_ready':
        base.update(order_result='not_submitted', order_placed=False, review_confirmed=True)
    else:
        outcome = event.get('order_result')
        base['order_result'] = outcome if outcome in ('rejected', 'unknown', 'not_submitted') else 'unknown'
        base['order_placed'] = None if base['order_result'] == 'unknown' else False
    identity = {key: base[key] for key in ('classification', 'source', 'stage', 'code')}
    base['event_key'] = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return base


def checkout_result_payload(event, target, store):
    """One sanitized receipt for review, acceptance, decline or a stopped run."""
    event = normalize_result(event)
    # Reuse existing catalog-link and product/store formatting. Remove its
    # failure-specific result/reason fields, then insert the fixed result label.
    payload = checkout_failure_payload({**event, 'classification': 'unknown_error'}, target, store)
    embed = payload['embeds'][0]
    embed['title'] = {'review_ready': 'Beta review ready | Apple US',
                      'order_accepted': 'Beta order confirmed | Apple US',
                      'payment_declined': 'Beta payment declined | Apple US'}.get(
                          event['classification'], 'Beta checkout stopped | Apple US')
    embed['color'] = 0x37A66A if event['classification'] in ('review_ready', 'order_accepted') else 0xED6A5A
    embed['fields'] = [field for field in embed['fields'] if field['name'] not in ('Result', 'Reason', 'Profile')]
    embed['fields'].append({'name': 'Result', 'value': event['reason']})
    if event.get('profile_alias'):
        embed['fields'].append({'name': 'Profile alias', 'value': event['profile_alias'], 'inline': True})
    timings = event['timings_ms']
    if timings:
        ordered = [('total', timings['total'])] if 'total' in timings else []
        ordered += [(key, value) for key, value in timings.items() if key != 'total'][:8]
        text = '\n'.join(key.replace('_', ' ') + ': ' + f'{value / 1000:.3f}s' for key, value in ordered)
        embed['fields'].append({'name': 'Timings', 'value': text[:1024]})
    return payload
