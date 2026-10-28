"""Offline cart/transport fixtures only; never contact Apple or add a phone."""
import copy
import http.cookiejar
import inspect
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from beta.apple_client import (APIError, AppleClient, Response, safe_url,
                               parse_init_data, assert_empty_bag, validate_bag)
from scripts.setup_api_transport import validate_member, PINS


def bag(**changes):
    item = {"d":{"sku":"MHRN4LL/A", "name":"iPhone 17e 256GB Black",
                 "unitPrice":"$699.00", "totalPrice":"$699.00", "hasAppleCareInstallment":False},
            "c":["itemQuantity", "delivery"], "itemQuantity":{"d":{"quantity":1}}}
    item["d"].update(changes)
    return {"shoppingCart":{"d":{"hasItems":True}, "items":{
        "d":{"bagCount":1}, "c":["fixture-item"], "fixture-item":item}}}


def cookie(name, value, domain="www.apple.com", path="/", expires=None):
    return http.cookiejar.Cookie(0,name,value,None,False,domain,domain.startswith("."),
        domain.startswith("."),path,True,True,expires,expires is None,None,None,{},False)


class FakeSession:
    def __init__(self, **options):
        self.options=options;self.verify=options["verify"]
        self.cookies=SimpleNamespace(jar=options["cookies"])
        self.calls=[];self.responses=[];self.closed=False
    def request(self, method, url, **kwargs):
        self.calls.append((method,url,kwargs))
        result=self.responses.pop(0)
        if isinstance(result, Exception):raise result
        if callable(result):result=result(self,method,url,kwargs)
        status,headers,content=result
        return SimpleNamespace(status_code=status,headers=headers,content=content,url=url)
    def close(self):self.closed=True


class CartTests(unittest.TestCase):
    def validate(self, model):
        return validate_bag(model,"MHRN4LL/A","iPhone 17e","256GB","Black",230000)

    def test_init_data_parser_requires_one_complete_dictionary_model(self):
        content=b'<script id="init_data" type="application/json">{"shoppingCart":{"d":{}}}</script>'
        self.assertIn("shoppingCart",parse_init_data(content))
        for bad in (b'',content+content,b'<script id="init_data">[]</script>',
                    b'<script id="init_data">{bad}</script>',b'<script id="init_data">{}'):
            with self.subTest(bad=bad):
                with self.assertRaises(APIError):parse_init_data(bad)

    def test_only_explicit_typed_empty_bag_allows_a_new_add(self):
        model={"shoppingCart":{"d":{"hasItems":False},"items":{"d":{"bagCount":0}}}}
        self.assertTrue(assert_empty_bag(model)["confirmed_empty"])
        for value in (None,{},bag()):
            with self.assertRaises(APIError):assert_empty_bag(value)
        for value in (False,"0",1):
            m=copy.deepcopy(model);m["shoppingCart"]["items"]["d"]["bagCount"]=value
            with self.assertRaises(APIError):assert_empty_bag(m)
        model["shoppingCart"]["items"]["hidden"]={"d":{"sku":"UNEXPECTED"}}
        with self.assertRaises(APIError):assert_empty_bag(model)

    def test_exact_single_phone_returns_sanitized_pre_tax_facts(self):
        facts=self.validate(bag())
        self.assertEqual(facts["base_price_cents"],69900)
        self.assertEqual(facts["quantity"],1)
        self.assertFalse(facts["applecare"])
        self.assertFalse(facts["tax_included"])
        self.assertNotIn("fixture-item",json.dumps(facts))

    def test_wrong_part_capacity_color_and_applecare_are_rejected(self):
        for change in ({"sku":"MK1F4LL/A"},{"name":"iPhone 17e 512GB Black"},
                       {"name":"iPhone 17e 256GB White"},{"hasAppleCareInstallment":True},
                       {"hasAppleCareInstallment":None},{"hasAppleCare":True}):
            with self.subTest(change=change):
                with self.assertRaises(APIError):self.validate(bag(**change))

    def test_multiple_and_hidden_line_items_cannot_pass_one_phone_guard(self):
        for change in ("count","children","hidden"):
            m=bag();items=m["shoppingCart"]["items"]
            if change=="count":items["d"]["bagCount"]=2
            if change=="children":items["c"].append("second")
            if change=="hidden":items["second"]={"d":{"sku":"OTHER"}}
            with self.assertRaises(APIError):self.validate(m)

    def test_quantity_and_malformed_options_fail_with_fixed_api_error(self):
        for value in (2,True,"1",None):
            m=bag();m["shoppingCart"]["items"]["fixture-item"]["itemQuantity"]["d"]["quantity"]=value
            with self.assertRaises(APIError):self.validate(m)
        for key,value in (("itemQuantity",None),("itemQuantity",[]),("c",None),("c",{})):
            m=bag();m["shoppingCart"]["items"]["fixture-item"][key]=value
            with self.assertRaises(APIError):self.validate(m)
        m=bag();m["shoppingCart"]["items"]["fixture-item"]["unlistedAppleCare"]={"d":{}}
        with self.assertRaises(APIError):self.validate(m)

    def test_money_is_usd_exact_positive_one_unit_and_within_cap(self):
        for change in ({"totalPrice":"$2,300.01","unitPrice":"$2,300.01"},
                       {"unitPrice":"$599.00"},{"unitPrice":"$58.25/mo."},
                       {"totalPrice":"CA$699.00"},{"unitPrice":"$0.00"}):
            with self.assertRaises(APIError):self.validate(bag(**change))
        with self.assertRaises(APIError):validate_bag(bag(),"MHRN4LL/A","iPhone 17e","256GB","Black",230001)


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.client=AppleClient(self.root,"fixture-run",session_factory=FakeSession)
        self.session=self.client._session
    def tearDown(self):
        self.client.close();self.temp.cleanup()

    def test_direct_chrome_profile_no_proxy_no_retry_tls_and_private_persistence(self):
        self.assertEqual(self.session.options["impersonate"],"chrome152")
        self.assertIs(self.session.options["trust_env"],False)
        self.assertEqual(self.session.options["proxies"],{})
        self.assertEqual(self.session.options["retry"],0)
        self.assertTrue(self.session.options["verify"])
        self.client._jar.set_cookie(cookie("as_atb","one%7Ctwo%7Cfixture-token"))
        self.session.responses=[(200,{"Content-Type":"text/html","Set-Cookie":"DO_NOT_EXPOSE"},b'ok')]
        r=self.client.request("GET","https://www.apple.com/shop/bag")
        kwargs=self.session.calls[0][2]
        self.assertEqual(kwargs["proxy"],"")
        self.assertEqual(kwargs["proxies"],{})
        self.assertIs(kwargs["allow_redirects"],False)
        self.assertEqual(self.client.atb_token(),"fixture-token")
        self.assertNotIn("set-cookie",r.headers)
        reopened=AppleClient(self.root,"fixture-run",session_factory=FakeSession)
        try:self.assertEqual(reopened.atb_token(),"fixture-token")
        finally:reopened.close()
        self.assertTrue(self.client.cookie_file.is_relative_to(self.root.resolve()/".data/api-runs"))
        self.assertIs(self.session.options["default_headers"],False)

    def test_run_identity_and_external_urls_are_rejected_before_network(self):
        for run in ("../run","run/path",""):
            with self.assertRaises(APIError):AppleClient(self.root,run,session_factory=FakeSession)
        for url in ("http://www.apple.com/shop/bag","https://www.apple.com.evil.test/shop/bag",
                    "https://evil.test/","https://www.apple.com@evil.test/","https://www.apple.com:444/shop/bag",
                    "https://secure8.store.apple.com.evil.test/","https://www.apple.com\\@evil.test/"):
            with self.assertRaises(APIError):self.client.request("GET",url)
        self.assertEqual(self.session.calls,[])
        self.assertEqual(safe_url("https://www.apple.com/shop/product#MK1F4LL/A"),"https://www.apple.com/shop/product")

    def test_redirect_to_external_host_never_receives_private_headers_or_fields(self):
        self.session.responses=[(302,{"Location":"https://evil.test/?token=PRIVATE"},b'')]
        with self.assertRaises(APIError) as caught:
            self.client.request("POST","https://secure8.store.apple.com/shop/checkout",
                                headers={"X-AOS-STK":"PRIVATE"},fields={"private":"PRIVATE"})
        self.assertEqual(len(self.session.calls),1)
        self.assertTrue(caught.exception.ambiguous)
        self.assertNotIn("PRIVATE",str(caught.exception))

    def test_safe_redirect_converts_post_to_get_drops_body_and_host_bound_headers(self):
        self.session.responses=[(303,{"Location":"https://secure8.store.apple.com/shop/checkout/start"},b''),
                                (200,{},b'review')]
        self.client.request("POST","https://www.apple.com/shop/checkout",headers={"X-AOS-STK":"PRIVATE"},fields={"a":"b"})
        method,url,kwargs=self.session.calls[1]
        self.assertEqual(method,"GET")
        self.assertIsNone(kwargs["data"])
        self.assertNotIn("X-AOS-STK",kwargs["headers"])

    def test_mutation_redirect_replay_is_blocked(self):
        for status,method,url in ((307,"POST","https://www.apple.com/shop/checkout"),
                                  (302,"GET","https://www.apple.com/shop/bag/add")):
            self.session.responses=[(status,{"Location":"/shop/bag/add?product=MHRN4LL%2FA"},b'')]
            with self.assertRaises(APIError):self.client.request(method,url)
        self.assertEqual(len(self.session.calls),2)

    def test_network_error_is_redacted_consumes_one_attempt_and_marks_cart_uncertain(self):
        self.session.responses=[OSError("PRIVATE_PAN_AND_TOKEN")]
        with self.assertRaises(APIError) as caught:
            self.client.request("GET","https://www.apple.com/shop/bag/add",fields={"product":"MHRN4LL/A","atbtoken":"PRIVATE"})
        self.assertEqual(len(self.session.calls),1)
        self.assertTrue(caught.exception.ambiguous)
        self.assertNotIn("PRIVATE",str(caught.exception))

    def test_headers_cannot_override_host_cookie_or_escape_into_new_header(self):
        for headers in ({"Host":"evil.test"},{"Cookie":"PRIVATE"},{"Referer":"https://evil.test/"},
                        {"X-AOS-STK":"ok\r\nCookie: PRIVATE"}):
            with self.assertRaises(APIError):self.client.request("GET","https://www.apple.com/shop/bag",headers=headers)
        self.assertEqual(self.session.calls,[])

    def test_deleted_cookie_is_not_resurrected_from_previous_saved_snapshot(self):
        self.client._jar.set_cookie(cookie("cart-fixture","old"))
        self.client._save()
        self.client._jar.clear("www.apple.com","/","cart-fixture")
        self.client._save()
        reopened=AppleClient(self.root,"fixture-run",session_factory=FakeSession)
        try:self.assertIsNone(reopened.get_cookie("cart-fixture"))
        finally:reopened.close()

    def test_document_navigation_headers_are_absent_from_api_and_beacon_requests(self):
        self.session.responses=[(200,{},b''),(200,{},b''),(200,{},b'')]
        self.client.request("GET","https://www.apple.com/shop/bag")
        self.client.request("GET","https://www.apple.com/shop/retail/pickup-message?pl=true")
        self.client.request("GET","https://www.apple.com/shop/beacon/atb")
        doc,api,beacon=[c[2]["headers"] for c in self.session.calls]
        self.assertEqual(doc["sec-fetch-mode"],"navigate")
        self.assertEqual(doc["sec-fetch-user"],"?1")
        for headers in (api,beacon):
            self.assertNotIn("sec-fetch-user",headers)
            self.assertNotIn("upgrade-insecure-requests",headers)
        self.assertEqual(api["sec-fetch-mode"],"cors")
        self.assertEqual(api["sec-fetch-dest"],"empty")
        self.assertEqual(beacon["sec-fetch-dest"],"image")
        self.assertEqual(api["sec-ch-ua-platform"],'"Windows"')

    def test_host_scoped_cookie_lookup_does_not_export_foreign_or_expired_cookie(self):
        self.client._jar.set_cookie(cookie("as_atb","PRIVATE_FOREIGN",domain="evil.test"))
        self.assertIsNone(self.client.get_cookie("as_atb"))
        self.client._jar.set_cookie(cookie("as_atb","PRIVATE_EXPIRED",expires=1))
        self.assertIsNone(self.client.get_cookie("as_atb"))
        self.client._jar.set_cookie(cookie("as_atb","one|two|right",domain="secure8.store.apple.com"))
        self.assertIsNone(self.client.get_cookie("as_atb"))
        self.assertEqual(self.client.get_cookie("as_atb","https://secure8.store.apple.com/shop/bag"),"one|two|right")
        self.client._jar.set_cookie(cookie("checkout-fixture","scoped",domain=".store.apple.com"))
        self.assertIsNone(self.client.get_cookie("checkout-fixture"))
        self.assertEqual(self.client.get_cookie("checkout-fixture","https://secure8.store.apple.com/shop/bag"),"scoped")

    def test_checkout_verification_uses_current_origin_and_keeps_existing_cart_jar(self):
        self.client._jar.set_cookie(cookie("cart-fixture","KEEP_ME",domain=".apple.com"))
        challenge={"challenge_version":3,"algorithm":"SHA-384","timeout":1000,
          "salt":"fixture-salt", "challenge":"6ab617ab616c61832541d7cf8e509f88f5b5743c9088dc952de5e4be8d4461b231c9484aa347f487b0fc5050fb61807a",
          "signature":"fixture-signature","X":"fixture-X"}
        def issued(session,method,url,kwargs):
            session.cookies.jar.set_cookie(cookie("shld_bt_ck","fixture|4102444800",domain="secure8.store.apple.com"))
            return 200,{},b'{}'
        self.session.responses=[(200,{},json.dumps(challenge).encode()),issued]
        report=self.client.verify_session("https://secure8.store.apple.com",client_profile="native_compatible_emulation")
        self.assertTrue(report["verification_cookie_valid"])
        self.assertEqual(len(self.session.calls),2) # No www image beacon during checkout renewal.
        self.assertTrue(all(c[1].startswith("https://secure8.store.apple.com/") for c in self.session.calls))
        submitted=json.loads(self.session.calls[1][2]["data"])
        self.assertEqual(submitted["flagskv"],{"patSkip":True})
        self.assertEqual(submitted["jsa"]["s"],30)
        self.assertEqual(self.client.get_cookie("cart-fixture"),"KEEP_ME")
        self.assertNotIn("fixture-salt",json.dumps(report))
        self.assertEqual(report["client_profile"],"native_compatible_emulation")

    def test_response_repr_does_not_include_body_headers_or_query_token(self):
        r=Response(200,"https://www.apple.com/shop/bag?token=PRIVATE",b'PRIVATE',{"x":"PRIVATE"})
        self.assertNotIn("PRIVATE",repr(r))

    def test_wheel_paths_and_pins_are_fixed(self):
        for path in ("../escape","/absolute","C:/absolute","package\\escape"):
            with self.assertRaises(ValueError):validate_member(path,self.root)
        validate_member("curl_cffi/module.py",self.root)
        self.assertEqual({p[1] for p in PINS},{"0.17.0","2.1.1","3.12","2026.10.23","2026.5"})


if __name__=="__main__":unittest.main()
