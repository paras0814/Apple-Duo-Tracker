import json
import unittest

from beta.alerts import checkout_failure_payload, normalize_failure, safe_text


TARGET = {"part":"MHRN4LL/A","model":"iPhone 17e","storage":"256GB","color":"Black", "buy_url":"https://www.apple.com/shop/buy-iphone/iphone-17e/6.1-inch-display-256gb-black-unlocked", "store_name":"World Trade Center"}
STORE = {"id":"R654","name":"World Trade Center","retail_url":"https://www.apple.com/retail/worldtradecenter/"}


class FailureAlertTests(unittest.TestCase):
    def test_decline_requires_explicit_apple_evidence_and_normalization_is_idempotent(self):
        for source, flag in (("local", True), ("apple", False), ("network", True)):
            event=normalize_failure({"classification":"payment_declined","source":source,"apple_decline":flag})
            self.assertNotEqual(event["classification"], "payment_declined")
        event=normalize_failure({"classification":"payment_declined","source":"apple","apple_decline":True})
        self.assertEqual(normalize_failure(event), event)
        payload=checkout_failure_payload(event,TARGET,STORE)
        self.assertIn("Apple reported a payment decline",json.dumps(payload))

    def test_store_and_model_are_clickable_and_no_payment_values_are_included(self):
        event={"kind":"field_validation","source":"apple","stage":"billing_validation", "profile_name":"Local beta", "total_cents":76104,"tax_included":True,"card_number":"PRIVATE_PAN","security_code":"PRIVATE_CVV"}
        payload=checkout_failure_payload(event,TARGET,STORE)
        text=json.dumps(payload)
        self.assertIn(STORE["retail_url"],text)
        self.assertIn(TARGET["buy_url"],text)
        self.assertIn("$761.04 incl. tax",text)
        self.assertNotIn("PRIVATE",text)
        self.assertEqual(payload["allowed_mentions"],{"parse":[]})
        self.assertEqual(payload["embeds"][0]["title"],"Beta checkout failed | Apple US")

    def test_untrusted_error_text_is_never_a_contact_or_payment_dump(self):
        result=normalize_failure({"classification":"validation_failed", "reason":"Jane Example, 123 Example Street; 4242424242424242; cvv=123; cookie=SECRETVALUE; Jane@example.com"})
        self.assertEqual(result["reason"],"Apple rejected checkout input")
        for private in ("Jane", "Example Street", "4242", "123", "SECRETVALUE"):
            self.assertNotIn(private,json.dumps(result))

    def test_local_profile_and_safe_text_redact_identifiers(self):
        text=safe_text("email me@example.com phone 201 555 1234 card number 4111 1111 1111 1111 security code 123 cookie=SECRET_COOKIE https://example.com?token=SECRET")
        for private in ("me@example", "201", "4111", "123", "SECRET"):
            self.assertNotIn(private,text)

    def test_event_dedup_ignores_variable_error_text_but_keeps_stage_and_code(self):
        a=normalize_failure({"kind":"field_validation","stage":"billing_validation","code":"invalidExpiry","reason":"ID 123"})
        b=normalize_failure({"kind":"field_validation","stage":"billing_validation","code":"invalidExpiry","reason":"ID 456"})
        c=normalize_failure({"kind":"field_validation","stage":"pickup_contact","code":"invalidExpiry"})
        self.assertEqual(a["event_key"],b["event_key"])
        self.assertNotEqual(a["event_key"],c["event_key"])

    def test_unknown_shapes_and_sensitive_model_urls_fail_closed(self):
        event=normalize_failure({"classification":[],"reason":{},"stage":"token=PRIVATE","code":"4111111111111111"})
        self.assertEqual(event["stage"],"checkout")
        self.assertEqual(event["code"],"")
        payload=checkout_failure_payload(event,{**TARGET,"buy_url":TARGET["buy_url"]+"#token=PRIVATE"},{**STORE,"retail_url":"https://evil.example/"})
        self.assertNotIn("PRIVATE",json.dumps(payload))
        self.assertNotIn("evil.example",json.dumps(payload))


if __name__=='__main__':
    unittest.main()
