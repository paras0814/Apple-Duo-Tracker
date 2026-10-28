"""Adversarial offline full-flow fixtures. No Apple, profile or cookie access."""
import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qsl, urlsplit

from beta.apple_client import APIError, Response
from beta.flow import run_flow, validate_plan, validate_review, product_quote
from beta.payment import EncryptedCard, PrivateBillingPayload

WWW="https://www.apple.com"; SECURE="https://secure8.store.apple.com"
PART="MHRN4LL/A"; ITEM="item-00000000-0000-0000-0000-000000000001"


def action(name,module,path="/shop/checkoutx/fulfillment"):
    return {"url":path+"?_a="+name+"&_m="+module}


def metadata():return {"h":{"x-aos-stk":"PRIVATE_FIXTURE_MODEL_TOKEN"}}
def html(model):return ('<script id="init_data" type="application/json">'+json.dumps(model)+'</script>').encode()
def response(model,status=200,url=SECURE+"/shop/checkout"):
    return Response(status,url,json.dumps(model).encode(),{"content-type":"application/json"})
def delta(body):return {"head":{"status":200},"body":{"meta":metadata(),**body}}


def bag(empty=False):
    items={"d":{"bagCount":0 if empty else 1}}
    if not empty:
        items.update(c=[ITEM],**{ITEM:{"d":{"sku":PART,"name":"iPhone 17e 256GB Black","unitPrice":"$699.00","totalPrice":"$699.00","hasAppleCareInstallment":False},
            "c":["itemQuantity","delivery"],"itemQuantity":{"d":{"quantity":1},"b":{"quantity":{"name":"quantity"}}}}})
    return {"meta":metadata(),"shoppingCart":{"d":{"hasItems":not empty},"items":items,
        "actions":{"a":{"checkout":action("checkout","shoppingCart.actions","/shop/bagx/checkout_now")}}}}


def product():
    p={"@type":"Product","name":"iPhone 17e 256GB Black",
       "offers":{"sku":PART+"+unlocked","priceCurrency":"USD","price":"699.00"}}
    return ('<script type="application/ld+json">'+json.dumps(p)+'</script>').encode()


def initial():
    return {"meta":metadata(),"checkout":{"fulfillment":{"fulfillmentOptions":{"a":{
        "selectFulfillmentLocationAction":action("selectFulfillmentLocationAction","checkout.fulfillment.fulfillmentOptions")}}}}}


def pickup():
    return {"storeLocator":{"a":{"select":action("select","checkout.fulfillment.pickupTab.pickup.storeLocator")}}}


def store_pickup(store_id="R654"):
    p=pickup();p["storeLocator"]["d"]={"selectStore":store_id}
    day=(datetime.now(timezone.utc)+timedelta(days=1)).date()
    w={"SlotId":"PRIVATE_SLOT_ID","signKey":"PRIVATE_SLOT_SIGNATURE","timeSlotValue":"PRIVATE_WINDOW_VALUE",
       "checkInStart":"9:15 AM","checkInEnd":"9:30 AM","timeZone":"America/New_York","isRestricted":False}
    p["timeSlot"]={"d":{"storeId":store_id},"dateTimeSlots":{"d":{"storeSelected":store_id,
       "pickUpDates":[{"date":day.isoformat(),"dayOfMonth":day.day}],"timeSlotWindows":[{str(day.day):[w]}]}}}
    return p


def contact():
    return {"pickupContactOptions":{"d":{"selectedPickupOption":"SELF"}},"a":{
        "continueFromPickupContactToBilling":action("continueFromPickupContactToBilling","checkout.pickupContact","/shop/checkoutx")}}


def billing(selected=False):
    options={"a":{"selectBillingOptionAction":action("selectBillingOptionAction","checkout.billing.billingOptions","/shop/checkoutx/billing")}}
    if selected:
        options["selectedBillingOptions"]={"creditCard":{
            "cardInputs":{"cardInput-0":{"d":{"encryption":{"config":{"publicKey":"FIXTURE_RSA_KEY","publicKeyHash":"FIXTURE_PUBLIC_HASH"}},
                       "encryption-securityCode":{"config":{"publicKey":"FIXTURE_RSA_KEY","publicKeyHash":"FIXTURE_PUBLIC_HASH"}}}}},
            "billingAddress":{"address":{"zipLookup":{"a":{"zipLookup":action("zipLookup",
                "checkout.billing.billingOptions.selectedBillingOptions.creditCard.billingAddress.address.zipLookup","/shop/checkoutx/billing")}}}}}}
    return {"billingOptions":options,"a":{"continueFromBillingToReview":action("continueFromBillingToReview","checkout.billing","/shop/checkoutx/billing")}}


def review():
    return {"fulfillmentReview":{"reviewGroup-1":{"d":{"pickup":{"storeName":"Apple World Trade Center","storeId":"R654"}},
        ITEM:{"d":{"sku":PART,"unitPrice":"$699.00","totalPrice":"$699.00","hasAppleCareInstallment":False},"itemQuantity":{"d":{"quantity":1}}}}},
        "summary":{"d":{"subtotal":"$699.00","tax":"$62.04","total":"$761.04"}},
        "placeOrder":{"a":{"continueFromReviewToProcess":action("continueFromReviewToProcess","checkout.review.placeOrder","/shop/checkoutx/review")},
                      "termsAndConditions":{"d":{"headerLabel":"Terms"},"b":{}}}}


class Guard:
    run_id="fixture-run"
    def __init__(self):self.status={};self.claims=[];self.points=[];self.current={};self.stopped=False;self.order_grant=False
    def progress(self,stage,message):pass
    def checkpoint(self,phase,facts):self.points.append(phase);self.current=copy.deepcopy(facts)
    def claim(self,key,phase):
        if key in self.status:raise RuntimeError("No repeat mutation")
        if key=="place_order":
            if not self.order_grant:raise RuntimeError("No one-run order grant")
            self.order_grant=False
        self.status[key]="claimed";self.claims.append((key,phase));return "fixture-intent-"+key
    def complete(self,key,token,outcome,facts=None):
        if self.status.get(key)!="claimed":return False
        self.status[key]=outcome;return True
    def cancelled(self):return self.stopped
    def has_order_grant(self,review):return self.order_grant and self.current.get("total_cents")==review.get("total_cents")
    def resume_info(self):return {"facts":self.current,"requires_reconciliation":any(v in {"claimed","unknown"} for v in self.status.values()),
        "mutations":[{"mutation_key":k,"status":v} for k,v in self.status.items()]}


class Client:
    last=None;fail_guest=False;wrong_store=False;atc_ambiguous=False;resume=False;billing_failure=False;order_mode="accepted";price_changed=False;guest_hashcash=False
    def __init__(self,root,run_id):
        Client.last=self;self.calls=[];self.proofs=[];self.has_item=self.resume;self.closed=False;self.billing_done=False;self.warm=False
        self.root=Path(root).resolve();self.cookie_file=self.root/".data/api-runs"/(run_id+".cookies")
    def verification_cookie_valid(self,origin=WWW):return self.warm and origin==WWW
    def verify_session(self,origin,client_profile):self.proofs.append((origin,client_profile));return {"verification_cookie_valid":True}
    def atb_token(self):return "PRIVATE_FIXTURE_ATB_TOKEN"
    def close(self):self.closed=True
    def request(self,method,url,headers=None,fields=None):
        self.calls.append((method,url,copy.deepcopy(fields),copy.deepcopy(headers)))
        p=urlsplit(url);a=dict(parse_qsl(p.query)).get("_a")
        if method=="GET" and p.path=="/shop/bag":return Response(200,url,html(bag(not self.has_item)),{})
        if method=="GET" and p.path=="/shop/retail/pickup-message":
            return response({"head":{"status":200},"body":{"stores":[{"storeNumber":"R654","state":"NY","partsAvailability":{PART:{"partNumber":PART,"pickupDisplay":"available",
                "messageTypes":{"regular":{"storePickupProductTitle":"iPhone 17e 256GB Black"}}}}}]}})
        if method=="GET" and "/shop/buy-iphone/" in p.path:return Response(200,url,product(),{})
        if method=="GET" and p.path=="/shop/bag/add":
            self.has_item=True
            if self.atc_ambiguous:raise APIError("request","Ambiguous fixture add",ambiguous=True)
            return Response(200,url,b'',{})
        if method=="POST" and a=="checkout":return response({"head":{"status":302,"data":{"url":SECURE+"/shop/signIn?session=PRIVATE_REDIRECT_TOKEN"}}})
        if method=="GET" and p.path=="/shop/signIn":
            signin={"d":{"guestCheckoutEnabled":True},"guestLogin":{"a":{"guestLogin":action("guestLogin","signIn.guestLogin","/shop/signInx")}}}
            if self.guest_hashcash:signin["guestLogin"]["d"]={"hashcashChallenge":"PRIVATE_CURRENT_CHALLENGE"}
            return Response(200,url,html({"meta":metadata(),"signIn":signin}),{})
        if method=="POST" and a=="guestLogin":
            if self.fail_guest:return Response(541,url,b'PRIVATE_SERVER_CARD_OR_CONTACT_ECHO',{})
            return response({"head":{"status":302,"data":{"url":SECURE+"/shop/checkout/start","method":"post","args":{"pltn":"PRIVATE_PLTN"}}}})
        if method=="POST" and p.path=="/shop/checkout/start":return Response(200,url,html(initial()),{})
        if method=="GET" and p.path=="/shop/checkout":
            if self.billing_done:
                value=review()
                if self.price_changed:value["summary"]["d"]["total"]="$762.04"
                return Response(200,url,html({"meta":metadata(),"checkout":{"review":value}}),{})
            return Response(200,url,html({"meta":metadata(),"checkout":{"pickupContact":contact()}}),{})
        if method=="POST" and a=="selectFulfillmentLocationAction":return response(delta({"checkout":{"fulfillment":{"pickupTab":{"pickup":pickup()}}}}))
        if method=="POST" and a=="select":
            return response(delta({"checkout":{"fulfillment":{"pickupTab":{"pickup":store_pickup("R032" if self.wrong_store else "R654")},
                "a":{"continueFromFulfillmentToPickupContact":action("continueFromFulfillmentToPickupContact","checkout.fulfillment")}}}}))
        if method=="POST" and a=="continueFromFulfillmentToPickupContact":return response(delta({"checkout":{"pickupContact":contact()}}))
        if method=="POST" and a=="continueFromPickupContactToBilling":return response(delta({"checkout":{"billing":billing()}}))
        if method=="POST" and a=="selectBillingOptionAction":return response(delta({"checkout":{"billing":billing(True)}}))
        if method=="POST" and a=="zipLookup":return response(delta({"checkout":{"billing":billing(True)}}))
        if method=="POST" and a=="continueFromBillingToReview":
            if self.billing_failure:return response(delta({"checkout":{"billing":{"errors":{"message":"Please enter a valid expiration. PRIVATE_CARD_ECHO"}}}}))
            self.billing_done=True
            return response(delta({"checkout":{"review":review()}}))
        if method=="POST" and a=="continueFromReviewToProcess":
            if self.order_mode=="timeout":raise APIError("request","PRIVATE_NETWORK_ERROR",ambiguous=True)
            if self.order_mode=="accepted":return response(delta({"checkout":{"d":{"page":"thankyou"},"thankYou":{"d":{"orderNumber":"PRIVATE-ORDER-12345"}}}}))
            poll=action("pollingProcess","spinner","/shop/checkoutx")
            if self.order_mode=="external_poll":poll["url"]="https://secure9.store.apple.com"+poll["url"]
            return response(delta({"spinner":{"a":{"pollingProcess":poll}}}))
        if method=="POST" and a=="pollingProcess":
            if self.order_mode=="declined":return response(delta({"checkout":{"m":[{"level":"error","text":"Your card was declined PRIVATE_CARD_CONTACT_ECHO"}]}}))
            return response(delta({"spinner":{"a":{"pollingProcess":action("pollingProcess","spinner","/shop/checkoutx")}}}))
        raise AssertionError("Unexpected offline fixture request")


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        (self.root/"catalog.json").write_text(json.dumps({"stores":[{"id":"R654","name":"World Trade Center","zip":"10007","state":"NY"}]}))
        self.plan={"run_id":"fixture-run","part":PART,"model":"iPhone 17e","storage":"256GB","color":"Black","quantity":1,"applecare":False,
            "mode":"review_only","is_test":True,"store_id":"R654","store_name":"World Trade Center","store_zip":"10007","max_total_cents":230000,
            "buy_url":WWW+"/shop/buy-iphone/iphone-17e/6.1-inch-display-256gb-black-unlocked"}
        private=self.root/".data";private.mkdir()
        self.profile=private/"fixture-profile.json"
        self.profile.write_text(json.dumps({"mode":"dummy_decline_test","contact":{"first_name":"Test","last_name":"Buyer","email":"fiction@example.com","phone":"2025550123"},
          "billing":{"address_line_1":"123 Example Street","address_line_2":"","city":"New York","state":"NY","postal_code":"10001","country":"US"},
          "card":{"number":"4000000000000002","security_code":"123","expiry_month":12,"expiry_year":2030}}))
        self.guard=Guard()
        Client.last=None;Client.fail_guest=False;Client.wrong_store=False;Client.atc_ambiguous=False;Client.resume=False;Client.billing_failure=False
        Client.order_mode="accepted";Client.price_changed=False;Client.guest_hashcash=False
        self.patches=[patch("beta.flow.AppleClient",Client),patch("beta.flow.encrypt_card",return_value=EncryptedCard("PRIVATE_ENCRYPTED_PAN","PRIVATE_ENCRYPTED_CVV")),
                      patch("beta.flow.build_us_billing_payload",return_value=PrivateBillingPayload({"cardNumber":"PRIVATE_ENCRYPTED_PAN","securityCode":"PRIVATE_ENCRYPTED_CVV"}))]
        for p in self.patches:p.start()
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.temp.cleanup()
    def run_flow(self,**kwargs):return run_flow(self.root,self.plan,self.guard,profile_path=self.profile,**kwargs)

    def test_full_pipeline_claims_before_each_mutation_and_stops_at_verified_review(self):
        result=self.run_flow();self.assertEqual(result["status"],"review_ready",result)
        self.assertEqual(result["review"]["total_cents"],76104)
        self.assertFalse(result["order_placed"])
        self.assertTrue(Client.last.closed)
        keys=[k for k,_ in self.guard.claims]
        self.assertEqual(keys,["add_to_cart","checkout_entry","guest_login","checkout_start","select_pickup","select_store","select_slot","pickup_contact","select_credit","zip_lookup","payment_validation"])
        self.assertTrue(all(v=="confirmed" for v in self.guard.status.values()))
        self.assertEqual([p[0] for p in Client.last.proofs],[WWW,SECURE])
        actions=[dict(parse_qsl(urlsplit(c[1]).query)).get("_a") for c in Client.last.calls]
        self.assertNotIn("continueFromReviewToProcess",actions)
        self.assertNotIn("PRIVATE_",json.dumps(result))
        self.assertNotIn("fiction@example.com",json.dumps(result))

    def test_atc_uses_generic_public_route_exact_quantity_and_no_private_key(self):
        self.run_flow()
        calls=[c for c in Client.last.calls if urlsplit(c[1]).path=="/shop/bag/add"]
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0][2],{"product":PART,"atbtoken":"PRIVATE_FIXTURE_ATB_TOKEN","purchaseOption":"fullPrice","carrierModel":"UNLOCKED/US","acpart":"none","quantity":"1"})
        self.assertNotIn("api",json.dumps(calls[0][2]))

    def test_guest_541_never_repeats_or_continues_and_does_not_echo_response(self):
        Client.fail_guest=True;result=self.run_flow()
        self.assertEqual(result["status"],"needs_attention")
        self.assertEqual(self.guard.status["guest_login"],"unknown")
        self.assertNotIn("checkout_start",self.guard.status)
        calls=[c for c in Client.last.calls if dict(parse_qsl(urlsplit(c[1]).query)).get("_a")=="guestLogin"]
        self.assertEqual(len(calls),1)
        self.assertNotIn("PRIVATE_",json.dumps(result))

    def test_ambiguous_add_is_reconciled_from_exact_bag_without_another_add(self):
        Client.atc_ambiguous=True;result=self.run_flow()
        self.assertEqual(result["status"],"review_ready",result)
        self.assertEqual(len([c for c in Client.last.calls if urlsplit(c[1]).path=="/shop/bag/add"]),1)
        self.assertEqual(self.guard.status["add_to_cart"],"confirmed")

    def test_wrong_store_cannot_advance_to_contact_billing_or_order(self):
        Client.wrong_store=True;result=self.run_flow()
        self.assertEqual(result["status"],"needs_attention")
        self.assertEqual(self.guard.status["select_store"],"unknown")
        self.assertNotIn("select_slot",self.guard.status)
        self.assertNotIn("payment_validation",self.guard.status)

    def test_billing_validation_error_is_not_an_issuer_decline_and_is_not_retried(self):
        Client.billing_failure=True;result=self.run_flow()
        self.assertEqual(result["status"],"needs_attention")
        self.assertEqual(result["failure"]["classification"],"field_validation")
        self.assertNotIn("apple_decline",result["failure"])
        self.assertNotIn("PRIVATE_CARD_ECHO",json.dumps(result))
        self.assertEqual([k for k,_ in self.guard.claims].count("payment_validation"),1)

    def test_resume_reads_fresh_models_and_does_not_repeat_confirmed_cart_or_pickup(self):
        Client.resume=True
        done=["add_to_cart","checkout_entry","guest_login","checkout_start","select_pickup","select_store","select_slot"]
        self.guard.status={key:"confirmed" for key in done}
        self.guard.current={"checkout_host":"secure8.store.apple.com","part":PART,"quantity":1,"applecare":False,"base_price_cents":69900,"store_id":"R654"}
        result=self.run_flow();self.assertEqual(result["status"],"review_ready",result)
        self.assertNotIn("add_to_cart",[k for k,_ in self.guard.claims])
        self.assertEqual([k for k,_ in self.guard.claims],["pickup_contact","select_credit","zip_lookup","payment_validation"])
        self.assertTrue(any(c[0]=="GET" and urlsplit(c[1]).path=="/shop/checkout" for c in Client.last.calls))

    def test_unknown_prior_mutation_stops_before_constructing_or_using_client(self):
        self.guard.status={"add_to_cart":"unknown"}
        result=self.run_flow()
        self.assertEqual(result["stage"],"reconciliation")
        self.assertIsNone(Client.last)

    def test_bad_plan_and_stopped_worker_never_add(self):
        for change in ({"quantity":2},{"applecare":True},{"max_total_cents":230001},{"store_zip":"99999"},{"color":"White"}):
            plan={**self.plan,**change};result=run_flow(self.root,plan,Guard())
            self.assertEqual(result["status"],"needs_attention")
            self.assertIsNone(Client.last)
        self.guard.stopped=True;result=self.run_flow()
        self.assertEqual(result["stage"],"stopped")
        self.assertEqual(Client.last.calls,[])

    def test_review_binds_one_sku_store_price_care_and_actual_tax_total(self):
        base={"checkout":{"review":review()}}
        facts={"base_price_cents":69900}
        self.assertEqual(validate_review(base,self.plan,facts)["total_cents"],76104)
        cases=[]
        v=copy.deepcopy(base);v["checkout"]["review"]["summary"]["d"]["total"]="$2,300.01";cases.append(v)
        v=copy.deepcopy(base);v["checkout"]["review"]["summary"]["d"]["tax"]="$60.00";cases.append(v)
        v=copy.deepcopy(base);v["checkout"]["review"]["fulfillmentReview"]["reviewGroup-1"]["d"]["pickup"]["storeId"]="R032";cases.append(v)
        v=copy.deepcopy(base);v["checkout"]["review"]["fulfillmentReview"]["reviewGroup-1"][ITEM]["itemQuantity"]["d"]["quantity"]=2;cases.append(v)
        v=copy.deepcopy(base);v["checkout"]["review"]["fulfillmentReview"]["reviewGroup-1"]["applecare"]={"d":{"plan":"selected"}};cases.append(v)
        for v in cases:
            with self.assertRaises(APIError):validate_review(v,self.plan,facts)

    def test_quote_requires_exact_unlocked_sku_currency_and_storage(self):
        self.assertEqual(product_quote(product(),self.plan)["base_price_cents"],69900)
        for bad in (product().replace(b'256GB',b'512GB'),product().replace(b'USD',b'CAD'),product().replace(b'+unlocked',b'+locked')):
            with self.assertRaises(APIError):product_quote(bad,self.plan)

    def test_new_iphone17_white_test_is_bound_to_approved_exact_variant(self):
        plan={**self.plan,"part":"MG474LL/A","model":"iPhone 17","color":"White",
              "buy_url":WWW+"/shop/buy-iphone/iphone-17/6.3-inch-display-256gb-white-unlocked"}
        self.assertEqual(validate_plan(self.root,plan)["part"],"MG474LL/A")
        for change in ({"is_test":False},{"storage":"512GB"},
                       {"buy_url":plan["buy_url"]+"-another-variant"}):
            with self.assertRaises(APIError):validate_plan(self.root,{**plan,**change})

    def test_review_html_pickup_quote_requires_prior_verified_store_identity(self):
        model={"checkout":{"review":review()}}
        pickup=model["checkout"]["review"]["fulfillmentReview"]["reviewGroup-1"]["d"]["pickup"]
        pickup.clear();pickup.update(quote='<p>Pick up at <b>Apple World Trade Center</b>.</p>',timeslot='Tomorrow 9:15 AM')
        facts={"base_price_cents":69900,"store_id":"R654","store_confirmed":True}
        self.assertEqual(validate_review(model,self.plan,facts)["store_id"],"R654")
        with self.assertRaises(APIError):validate_review(model,self.plan,{"base_price_cents":69900})
        pickup["quote"]='<script>Apple World Trade Center</script><b>Apple SoHo</b>'
        with self.assertRaises(APIError):validate_review(model,self.plan,facts)

    def approve_order(self,review):
        self.assertEqual(self.guard.points[-1],"review_ready")
        self.assertIn("review_observed_at",self.guard.current)
        self.guard.order_grant=True
        return True

    def final_posts(self):
        return [c for c in Client.last.calls if c[0]=="POST" and dict(parse_qsl(urlsplit(c[1]).query)).get("_a")=="continueFromReviewToProcess"]

    def test_accepted_order_requires_fresh_review_callback_grant_and_one_final_post(self):
        result=self.run_flow(allow_submit_once=True,authorize_final=self.approve_order)
        self.assertEqual(result["status"],"order_accepted",result)
        self.assertTrue(result["order_placed"])
        self.assertEqual(result["order_result"],"accepted")
        self.assertTrue(result["apple_response_confirmed"])
        self.assertEqual(self.guard.status["place_order"],"confirmed")
        self.assertEqual(len(self.final_posts()),1)
        self.assertEqual(dict(self.final_posts()[0][2]),{})
        self.assertIn("final_review_get",result["timings_ms"])
        self.assertNotIn("PRIVATE",json.dumps(result))
        self.assertNotIn("fiction@example.com",json.dumps(result))

    def test_boolean_or_callback_without_actual_grant_cannot_send_order(self):
        result=self.run_flow(allow_submit_once=True)
        self.assertEqual(result["stage"],"order_authorization")
        self.assertEqual(self.final_posts(),[])
        self.assertNotIn("place_order",self.guard.status)
        # A second test run with a callback that lies about creating a grant.
        self.guard=Guard();Client.last=None
        result=self.run_flow(allow_submit_once=True,authorize_final=lambda review:True)
        self.assertEqual(result["stage"],"order_authorization")
        self.assertEqual(self.final_posts(),[])

    def test_real_apple_decline_after_advertised_poll_is_rejected_without_replay(self):
        Client.order_mode="declined"
        result=self.run_flow(allow_submit_once=True,authorize_final=self.approve_order)
        self.assertEqual(result["stage"],"payment_declined",result)
        self.assertEqual(result["order_result"],"declined")
        self.assertTrue(result["failure"]["apple_decline"])
        self.assertEqual(self.guard.status["place_order"],"rejected")
        self.assertEqual(result["order_polls"],1)
        self.assertEqual(len(self.final_posts()),1)
        self.assertNotIn("PRIVATE",json.dumps(result))

    def test_ambiguous_final_timeout_consumes_grant_without_automatic_retry(self):
        Client.order_mode="timeout"
        result=self.run_flow(allow_submit_once=True,authorize_final=self.approve_order)
        self.assertEqual(result["order_result"],"unknown",result)
        self.assertIsNone(result["order_placed"])
        self.assertEqual(self.guard.status["place_order"],"unknown")
        self.assertFalse(self.guard.order_grant)
        self.assertEqual(len(self.final_posts()),1)
        self.assertNotIn("PRIVATE",json.dumps(result))

    def test_polling_is_bounded_and_cannot_change_checkout_host(self):
        Client.order_mode="processing_forever"
        with patch("beta.flow.ORDER_POLL_LIMIT",3),patch("beta.flow.time.sleep"):
            result=self.run_flow(allow_submit_once=True,authorize_final=self.approve_order)
        self.assertEqual(result["order_result"],"unknown")
        self.assertEqual(result["order_polls"],3)
        self.assertEqual(len(self.final_posts()),1)
        self.guard=Guard();Client.order_mode="external_poll"
        result=self.run_flow(allow_submit_once=True,authorize_final=self.approve_order)
        self.assertEqual(result["order_result"],"unknown")
        self.assertFalse(any("secure9" in c[1] for c in Client.last.calls))
        self.assertEqual(len(self.final_posts()),1)

    def test_changed_fresh_review_cannot_use_earlier_checkpoint_or_authorization(self):
        Client.price_changed=True;approved=[]
        result=self.run_flow(allow_submit_once=True,authorize_final=lambda r:approved.append(r) or True)
        self.assertEqual(result["status"],"needs_attention")
        self.assertEqual(approved,[])
        self.assertEqual(self.final_posts(),[])
        self.assertNotIn("place_order",self.guard.status)

    def test_prepared_session_is_bound_reused_and_closed_even_on_early_failure(self):
        client=Client(self.root,self.plan["run_id"]);client.warm=True
        result=self.run_flow(prepared_client=client)
        self.assertEqual(result["status"],"review_ready",result)
        self.assertTrue(result["prepared_session_reused"])
        self.assertEqual([p[0] for p in client.proofs],[SECURE])
        self.assertTrue(client.closed)
        wrong=Client(self.root,"another-run")
        self.guard=Guard()
        result=self.run_flow(prepared_client=wrong)
        self.assertEqual(result["stage"],"prepared_session")
        self.assertEqual(wrong.calls,[])
        self.assertTrue(wrong.closed)

    def test_advertised_unsupported_guest_hashcash_stops_before_guest_claim(self):
        Client.guest_hashcash=True;result=self.run_flow()
        self.assertEqual(result["stage"],"guest_verification")
        self.assertNotIn("guest_login",self.guard.status)
        self.assertNotIn("PRIVATE",json.dumps(result))


if __name__=="__main__":unittest.main()
