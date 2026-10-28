import json
import subprocess
import unittest
from unittest.mock import patch

from beta.payment import (ADDRESS, B, CC, CI, EncryptedCard, PaymentError,
    PrivateBillingPayload, build_us_billing_payload, classify_apple_payment_response,
    encrypt_card, submit_billing_once, validate_profile, _node_path,
    build_order_payload, classify_order_response, ORDER_PREFIX)


def fixture():
    # Public test card and fictional contact/address only.
    return {'mode': 'dummy_decline_test',
        'contact': {'first_name': 'Test', 'last_name': 'Buyer',
                    'email': 'buyer@example.com', 'phone': '202-555-0123'},
        'billing': {'address_line_1': '123 Example Street', 'address_line_2': '',
                    'city': 'New York', 'state': 'NY', 'postal_code': '10001', 'country': 'US'},
        'card': {'number': '4000000000000002', 'expiry_month': 12,
                 'expiry_year': 2030, 'security_code': '123'}}


def model(zip_lookup=False):
    fields = [B + 'selectBillingOption', CC + 'isUseShippingAddress']
    fields += [CI + key for key in ('validCardNumber', 'isSplitPayment',
        'cardNumberForBinDetection', 'selectCardType', 'securityCode', 'expiration', 'cardNumber')]
    fields += [ADDRESS + key for key in ('countryCode', 'firstName', 'lastName',
        'street', 'street2', 'mobilePhone', 'isDaytimePhoneSelected')]
    fields += [ADDRESS + key for key in (
        ('zipLookup.zipLookupCityState', 'zipLookup.postalCode', 'zipLookup.countryCode')
        if zip_lookup else ('city', 'state', 'postalCode'))]
    return {'submit_fields': fields}


class PaymentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Generated disposable test RSA pair, never a production key.
        script = """const c=require('node:crypto');const k=c.generateKeyPairSync('rsa',
          {modulusLength:2048,publicKeyEncoding:{type:'spki',format:'der'},
          privateKeyEncoding:{type:'pkcs8',format:'pem'}});
          process.stdout.write(JSON.stringify({pub:k.publicKey.toString('base64'),priv:k.privateKey}));"""
        cls.key = json.loads(subprocess.run([_node_path(), '-e', script], capture_output=True,
            text=True, timeout=20, check=True).stdout)

    def test_profile_and_form_representations_are_private(self):
        profile = validate_profile(fixture())
        self.assertTrue(profile.known_dummy)
        self.assertEqual(profile.card['expiry_month'], '12')
        with self.assertRaises(TypeError):
            profile.card['number'] = '4000001234567890'
        payload = build_us_billing_payload(profile, EncryptedCard('cipher-PAN', 'cipher-CVV'), model())
        for value in (profile, payload, EncryptedCard('cipher-PAN', 'cipher-CVV')):
            text = str(value) + repr(value)
            self.assertNotIn('4000000000000002', text)
            self.assertNotIn('buyer@example.com', text)
            self.assertNotIn('cipher-PAN', text)

    def test_declared_non_public_dummy_can_validate_billing_but_is_not_marked_known(self):
        data = fixture(); data['card']['number'] = '4000001234567890'
        self.assertFalse(validate_profile(data).known_dummy)
        data['card']['expiry_year'] = 2020
        self.assertEqual(validate_profile(data).card['expiry_year'], '2020')

    def test_bad_profile_errors_never_echo_private_values(self):
        for area, key, value in [('contact', 'email', 'PRIVATE_INVALID'),
                                 ('card', 'number', 'PRIVATE_INVALID'),
                                 ('card', 'security_code', 'PRIVATE_INVALID'),
                                 ('billing', 'country', 'PRIVATE_INVALID')]:
            data = fixture(); data[area][key] = value
            with self.subTest(area=area, key=key):
                with self.assertRaises(PaymentError) as caught:
                    validate_profile(data)
                self.assertNotIn('PRIVATE_INVALID', str(caught.exception))
        for month in (True, 0, 13, '01\n'):
            data = fixture();data['card']['expiry_month'] = month
            with self.assertRaises(PaymentError):
                validate_profile(data)

    def test_encryption_is_oaep_sha256_current_key_and_hash_round_trip(self):
        profile = validate_profile(fixture())
        encrypted = encrypt_card(profile, self.key['pub'], 'fixture-current-key-hash')
        script = """const c=require('node:crypto');let s='';process.stdin.on('data',x=>s+=x);
          process.stdin.on('end',()=>{const d=JSON.parse(s);const out=d.values.map(x=>{
            const e=JSON.parse(x);return c.privateDecrypt({key:d.key,
            padding:c.constants.RSA_PKCS1_OAEP_PADDING,oaepHash:'sha256',
            oaepLabel:Buffer.alloc(0)},Buffer.from(e.cipherText,'base64')).toString();});
            process.stdout.write(JSON.stringify(out));});"""
        result = subprocess.run([_node_path(), '-e', script],
            input=json.dumps({'key': self.key['priv'], 'values': [encrypted.card_number, encrypted.security_code]}),
            capture_output=True, text=True, timeout=10, check=True)
        self.assertEqual(json.loads(result.stdout), ['4000000000000002', '123'])
        self.assertEqual(json.loads(encrypted.card_number)['publicKeyHash'], 'fixture-current-key-hash')
        self.assertNotEqual(encrypt_card(profile, self.key['pub'], 'fixture-current-key-hash').card_number,
                            encrypted.card_number)  # OAEP fresh randomness

    def test_invalid_or_missing_key_has_no_fallback_and_no_private_exception(self):
        profile = validate_profile(fixture())
        for key, hash_value in [('', ''), ('not a key', 'x'), ('YWJj', 'x')]:
            with self.subTest(key=key):
                with self.assertRaises(PaymentError) as caught:
                    encrypt_card(profile, key, hash_value)
                self.assertNotIn(profile.card['number'], str(caught.exception))
        with patch('beta.payment.subprocess.run', return_value=subprocess.CompletedProcess(
                [], 2, '', 'PRIVATE_PAN_AND_CVV')):
            with self.assertRaises(PaymentError) as caught:
                encrypt_card(profile, self.key['pub'], 'x')
            self.assertNotIn('PRIVATE_', str(caught.exception))

    def test_plain_and_zip_layout_are_explicit_and_pan_is_encrypted(self):
        profile = validate_profile(fixture())
        encrypted = EncryptedCard('cipher-PAN', 'cipher-CVV')
        for zip_lookup in (False, True):
            payload = build_us_billing_payload(profile, encrypted, model(zip_lookup))
            self.assertEqual(payload[CI + 'cardNumber'], 'cipher-PAN')
            self.assertEqual(payload[CI + 'securityCode'], 'cipher-CVV')
            self.assertEqual(payload[CI + 'expiration'], '12/30')
            self.assertEqual(payload[CI + 'cardNumberForBinDetection'], '4000 00')
            self.assertEqual(payload[CC + 'isUseShippingAddress'], 'false')
            self.assertNotIn(profile.card['number'], json.dumps(payload))
            if zip_lookup:
                self.assertEqual(payload[ADDRESS + 'zipLookup.zipLookupCityState'], 'New York, NY')
                self.assertNotIn(ADDRESS + 'city', payload)
        for changed in ({}, {'submit_fields': model()['submit_fields'] + [B + 'newRequiredField']}):
            with self.assertRaises(PaymentError):
                build_us_billing_payload(profile, encrypted, changed)

    def test_current_model_binding_extraction(self):
        root = {}
        for name in model()['submit_fields']:
            parts = name.split('.')
            node = root
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node.setdefault('b', {})[parts[-1]] = {'key': 'd.' + parts[-1], 'submit': True}
        result = build_us_billing_payload(validate_profile(fixture()), EncryptedCard('cipher', 'cipher'), root)
        self.assertEqual(set(result), set(model()['submit_fields']))

    def test_current_conditional_card_bindings_and_us_zip_fields(self):
        root = {}
        fields = model(zip_lookup=True)['submit_fields'] + [ADDRESS + 'zipLookup.city', ADDRESS + 'zipLookup.state',
            B + 'selectedBillingOptions.giftCard.giftCardInput.deviceID',
            B + 'selectedBillingOptions.giftCard.giftCardInput.giftCard']
        conditional = {'op': 'NOT', 'args': [{'op': 'IN', 'args': ['•', {'val': '_'}]}]}
        card_node = None
        for name in fields:
            parts = name.split('.')
            node = root
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            descriptor = {'key': 'd.' + parts[-1], 'submit': True}
            if name in (CI + 'cardNumber', CI + 'securityCode'):
                descriptor['submit'] = conditional
                descriptor['encryption'] = {'get': 'd.encryption' if name.endswith('.cardNumber') else 'd.encryption-securityCode'}
                card_node = node
            node.setdefault('b', {})[parts[-1]] = descriptor
        # Public bootstrap structure only: no saved profile or live card data.
        card_node['d'] = {'ref': {'scheme': 'rsa-with-hash'},
            'encryption': {'publicKey': 'FIXTURE_PUBLIC_KEY', 'publicKeyHash': 'FIXTURE_HASH', 'algorithm': 'RSA-OAEP'},
            'encryption-securityCode': {'publicKey': 'FIXTURE_PUBLIC_KEY', 'publicKeyHash': 'FIXTURE_HASH', 'algorithm': 'RSA-OAEP'}}
        profile = validate_profile(fixture())
        encrypted = EncryptedCard('cipher-PAN', 'cipher-CVV')
        result = build_us_billing_payload(profile, encrypted, root)
        self.assertEqual(result[CI + 'cardNumber'], 'cipher-PAN')
        self.assertEqual(result[CI + 'securityCode'], 'cipher-CVV')
        self.assertEqual(result[ADDRESS + 'zipLookup.city'], 'New York')
        self.assertEqual(result[ADDRESS + 'zipLookup.state'], 'NY')
        self.assertEqual(result[B + 'selectedBillingOptions.giftCard.giftCardInput.deviceID'], '')
        self.assertEqual(result[B + 'selectedBillingOptions.giftCard.giftCardInput.giftCard'], '')
        for bad in ({'op': 'NOT', 'args': [{'op': 'IN', 'args': ['*', {'val': '_'}]}]},
                    {'op': 'ALWAYS_TRUE'}, 'true'):
            card_node['b']['cardNumber']['submit'] = bad
            with self.assertRaisesRegex(PaymentError, 'unsupported submit condition'):
                build_us_billing_payload(profile, encrypted, root)

    def test_real_response_categories_never_infer_decline_from_http200(self):
        cases = [(200, {'error': 'Please provide a valid card expiration date PRIVATE_PII'}, 'field_validation'),
                 (200, {'error': 'Your card was declined PRIVATE_PII'}, 'payment_declined'),
                 (200, {'error': 'Pickup time is unavailable PRIVATE_PII'}, 'checkout_error'),
                 (200, {'checkout': {'review': {'d': {'ready': True}}}}, 'review_ready'),
                 (200, {'message': 'If your card was declined, contact the issuer.'}, 'unconfirmed'),
                 (200, {'checkout': {'review': {'d': {'message': 'A card was declined FAQ.'}}}}, 'review_ready'),
                 (200, {'b': {'errorMessage': 'Your card was declined'}}, 'unconfirmed'),
                 (200, {'body': {'checkout': {'m': [{'key': 'fixture', 'level': 'error',
                            'text': 'Your card was declined PRIVATE_PII'}]}}}, 'payment_declined'),
                 (200, {'m': [{'key': 'fixture', 'level': 'info', 'text': 'If your card was declined, contact the issuer.'}]}, 'unconfirmed'),
                 (200, {'d': {}}, 'unconfirmed'), (200, '<html>no</html>', 'unconfirmed'),
                 (541, {'error': 'declined'}, 'http_error')]
        for status, body, kind in cases:
            outcome = classify_apple_payment_response(status, body)
            self.assertEqual(outcome.kind, kind)
            self.assertFalse(outcome.order_placed)
            self.assertNotIn('PRIVATE_', repr(outcome))

    def test_durable_claim_precedes_one_post_and_uncertain_outcome_is_not_retried(self):
        events, claims = [], set()
        def claim(run_id, key, phase):
            events.append(('claim', run_id, key, phase))
            if (run_id, key) in claims:
                return False
            claims.add((run_id, key));return True
        def send(payload):
            events.append(('send',));raise TimeoutError('PRIVATE_TRANSPORT_DATA')
        payload = PrivateBillingPayload({'encrypted': 'cipher'})
        result = submit_billing_once('test-run', payload, send, claim)
        self.assertEqual(result.kind, 'network_error')
        self.assertEqual(events[0], ('claim', 'test-run', 'billing_card', 'billing_validation'))
        with self.assertRaises(PaymentError):
            submit_billing_once('test-run', payload, send, claim)
        self.assertEqual(sum(e[0] == 'send' for e in events), 1)
        self.assertNotIn('PRIVATE_', repr(result))

    def test_final_payload_requires_fresh_authorized_review_and_current_terms_action(self):
        import time
        current = {'body': {'checkout': {'review': {'placeOrder': {
            'a': {'continue': {'url': '/shop/checkoutx?_a=continueFromReviewToProcess&_m=checkout.review.placeOrder'}},
            'termsAndConditions': {'geoSpecificTerms': {'b': {'termsCheckbox': {
                'key': 'd.termsCheckbox', 'submit': True}}}}}}}}}
        proof = {'order_authorized': True, 'verified_at': time.time(), 'part': 'MHRN4LL/A',
            'store_id': 'R654', 'quantity': 1, 'applecare': False, 'currency': 'USD',
            'tax_included': True, 'total_cents': 76104}
        self.assertEqual(dict(build_order_payload(current, proof)),
            {ORDER_PREFIX + 'termsAndConditions.geoSpecificTerms.termsCheckbox': 'true'})
        for changes in ({'order_authorized': False}, {'verified_at': time.time() - 31},
                        {'verified_at': float('nan')}, {'quantity': True}, {'quantity': 2},
                        {'applecare': True}, {'total_cents': 230001}, {'tax_included': False}):
            with self.assertRaises(PaymentError):
                build_order_payload(current, {**proof, **changes})
        bad = json.loads(json.dumps(current));bad['body']['checkout']['review']['placeOrder']['a'] = {}
        with self.assertRaises(PaymentError):
            build_order_payload(bad, proof)

    def test_final_response_processing_and_decline_are_distinct_from_acceptance(self):
        url = 'https://secure8.store.apple.com/shop/checkout'
        outcomes = [
            ({'head': {'data': {'url': '/shop/checkout/status'}}}, 'processing'),
            ({'body': {'checkout': {'spinner': {'a': {'poll': {'url':
                '/shop/checkoutx?_a=pollingProcess&_m=spinner'}}}}}}, 'processing'),
            ({'body': {'spinner': {'a': {'pollingProcess': {'url':
                '/shop/checkoutx?_a=pollingProcess&_m=spinner'}}}}}, 'processing'),
            ({'head': {'data': {'url': '/shop/checkout/thankyou'}}}, 'confirmation_pending'),
            ({'body': {'checkout': {'d': {'page': 'thankyou'}, 'thankYou': {'d': {'orderNumber': 'FIXTURE12345'}}}}}, 'accepted'),
            ({'body': {'checkout': {'d': {'page': 'thankyou'}, 'thankYou': {'d': {}}}}}, 'unknown'),
            ({'m': [{'key': 'fixture', 'level': 'error', 'text': 'Your card was declined PRIVATE_PII'}]}, 'payment_declined'),
            ({'m': [{'key': 'fixture', 'level': 'info', 'text': 'Your card was declined FAQ'}]}, 'unknown'),
            ({'body': {'checkout': {'paymentAuthentication': {'d': {'required': True}}}}}, 'authentication_required'),
            ({'head': {'data': {'url': 'https://example.com/shop/checkout/status'}}}, 'unknown'),
        ]
        for body, kind in outcomes:
            result = classify_order_response(200, body, response_url=url)
            self.assertEqual(result.kind, kind)
            self.assertEqual(result.order_accepted, kind == 'accepted')
            self.assertNotIn('PRIVATE_', repr(result))
            self.assertNotIn('FIXTURE12345', repr(result))
        html = b'<script id="init_data" type="application/json">{"head":{"data":{"url":"/shop/checkout/status"}}}</script>'
        self.assertEqual(classify_order_response(200, html, response_url=url).kind, 'processing')
        self.assertEqual(classify_order_response(541, {}, response_url=url).kind, 'unknown')

    def test_current_us_review_with_informational_terms_sends_empty_form(self):
        import time
        current = {'body': {'checkout': {'review': {'placeOrder': {
            'd': {}, 'b': {'continue': {'key': None, 'submit': None}},
            'a': {'continue': {'url': '/shop/checkoutx/review?_a=continueFromReviewToProcess&_m=checkout.review.placeOrder'}},
            'termsAndConditions': {'d': {'termsAndConditionsHeaderLabel': 'Fixture terms'},
                'b': {'showTermData': {}}, 't': {}, 'p': {}}}}}}}
        proof = {'country': 'US', 'order_authorized': True, 'verified_at': time.time(),
            'part': 'MHRN4LL/A', 'store_id': 'R654', 'quantity': 1, 'applecare': False,
            'currency': 'USD', 'tax_included': True, 'total_cents': 76104}
        self.assertEqual(dict(build_order_payload(current, proof)), {})
        with self.assertRaises(PaymentError):
            build_order_payload(current, {**proof, 'country': 'CA'})
        del current['body']['checkout']['review']['placeOrder']['termsAndConditions']
        with self.assertRaises(PaymentError):
            build_order_payload(current, proof)


if __name__ == '__main__':
    unittest.main()
