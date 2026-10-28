"""Private billing and final-review payloads; no order is sent by this module.

Private profiles and form bodies must stay in memory. Callers provide the current
Apple billing model, its published RSA key and a durable mutation-claim callback.
"""
from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
import time
import math
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping
from urllib.parse import parse_qsl, urljoin, urlsplit


class PaymentError(ValueError):
    """Safe fixed-message exception; never include a submitted value."""


@dataclass(frozen=True, repr=False)
class PaymentProfile:
    contact: Mapping[str, str] = field(repr=False)
    billing: Mapping[str, str] = field(repr=False)
    card: Mapping[str, str] = field(repr=False)
    known_dummy: bool = False

    def __repr__(self):
        return f"PaymentProfile(mode='dummy_decline_test', known_dummy={self.known_dummy}, private_fields=<redacted>)"


@dataclass(frozen=True, repr=False)
class EncryptedCard:
    card_number: str
    security_code: str

    def __repr__(self):
        return 'EncryptedCard(<redacted>)'


class PrivateBillingPayload(dict):
    def __repr__(self):
        return 'PrivateBillingPayload(<redacted>)'

    __str__ = __repr__


PUBLIC_DUMMY_NUMBERS = frozenset({
    '4242424242424242', '4000000000000002', '4000000000009995',
    '4111111111111111', '5555555555554444', '378282246310005',
    '6011111111111117',
})
B = 'checkout.billing.billingOptions.'
CC = B + 'selectedBillingOptions.creditCard.'
CI = CC + 'cardInputs.cardInput-0.'
ADDRESS = CC + 'billingAddress.address.'
BILLING_ACTION = '_a=continueFromBillingToReview&_m=checkout.billing'


def _text(obj, key, *, optional=False, limit=160):
    value = obj.get(key, '')
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
        raise PaymentError('Invalid profile field: ' + key)
    value = value.strip()
    if not value and not optional:
        raise PaymentError('Missing profile field: ' + key)
    return value


def validate_profile(data) -> PaymentProfile:
    """Validate formats, without claiming a declared test card is a public dummy.

    Expired cards are allowed for an explicitly requested billing-validation test.
    This function does not authorize a purchase or an order submission.
    """
    if not isinstance(data, dict) or data.get('mode') != 'dummy_decline_test':
        raise PaymentError('Billing test requires dummy_decline_test mode.')
    if any(not isinstance(data.get(k), dict) for k in ('contact', 'billing', 'card')):
        raise PaymentError('Contact, billing and card objects are required.')
    contact = {k: _text(data['contact'], k) for k in ('first_name', 'last_name', 'email', 'phone')}
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', contact['email']):
        raise PaymentError('Invalid contact email.')
    phone = re.sub(r'[ ()+.-]', '', contact['phone'])
    if not re.fullmatch(r'(?:1)?\d{10}', phone):
        raise PaymentError('Invalid US contact phone.')
    contact['phone'] = phone[-10:]
    billing = {k: _text(data['billing'], k, optional=(k == 'address_line_2'))
               for k in ('address_line_1', 'address_line_2', 'city', 'state', 'postal_code', 'country')}
    billing['country'] = billing['country'].upper()
    billing['state'] = billing['state'].upper()
    if billing['country'] != 'US' or not re.fullmatch(r'[A-Z]{2}', billing['state']):
        raise PaymentError('Only US billing addresses are supported.')
    if not re.fullmatch(r'\d{5}(?:-\d{4})?', billing['postal_code']):
        raise PaymentError('Invalid US billing postal code.')
    raw_card = data['card']
    number = _text(raw_card, 'number', limit=32).replace(' ', '').replace('-', '')
    code = _text(raw_card, 'security_code', limit=4)
    if not re.fullmatch(r'\d{12,19}', number) or not re.fullmatch(r'\d{3,4}', code):
        raise PaymentError('Invalid card number or security code format.')
    month, year = raw_card.get('expiry_month'), raw_card.get('expiry_year')
    if isinstance(month, bool) or isinstance(year, bool):
        raise PaymentError('Invalid card expiration format.')
    if not re.fullmatch(r'\d{1,2}', str(month)) or not re.fullmatch(r'\d{4}', str(year)):
        raise PaymentError('Invalid card expiration format.')
    if not 1 <= int(month) <= 12 or not 2000 <= int(year) <= 2099:
        raise PaymentError('Invalid card expiration format.')
    card = {'number': number, 'security_code': code, 'expiry_month': f'{int(month):02d}', 'expiry_year': str(year)}
    return PaymentProfile(MappingProxyType(contact), MappingProxyType(billing),
                          MappingProxyType(card), number in PUBLIC_DUMMY_NUMBERS)


def _node_path(node_path=None):
    if node_path:
        return str(node_path)
    bundled = Path(__file__).resolve().parents[1] / '.runtime/node-v24.22.1-win-x64/node.exe'
    found = str(bundled) if bundled.is_file() else shutil.which('node')
    if not found:
        raise PaymentError('Node.js is required for card encryption.')
    return found


def encrypt_card(profile: PaymentProfile, public_key_der_b64: str, public_key_hash: str,
                 *, node_path=None) -> EncryptedCard:
    if not isinstance(profile, PaymentProfile):
        raise PaymentError('A validated billing profile is required.')
    if (not isinstance(public_key_der_b64, str) or len(public_key_der_b64) > 16384
            or not isinstance(public_key_hash, str) or not 1 <= len(public_key_hash) <= 1024
            or any(ord(c) < 32 for c in public_key_hash)):
        raise PaymentError('Current Apple public encryption metadata is required.')
    try:
        base64.b64decode(public_key_der_b64, validate=True)
    except (ValueError, TypeError):
        raise PaymentError('Invalid Apple public encryption key.') from None
    request = {'publicKeyDER': public_key_der_b64, 'publicKeyHash': public_key_hash,
               'number': profile.card['number'], 'securityCode': profile.card['security_code']}
    try:
        result = subprocess.run([_node_path(node_path), str(Path(__file__).with_name('encrypt-card.mjs'))],
            input=json.dumps(request), text=True, capture_output=True, timeout=10, check=False,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode or len(result.stdout) > 32768:
            raise PaymentError('Card encryption failed.')
        output = json.loads(result.stdout)
        values = []
        for name in ('cardNumber', 'securityCode'):
            value = output[name]
            envelope = json.loads(value)
            ciphertext = base64.b64decode(envelope['cipherText'], validate=True)
            if not 256 <= len(ciphertext) <= 1024 or envelope['publicKeyHash'] != public_key_hash:
                raise ValueError()
            values.append(value)
        return EncryptedCard(*values)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        # Never propagate subprocess stderr, input, key material or response bodies.
        raise PaymentError('Card encryption failed.') from None


def _submit_fields(model, profile):
    """Extract explicit submit bindings; callers may supply their extracted list.

    submit_fields is a caller-owned list of fully qualified fields observed in the
    CURRENT billing model, never a list inferred from a previous session.
    """
    if not isinstance(model, dict):
        raise PaymentError('A current Apple billing model is required.')
    explicit = model.get('submit_fields')
    if isinstance(explicit, (list, tuple)):
        return {v for v in explicit if isinstance(v, str) and v.startswith(B)}
    found = set()

    def walk(node, path=''):
        if not isinstance(node, dict):
            return
        bindings = node.get('b')
        if isinstance(bindings, dict):
            for name, spec in bindings.items():
                if not isinstance(spec, dict) or spec.get('submit') is None or spec.get('submit') is False:
                    continue
                name = spec.get('key', name)
                if not isinstance(name, str):
                    raise PaymentError('Current billing form has an unsupported submit binding.')
                candidate = name if name.startswith('checkout.') else path + '.' + name.removeprefix('d.')
                if not candidate.startswith(B):
                    continue
                predicate = spec.get('submit')
                if predicate is not True:
                    # Current Apple card bindings omit masked saved-card values.
                    # Only this observed AST can be evaluated for our fresh,
                    # unmasked numeric profile; never generally evaluate an AST.
                    unmasked = {'op': 'NOT', 'args': [{'op': 'IN', 'args': ['•', {'val': '_'}]}]}
                    secret_field = {CI + 'cardNumber': 'number', CI + 'securityCode': 'security_code'}.get(candidate)
                    numeric = profile.card.get(secret_field, '') if secret_field else ''
                    if predicate != unmasked or not secret_field or not re.fullmatch(r'[0-9]+', numeric):
                        raise PaymentError('Current billing form has an unsupported submit condition.')
                if candidate.startswith(B):
                    found.add(candidate)
        for name, child in node.items():
            if name not in ('b', 'd') and isinstance(child, dict):
                walk(child, f'{path}.{name}' if path else name)

    walk(model)
    if not found:
        walk(model, 'checkout.billing')
    return found


def _card_company(number):
    if number.startswith('4'):
        return 'VISA'
    if number[:2] in ('34', '37'):
        return 'AMERICAN_EXPRESS'
    if 51 <= int(number[:2]) <= 55 or 2221 <= int(number[:4]) <= 2720:
        return 'MASTERCARD'
    if number.startswith(('6011', '65')) or 644 <= int(number[:3]) <= 649:
        return 'DISCOVER'
    raise PaymentError('Unsupported card company for this US billing adapter.')


def build_us_billing_payload(profile: PaymentProfile, encrypted: EncryptedCard,
                             current_billing_model: dict) -> PrivateBillingPayload:
    if not isinstance(profile, PaymentProfile) or not isinstance(encrypted, EncryptedCard):
        raise PaymentError('Validated profile and encrypted card are required.')
    fields = _submit_fields(current_billing_model, profile)
    required_card = {B + 'selectBillingOption', CI + 'cardNumber', CI + 'securityCode', CI + 'expiration'}
    if not required_card <= fields:
        raise PaymentError('Current billing model does not advertise the supported card form.')
    p, a = profile.contact, profile.billing
    values = {B + 'selectBillingOption': 'CREDIT', CC + 'isUseShippingAddress': 'false',
        CI + 'validCardNumber': 'true', CI + 'isSplitPayment': 'false',
        CI + 'cardNumberForBinDetection': profile.card['number'][:4] + ' ' + profile.card['number'][4:6],
        CI + 'selectCardType': _card_company(profile.card['number']),
        CI + 'securityCode': encrypted.security_code, CI + 'cardNumber': encrypted.card_number,
        CI + 'expiration': profile.card['expiry_month'] + '/' + profile.card['expiry_year'][-2:],
        ADDRESS + 'countryCode': 'US', ADDRESS + 'firstName': p['first_name'],
        ADDRESS + 'lastName': p['last_name'], ADDRESS + 'street': a['address_line_1'],
        ADDRESS + 'street2': a['address_line_2'], ADDRESS + 'mobilePhone': p['phone'],
        ADDRESS + 'isDaytimePhoneSelected': 'false',
        ADDRESS + 'city': a['city'], ADDRESS + 'state': a['state'], ADDRESS + 'postalCode': a['postal_code'],
        ADDRESS + 'zipLookup.zipLookupCityState': a['city'] + ', ' + a['state'],
        ADDRESS + 'zipLookup.postalCode': a['postal_code'], ADDRESS + 'zipLookup.countryCode': 'US',
        ADDRESS + 'zipLookup.city': a['city'], ADDRESS + 'zipLookup.state': a['state']}
    common = {ADDRESS + k for k in ('firstName', 'lastName', 'street')}
    plain = {ADDRESS + k for k in ('city', 'state', 'postalCode')}
    zip_lookup = {ADDRESS + 'zipLookup.' + k for k in ('zipLookupCityState', 'postalCode')}
    if not common <= fields or not (plain <= fields or zip_lookup <= fields):
        raise PaymentError('Current billing address layout is unsupported or incomplete.')
    unknown = fields - values.keys()
    optional_empty = {B + 'selectedBillingOptions.giftCard.giftCardInput.giftCard',
                      B + 'selectedBillingOptions.giftCard.giftCardInput.deviceID',
                      B + 'selectedBillingOptions.creditCard.billingAddress.vatNumber'}
    if unknown - optional_empty:
        raise PaymentError('Current billing form contains unsupported submit bindings.')
    return PrivateBillingPayload({name: values.get(name, '') for name in sorted(fields)})


@dataclass(frozen=True)
class PaymentOutcome:
    kind: str
    message: str
    http_status: int | None = None
    stage: str = 'billing_validation'
    order_placed: bool = False


def classify_apple_payment_response(status, body=None, *, network_error=False) -> PaymentOutcome:
    """Return fixed safe descriptions, never Apple's potentially private text."""
    def outcome(kind, message):
        return PaymentOutcome(kind, message, status if type(status) is int else None)
    if network_error:
        return outcome('network_error', 'Billing submission outcome is uncertain; automatic retry is disabled.')
    if type(status) is not int or not 200 <= status < 300:
        return outcome('http_error', 'Apple rejected the billing request; no order was placed.')
    try:
        if isinstance(body, (str, bytes)):
            if len(body) > 1_048_576:
                raise ValueError()
            model = json.loads(body)
        else:
            model = body
        if not isinstance(model, dict):
            raise ValueError()
        # A normal page can contain FAQ/informational messages about declined
        # cards. Only explicit server error nodes constitute failure evidence.
        messages = []
        def collect(node, error_context=False, depth=0):
            if depth > 30:
                return
            if isinstance(node, dict):
                for key, value in node.items():
                    if key == 'b':  # Client binding/validation definitions.
                        continue
                    if key == 'm' and isinstance(value, list):
                        # Observed Graviton response messages have key/level/text.
                        # Informational m entries are not decline evidence.
                        for message in value[:100]:
                            if (isinstance(message, dict) and isinstance(message.get('level'), str)
                                    and message['level'].lower() == 'error'
                                    and isinstance(message.get('text'), str) and message['text']):
                                messages.append(message['text'][:2048].lower())
                        continue
                    active = error_context or key in {
                        'error', 'errors', 'errorMessage', 'validationError',
                        'validationErrors', 'fieldErrors', 'paymentError', 'paymentErrors'}
                    collect(value, active, depth + 1)
            elif isinstance(node, list):
                for value in node[:100]:
                    collect(value, error_context, depth + 1)
            elif isinstance(node, str) and error_context and node:
                messages.append(node[:2048].lower())
        collect(model)
        text = ' '.join(messages)
        if any(x in text for x in ('card was declined', 'card has been declined', 'payment was declined',
                                   'issuer declined', 'bank declined', 'payment authorization failed')):
            return outcome('payment_declined', 'Apple reported a payment decline during billing validation.')
        if any(x in text for x in ('please enter', 'please provide', 'invalid', 'expiration', 'security code',
                                   'alphanumeric', 'fewer characters', 'another form of payment',
                                   'purchasing cards', 'required field')):
            return outcome('field_validation', 'Apple reported a billing or card validation error.')
        if messages:
            return outcome('checkout_error', 'Apple reported a checkout error; no order was placed.')
        checkout = model.get('checkout', model.get('body', {}).get('checkout', {}))
        if isinstance(checkout, dict) and isinstance(checkout.get('review'), dict) and checkout['review']:
            return outcome('review_ready', 'Apple returned the review stage; no order was submitted.')
    except (ValueError, TypeError, AttributeError, RecursionError):
        pass
    return outcome('unconfirmed', 'Apple billing acceptance is unconfirmed; no order was submitted.')


def submit_billing_once(run_id: str, payload: PrivateBillingPayload,
                        send: Callable, claim_mutation: Callable) -> PaymentOutcome:
    """Claim durably BEFORE sending. An uncertain attempt remains consumed.

    send(payload) returns (HTTP status, parsed model or raw JSON). The caller owns
    current model/action/host checks and transport. No redirects, retries, final
    order calls or notifications are performed here.
    """
    if not isinstance(run_id, str) or not run_id or not isinstance(payload, PrivateBillingPayload):
        raise PaymentError('A guarded billing run and private payload are required.')
    if not claim_mutation(run_id, 'billing_card', 'billing_validation'):
        raise PaymentError('Billing was already attempted for this run; automatic retry is disabled.')
    try:
        status, body = send(payload)
    except Exception:
        return classify_apple_payment_response(None, network_error=True)
    return classify_apple_payment_response(status, body)


ORDER_ACTION = '_a=continueFromReviewToProcess&_m=checkout.review.placeOrder'
ORDER_PREFIX = 'checkout.review.placeOrder.'
ORDER_TERMS = frozenset(ORDER_PREFIX + 'termsAndConditions.' + name + '.termsCheckbox'
    for name in ('segmentSpecificRetailTerms', 'geoSpecificRetailTerms', 'segmentSpecificTerms', 'geoSpecificTerms'))


def _review_root(model):
    try:
        if 'body' in model:
            model = model['body']
        review = model['checkout']['review']
        if not isinstance(review, dict) or not isinstance(review.get('placeOrder'), dict):
            raise ValueError()
        return review['placeOrder']
    except (KeyError, TypeError, ValueError):
        raise PaymentError('A current Apple review and place-order model is required.') from None


def _strings(node, depth=0):
    if depth > 20:
        return
    if isinstance(node, dict):
        for value in node.values():
            yield from _strings(value, depth + 1)
    elif isinstance(node, list):
        for value in node[:100]:
            yield from _strings(value, depth + 1)
    elif isinstance(node, str):
        yield node


def build_order_payload(current_model, review_proof) -> PrivateBillingPayload:
    """Pure formatter gated by a fresh caller-verified, explicitly authorized review.

    The caller must durably journal ONE final attempt and validate the actual
    current action host before sending. This function performs no HTTP request.
    """
    p = review_proof if isinstance(review_proof, dict) else {}
    verified = p.get('verified_at')
    if (p.get('order_authorized') is not True or type(verified) not in (int, float)
            or not math.isfinite(verified) or not -5 <= time.time() - verified <= 30
            or not isinstance(p.get('part'), str) or p['part'] not in {'MK1F4LL/A', 'MK1E4LL/A', 'MHRN4LL/A'}
            or not isinstance(p.get('store_id'), str) or not re.fullmatch(r'R[0-9]{3}', p['store_id'])
            or type(p.get('quantity')) is not int or p['quantity'] != 1 or p.get('applecare') is not False
            or p.get('currency') != 'USD' or p.get('tax_included') is not True
            or type(p.get('total_cents')) is not int or not 0 < p['total_cents'] <= 230_000):
        raise PaymentError('Final order requires a fresh authorized review within the one-phone spending limits.')
    node = _review_root(current_model)
    advertised = False
    for value in _strings(node.get('a', {})):
        try:
            url = urlsplit(value)
            query = dict(parse_qsl(url.query))
            if (url.path in {'/shop/checkoutx', '/shop/checkoutx/review'} and query.get('_a') == 'continueFromReviewToProcess'
                    and query.get('_m') == 'checkout.review.placeOrder'):
                advertised = True
        except ValueError:
            continue
    if not advertised:
        raise PaymentError('The current review does not advertise the supported final-order action.')
    fields = set()
    def walk(value, path=ORDER_PREFIX.rstrip('.')):
        if not isinstance(value, dict):
            return
        bindings = value.get('b', {})
        if isinstance(bindings, dict):
            for name, spec in bindings.items():
                if not isinstance(spec, dict) or spec.get('submit') is None or spec.get('submit') is False:
                    continue
                key = spec.get('key', name)
                if not isinstance(key, str) or spec.get('submit') is not True:
                    raise PaymentError('The final review contains an unsupported submit binding.')
                candidate = key if key.startswith('checkout.') else path + '.' + key.removeprefix('d.')
                if candidate not in ORDER_TERMS:
                    raise PaymentError('The final review contains an unsupported submit field.')
                fields.add(candidate)
        for name, child in value.items():
            if name not in {'a', 'b', 'd'} and isinstance(child, dict):
                walk(child, path + '.' + name)
    walk(node)
    if not fields:
        # Current US review can expose informational terms only. Submit an empty
        # form, rather than invent a checkbox absent from its live bindings.
        terms = node.get('termsAndConditions')
        if (p.get('country') != 'US' or not isinstance(terms, dict)
                or not isinstance(terms.get('d'), dict) or not isinstance(terms.get('b'), dict)):
            raise PaymentError('A current US informational terms module is required for an empty final form.')
        return PrivateBillingPayload()
    if len(fields) != 1:
        raise PaymentError('The current review contains ambiguous terms checkbox bindings.')
    return PrivateBillingPayload({next(iter(fields)): 'true'})


@dataclass(frozen=True)
class OrderOutcome:
    kind: str
    message: str
    http_status: int | None
    continuation_url: str | None = field(default=None, repr=False)
    continuation_method: str | None = None
    order_accepted: bool = False


def classify_order_response(status, body, *, response_url=None) -> OrderOutcome:
    """Conservative structured response handling, without retrying final POST.

    A thank-you redirect requires a separate confirmation read; a spinner only
    permits status polling. No raw response or order identifiers are returned.
    """
    def result(kind, message, url=None, method=None, accepted=False):
        return OrderOutcome(kind, message, status if type(status) is int else None, url, method, accepted)
    if type(status) is not int or not 200 <= status < 300:
        return result('unknown', 'The final submission outcome is unconfirmed; do not repeat the order request.')
    try:
        if isinstance(body, (bytes, str)):
            if len(body) > 1_048_576:
                raise ValueError()
            try:
                model = json.loads(body)
            except (ValueError, UnicodeError):
                from beta.apple_client import parse_init_data
                model = parse_init_data(body.encode() if isinstance(body, str) else body)
        else:
            model = body
        if not isinstance(model, dict):
            raise ValueError()
        payment = classify_apple_payment_response(status, model)
        if payment.kind == 'payment_declined':
            return result('payment_declined', 'Apple reported a payment decline after final submission.')
        if payment.kind in {'field_validation', 'checkout_error'}:
            return result(payment.kind, 'Apple reported a checkout error after final submission.')
        checkout = model.get('body', model).get('checkout', {})
        # A confirmation document must explicitly identify the thank-you page
        # AND contain its own order number. A redirect alone is insufficient.
        page = checkout.get('d', {}).get('page') if isinstance(checkout, dict) else None
        if page in {'thankyou', 'thankYou'}:
            confirmation = checkout.get('thankYou', checkout.get('thankyou', {}))
            order_number = confirmation.get('d', {}).get('orderNumber') if isinstance(confirmation, dict) else None
            if isinstance(order_number, str) and re.fullmatch(r'[A-Za-z0-9-]{5,40}', order_number):
                return result('accepted', 'Apple returned a structured order confirmation.', accepted=True)
        candidates = list(_strings(model.get('head', {})))
        if isinstance(checkout, dict) and isinstance(checkout.get('spinner'), dict):
            candidates.extend(_strings(checkout['spinner'].get('a', {})))
        from beta.apple_client import safe_url
        for candidate in candidates:
            if not candidate.startswith(('https://', '/shop/')):
                continue
            url = safe_url(urljoin(response_url or '', candidate))
            parsed = urlsplit(url)
            query = dict(parse_qsl(parsed.query))
            if parsed.path == '/shop/checkout/thankyou':
                return result('confirmation_pending', 'Apple provided a confirmation page to read; acceptance is not yet verified.', url, 'GET')
            if parsed.path == '/shop/checkout/status':
                return result('processing', 'Apple is processing the submitted order.', url, 'GET')
            if ((parsed.path == '/shop/checkoutx' and query.get('_a') == 'pollingProcess')
                    or (parsed.path == '/shop/checkoutx/statusX' and query.get('_a') == 'checkStatus')) and query.get('_m') == 'spinner':
                return result('processing', 'Apple is processing the submitted order.', url, 'POST')
        if isinstance(checkout, dict) and isinstance(checkout.get('paymentAuthentication'), dict):
            return result('authentication_required', 'Apple requires payment authentication; no automatic order replay is allowed.')
    except (ValueError, TypeError, AttributeError, KeyError):
        pass
    return result('unknown', 'Apple has not provided verified acceptance or decline; do not repeat the order request.')
