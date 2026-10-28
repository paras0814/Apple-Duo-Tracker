"""One-phone API preparation, with durable intents before checkout mutations.

Page models, server tokens and private profile fields remain in memory. Only
allowlisted facts/timings leave this module. The default ends at verified review.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import time
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from beta.apple_client import (APIError, AppleClient, MAX_TOTAL_CENTS, PARTS,
                               assert_empty_bag, parse_init_data, safe_url, validate_bag)
from beta.payment import (PaymentError, build_us_billing_payload,
                          classify_apple_payment_response, encrypt_card, validate_profile)
from beta.slots import SlotError, choose_earliest_slot

WWW = "https://www.apple.com"


class BetaGuard:
    """Bind CheckoutBeta's durable journal to one current worker/run."""
    def __init__(self, beta, run_id, stop=None):
        self.beta, self.run_id, self.stop = beta, run_id, stop
    def progress(self, stage, message):
        return self.beta.progress(self.run_id, stage, message)
    def checkpoint(self, phase, facts):
        return self.beta.checkpoint(self.run_id, phase, facts)
    def claim(self, key, phase):
        return self.beta.claim_mutation(self.run_id, key, phase)
    def complete(self, key, token, outcome, facts=None):
        return self.beta.complete_mutation(self.run_id, key, token, outcome, facts)
    def resume_info(self):
        return self.beta.resume_info(self.run_id)
    def cancelled(self):
        return bool(self.stop and self.stop.is_set()) or self.beta.cancelled(self.run_id)


def _node(model, *path):
    for key in path:
        if not isinstance(model, dict) or key not in model:
            raise APIError("model", "The current Apple checkout model is incomplete.")
        model = model[key]
    if not isinstance(model, dict):
        raise APIError("model", "The current Apple checkout module is unavailable.")
    return model


def _merge(model, patch):
    """Merge a server module delta; never infer that omitted modules advanced."""
    result = deepcopy(model)
    for key, value in patch.items():
        result[key] = _merge(result[key], value) if isinstance(result.get(key), dict) and isinstance(value, dict) else deepcopy(value)
    return result


def _amount(value, *, zero=False):
    if not isinstance(value, str) or not re.fullmatch(r"\$(?:\d+|\d{1,3}(?:,\d{3})+)\.\d{2}", value):
        raise APIError("price", "Apple did not confirm an exact USD amount.")
    try:
        amount = int(Decimal(value[1:].replace(",", "")) * 100)
    except (InvalidOperation, ValueError):
        raise APIError("price", "The Apple amount is invalid.") from None
    if amount < 0 or (not zero and not amount):
        raise APIError("price", "The Apple amount is invalid.")
    return amount


def validate_plan(root, plan):
    if not isinstance(plan, dict):
        raise APIError("plan", "A selected API beta plan is required.")
    config = PARTS.get(plan.get("part"))
    if (not config or config != (plan.get("model"), plan.get("storage"), plan.get("color"))
            or type(plan.get("quantity")) is not int or plan["quantity"] != 1
            or plan.get("applecare") is not False or plan.get("mode") != "review_only"
            or type(plan.get("max_total_cents")) is not int or plan["max_total_cents"] != MAX_TOTAL_CENTS
            or not isinstance(plan.get("run_id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", plan["run_id"])
            or type(plan.get("is_test")) is not bool or plan["is_test"] != (plan["part"] in {"MHRN4LL/A","MG474LL/A"})):
        raise APIError("plan", "The API plan must select one supported 256GB phone, no AppleCare, and the $2,300 limit.")
    try:
        stores = json.loads((Path(root) / "catalog.json").read_text(encoding="utf8"))["stores"]
        store = next(s for s in stores if s["id"] == plan.get("store_id"))
        if store["name"] != plan.get("store_name") or store["zip"] != plan.get("store_zip"):
            raise ValueError()
    except (OSError, ValueError, KeyError, TypeError, StopIteration):
        raise APIError("plan", "The selected pickup store does not match the watched catalog.") from None
    buy = safe_url(plan.get("buy_url"))
    slug = {"MHRN4LL/A":"iphone-17e","MG474LL/A":"iphone-17"}.get(plan["part"],"iphone-duo")
    variant = "256gb-" + plan["color"].lower().replace(" ", "-") + "-unlocked"
    if urlsplit(buy).hostname != "www.apple.com" or not urlsplit(buy).path.startswith("/shop/buy-iphone/" + slug + "/") or variant not in urlsplit(buy).path:
        raise APIError("plan", "The model link does not match the selected unlocked phone.")
    test_paths={"MHRN4LL/A":"/shop/buy-iphone/iphone-17e/6.1-inch-display-256gb-black-unlocked",
                "MG474LL/A":"/shop/buy-iphone/iphone-17/6.3-inch-display-256gb-white-unlocked"}
    if plan["is_test"] and urlsplit(buy).path!=test_paths[plan["part"]]:
        raise APIError("plan", "The diagnostic phone must use its exact approved model page.")
    return {**plan, "buy_url":buy}


def product_quote(content, plan):
    """Bind a selected Product offer to the exact SKU+unlocked, not visible price text."""
    class Structured(HTMLParser):
        def __init__(self):
            super().__init__(); self.active=False; self.parts=[]; self.values=[]
        def handle_starttag(self, tag, attrs):
            if tag == "script" and dict(attrs).get("type") == "application/ld+json":
                self.active=True;self.parts=[]
        def handle_data(self, text):
            if self.active:self.parts.append(text)
        def handle_endtag(self, tag):
            if tag == "script" and self.active:
                self.active=False
                try:self.values.append(json.loads("".join(self.parts)))
                except ValueError:pass
    try:
        parser=Structured();parser.feed(content.decode("utf8"));parser.close()
        products=[]
        for value in parser.values:
            records=value if isinstance(value,list) else value.get("@graph",[value]) if isinstance(value,dict) else []
            products.extend(p for p in records if isinstance(p,dict) and p.get("@type") == "Product")
        matches=[]
        for product in products:
            name=product.get("name","")
            if not isinstance(name,str) or not all(re.search(r"\b" + re.escape(v) + r"\b",name,re.I) for v in (plan["model"],plan["storage"],plan["color"])):
                continue
            offers=product.get("offers",[]);offers=offers if isinstance(offers,list) else [offers]
            for offer in offers:
                if not isinstance(offer,dict) or offer.get("sku") != plan["part"] + "+unlocked" or offer.get("priceCurrency") != "USD":continue
                price=Decimal(str(offer.get("price")))
                if not price.is_finite() or price <= 0 or price*100 != (price*100).to_integral():continue
                matches.append(int(price*100))
        if len(matches) != 1 or matches[0] > plan["max_total_cents"]:
            raise ValueError()
        return {"part":plan["part"],"base_price_cents":matches[0],"currency":"USD","tax_included":False}
    except (ValueError, TypeError, InvalidOperation, UnicodeError, RecursionError):
        raise APIError("product_quote", "Apple did not independently confirm the exact unlocked product and price.") from None


def _action(node, key, origin, path, module, *, stage=None):
    try:url=safe_url(urljoin(origin, node["a"][key]["url"]))
    except (KeyError, TypeError, APIError):
        raise APIError("action", "The current Apple checkout action is unavailable.") from None
    parts=urlsplit(url); pairs=parse_qsl(parts.query,keep_blank_values=True)
    keys=[k for k,_ in pairs]
    args=dict(pairs)
    if (parts.hostname != urlsplit(origin).hostname or parts.path != path
            or keys.count("_a") != 1 or keys.count("_m") != 1
            or args.get("_a") != key or args.get("_m") != module):
        raise APIError("action", "Apple advertised an unsupported checkout action.")
    if stage:
        pairs=[(k,v) for k,v in pairs if k!="_s"] + [("_s",stage)]
        url=urlunsplit((parts.scheme,parts.netloc,parts.path,urlencode(pairs),""))
    return url


def _headers(model, origin, referer=None):
    meta=_node(model,"meta").get("h")
    if not isinstance(meta,dict) or not meta or not all(isinstance(k,str) and isinstance(v,str) for k,v in meta.items()):
        raise APIError("model_headers", "Apple did not advertise current checkout request headers.")
    return {**meta,"x-requested-with":"Fetch","Accept":"*/*",
            "Content-Type":"application/x-www-form-urlencoded","Origin":origin,
            "Referer":referer or origin+"/shop/checkout"}


def _head_redirect(response, *, path, origin=None, continuation=False):
    if response.status != 200:
        raise APIError("checkout_redirect", "Apple rejected the checkout transition.", response.status,ambiguous=True)
    value=response.json()
    try:
        if str(value["head"]["status"]) != "302":raise ValueError()
        data=value["head"]["data"]
        url=safe_url(data["url"])
        p=urlsplit(url)
        if not re.fullmatch(r"secure\d{0,3}\.store\.apple\.com",p.hostname or "") or p.path != path:raise ValueError()
        if origin and (p.scheme+"://"+p.netloc) != origin:raise ValueError()
        if continuation:
            if str(data.get("method","")).lower() != "post" or set(data.get("args",{})) != {"pltn"} or not isinstance(data["args"]["pltn"],str) or not data["args"]["pltn"]:raise ValueError()
        return data,url
    except (KeyError, TypeError, ValueError, APIError):
        raise APIError("checkout_redirect", "Apple returned an unsupported checkout continuation.",ambiguous=True) from None


def _has_errors(value, depth=0):
    if depth > 32:return True
    if isinstance(value,dict):
        for key,child in value.items():
            if key == "b":continue # Binding definitions contain validation strings.
            if key in {"errors","error","errorMessage","validationErrors","fieldErrors"} and child:return True
            if _has_errors(child,depth+1):return True
    elif isinstance(value,list):
        return any(_has_errors(v,depth+1) for v in value)
    return False


def _payload(response):
    if response.status != 200:
        raise APIError("checkout_response", "Apple rejected the checkout request; it will not be replayed.",response.status,ambiguous=True)
    value=response.json()
    if not isinstance(value,dict) or str(value.get("head",{}).get("status")) != "200" or not isinstance(value.get("body"),dict) or _has_errors(value["body"]):
        raise APIError("checkout_response", "Apple did not confirm the checkout transition.",response.status,ambiguous=True)
    return value["body"]


def _contains_care_module(value):
    if not isinstance(value,dict):return False
    for key,child in value.items():
        if key in {"b","a","f","t"}:continue
        if "applecare" in key.lower() and child not in (None,False,"",[],{}):return True
        if isinstance(child,dict) and _contains_care_module(child):return True
    return False


def _visible_quote(value):
    if not isinstance(value,str) or len(value)>32768:return ""
    class Text(HTMLParser):
        def __init__(self):super().__init__();self.parts=[];self.hidden=0
        def handle_starttag(self,tag,attrs):
            if tag in {"script","style"}:self.hidden+=1
        def handle_endtag(self,tag):
            if tag in {"script","style"} and self.hidden:self.hidden-=1
        def handle_data(self,text):
            if not self.hidden:self.parts.append(text)
    parser=Text();parser.feed(value);parser.close()
    return " ".join(" ".join(parser.parts).split())


def validate_review(model, plan, bag_facts):
    """Exact observed review line/quantity and summary amount paths only."""
    review=_node(model,"checkout","review")
    fulfillment=_node(review,"fulfillmentReview")
    groups=[v for k,v in fulfillment.items() if re.fullmatch(r"reviewGroup-\d+",k) and isinstance(v,dict)]
    lines=[]
    for group in groups:
        for child in group.values():
            if isinstance(child,dict) and isinstance(child.get("d"),dict) and child["d"].get("sku"):
                lines.append((group,child))
    if len(lines) != 1 or _contains_care_module(fulfillment):
        raise APIError("review", "The review does not confirm one phone without AppleCare.")
    group,item=lines[0]
    data=_node(item,"d");quantity=_node(item,"itemQuantity","d").get("quantity")
    if data.get("sku") != plan["part"] or type(quantity) is not int or quantity != 1:
        raise APIError("review", "The reviewed phone or quantity differs from this run.")
    unit,line=_amount(data.get("unitPrice")),_amount(data.get("totalPrice"))
    if unit != line or line != bag_facts.get("base_price_cents"):
        raise APIError("review", "The reviewed phone price differs from the verified bag.")
    pickup=_node(group,"d","pickup")
    ids=[];names=[]
    def public_store_fields(node):
        for key,value in node.items():
            if isinstance(value,dict):public_store_fields(value)
            elif isinstance(value,str):
                if key.lower() in {"storeid","store_id","storenumber"}:ids.append(value)
                if key.lower() in {"name","storename","storedisplayname","displayname","title"} or key.lower().endswith("storename"):names.append(value)
    public_store_fields(pickup)
    normalized=lambda s:re.sub(r"^apple\s+","",s.strip(),flags=re.I).casefold()
    quote=_visible_quote(pickup.get("quote"))
    store_pattern=r"(?<!\w)(?:Apple\s+)?"+r"\s+".join(re.escape(word) for word in plan["store_name"].split())+r"(?!\w)"
    quote_bound=(bag_facts.get("store_confirmed") is True and bag_facts.get("store_id")==plan["store_id"]
                 and bool(re.search(store_pattern,quote,re.I)))
    if (ids and (any(v != plan["store_id"] for v in ids))) or not (ids or any(normalized(n)==normalized(plan["store_name"]) for n in names) or quote_bound):
        raise APIError("review_store", "The review does not confirm the selected Apple pickup store.")
    summary=_node(review,"summary","d")
    subtotal,tax,total=_amount(summary.get("subtotal")),_amount(summary.get("tax"),zero=True),_amount(summary.get("total"))
    if subtotal != line or total != subtotal+tax or total > plan["max_total_cents"]:
        raise APIError("review_total", "The final tax-inclusive total is inconsistent or exceeds $2,300.")
    return {"part":plan["part"],"store_id":plan["store_id"],"quantity":1,"applecare":False,
            "currency":"USD","tax_included":True,"total_cents":total}


def final_order_hook(client, model, plan, guard, review):
    """Root integration may replace this with the separately authorized adapter."""
    return {"status":"needs_attention","stage":"order_adapter", "review":review,
            "message":"The phone reached verified review. Final order submission is not connected in this adapter.",
            "order_placed":False}


class _Flow:
    def __init__(self,root,plan,guard,profile_path,allow_submit_once):
        self.root,self.plan,self.guard=Path(root).resolve(),plan,guard
        self.profile_path,self.allow_submit_once=profile_path,allow_submit_once
        self.client=None;self.stage="plan";self.started=time.monotonic();self.timings={}
        self.phase_started=self.started
        self.facts={};self.journal={};self.profile=None;self.model=None;self.origin=WWW

    def stage_start(self,stage,message):
        if self.guard.cancelled():raise APIError("stopped","The API beta was stopped. Inspect its existing bag before resetting.")
        now=time.monotonic();self.timings[self.stage]=round((now-self.phase_started)*1000);self.phase_started=now
        self.stage=stage;self.guard.progress(stage,message)

    def checkpoint(self,phase,**facts):
        self.facts.update(facts)
        self.guard.checkpoint(phase,self.facts)

    def get(self,url,accept="text/html",referer=None):
        if self.guard.cancelled():raise APIError("stopped","The API beta was stopped before the next request.")
        started=time.monotonic()
        try:
            response=self.client.request("GET",url,headers={"Accept":accept,"Referer":referer or (self.origin+"/shop/checkout" if self.origin!=WWW else WWW+"/")})
            if response.status != 200:raise APIError(self.stage,"Apple rejected this API stage; no mutation will be replayed.",response.status)
            return response
        finally:self.timings[self.stage]=self.timings.get(self.stage,0)+round((time.monotonic()-started)*1000)

    def mutate(self,key,phase,url,fields,headers,parse,confirm=None):
        if key in self.journal:
            raise APIError(phase,"This mutation already has a recorded outcome and will not be repeated.")
        if self.guard.cancelled():raise APIError("stopped","The API beta was stopped before a checkout mutation.")
        token=self.guard.claim(key,phase)
        if not token:raise APIError(phase,"The durable mutation guard did not grant this operation.")
        started=time.monotonic()
        try:
            response=self.client.request("POST",url,headers=headers,fields=fields)
            value=parse(response)
            if confirm and not confirm(value):raise APIError(phase,"Apple did not confirm the requested checkout choice.",ambiguous=True)
            facts={"apple_response_confirmed":True}
            if not self.guard.complete(key,token,"confirmed",facts):raise APIError(phase,"The durable checkout checkpoint could not be confirmed.",ambiguous=True)
            self.journal[key]="confirmed"
            return value
        except Exception:
            self.guard.complete(key,token,"unknown",{})
            self.journal[key]="unknown"
            raise
        finally:self.timings[phase]=round((time.monotonic()-started)*1000)

    def post_action(self,key,phase,node,action,path,module,fields,*,stage=None,referer_stage=None,confirm=None):
        self.stage_start(phase,"Preparing the next verified Apple checkout step.")
        url=_action(node,action,self.origin,path,module,stage=stage)
        state=referer_stage or stage
        referer=self.origin+"/shop/checkout"+("?_s="+state if state else "")
        patch=self.mutate(key,phase,url,fields,_headers(self.model,self.origin,referer),_payload,confirm)
        self.model=_merge(self.model,patch)
        self.checkpoint(phase,checkout_host=urlsplit(self.origin).hostname,checkout_path="/shop/checkout")

    def load_profile(self):
        if self.profile_path is None:return
        path=Path(self.profile_path).resolve()
        private=(self.root/".data").resolve()
        if not private.is_relative_to(self.root) or not path.is_relative_to(private):
            raise APIError("profile","The private beta profile must remain in this project's .data directory.")
        try:
            if path.stat().st_size>32768:raise ValueError()
            self.profile=validate_profile(json.loads(path.read_text(encoding="utf-8-sig")))
        except (OSError,ValueError,TypeError):
            raise APIError("profile","The local beta profile is unavailable or invalid; no profile values were logged.") from None

    def fresh_cart(self):
        p=self.plan
        self.stage_start("session_bootstrap","Verifying a fresh direct Apple API session.")
        self.client.verify_session(WWW,client_profile="native_compatible_emulation")
        self.checkpoint("session_ready",session_ready=True)
        self.stage_start("bag_preflight","Confirming that this API session's bag is empty.")
        assert_empty_bag(parse_init_data(self.get(WWW+"/shop/bag").content))
        stock_started=time.monotonic()
        self.stage_start("stock","Rechecking the exact phone at the selected pickup store.")
        from monitor import parse_pickup
        stock=self.client.request("GET",WWW+"/shop/retail/pickup-message",headers={"Accept":"application/json","Referer":WWW+"/"},
                                  fields={"pl":"true","location":p["store_zip"],"parts.0":p["part"]})
        if stock.status != 200:raise APIError("stock","Apple did not confirm current pickup stock.",stock.status)
        row=parse_pickup(stock.json(),parts={p["part"]:{"storage":p["storage"],"color":p["color"]}},product_name=p["model"]).get((p["store_id"],p["part"]),{})
        if row.get("status")!="available" or row.get("verified") is not True:raise APIError("stock","The exact phone is not currently verified available at the selected store.")
        self.stage_start("product_quote","Verifying the exact unlocked model and current upfront price.")
        quote=product_quote(self.get(p["buy_url"]).content,p)
        if time.monotonic()-stock_started>15:raise APIError("stock","The pre-add stock observation became stale; no phone was added.")
        self.stage_start("adding","Adding one phone once, unlocked full price and no AppleCare.")
        atb_token=self.client.atb_token()
        if self.guard.cancelled():raise APIError("stopped","The API beta was stopped before adding a phone.")
        token=self.guard.claim("add_to_cart","adding")
        if not token:raise APIError("adding","The durable cart guard did not grant an add attempt.")
        self.journal["add_to_cart"]="claimed"
        add_error=None;add_response=None
        try:
            add_response=self.client.request("GET",WWW+"/shop/bag/add",headers={"Accept":"*/*","Referer":p["buy_url"]},
                fields={"product":p["part"],"atbtoken":atb_token,"purchaseOption":"fullPrice",
                        "carrierModel":"UNLOCKED/US","acpart":"none","quantity":"1"})
        except Exception as error:add_error=error
        try:
            self.stage_start("bag_reconciliation","Reconciling the bag after the single add attempt.")
            model=parse_init_data(self.get(WWW+"/shop/bag").content)
            try:empty=assert_empty_bag(model)
            except APIError:empty=None
            if empty:
                self.guard.complete("add_to_cart",token,"rejected",empty)
                self.journal["add_to_cart"]="rejected"
                raise APIError("adding","Apple did not add the phone; the API bag is confirmed empty. No add will be retried.",
                               getattr(add_error,"status",None) or (add_response.status if add_response else None))
            facts=validate_bag(model,p["part"],p["model"],p["storage"],p["color"],p["max_total_cents"])
            if facts["base_price_cents"]!=quote["base_price_cents"]:raise APIError("bag_price","The bag price changed from the verified product quote.")
            if not self.guard.complete("add_to_cart",token,"confirmed",facts):
                raise APIError("bag_reconciliation","The durable cart confirmation could not be saved.",ambiguous=True)
            self.journal["add_to_cart"]="confirmed";self.model=model
            self.checkpoint("cart_confirmed",**facts)
        except Exception:
            if self.journal["add_to_cart"]!="rejected":
                self.guard.complete("add_to_cart",token,"unknown",{})
                self.journal["add_to_cart"]="unknown"
            if isinstance(add_error,APIError):raise add_error
            raise

    def checkout(self):
        p=self.plan
        if "checkout_entry" not in self.journal:
            cart=_node(self.model,"shoppingCart");items=_node(cart,"items");key=items["c"][0]
            binding=_node(items,key,"itemQuantity","b","quantity")
            if binding.get("name")!="quantity" or not re.fullmatch(r"item-[a-f0-9-]{36}",key):
                raise APIError("checkout_entry","The current cart quantity binding is unsupported.")
            fields={"shoppingCart.recommendations.recommendedItem.part":"",
                    "shoppingCart.items."+key+".itemQuantity.quantity":"1",
                    "shoppingCart.locationConsent.locationConsent":"false","shoppingCart.summary.promoCode.promoCode":"",
                    "shoppingCart.actions.fcscounter":"","shoppingCart.actions.fcsdata":""}
            url=_action(_node(cart,"actions"),"checkout",WWW,"/shop/bagx/checkout_now","shoppingCart.actions")
            self.stage_start("checkout_entry","Opening the API checkout for the confirmed one-phone bag.")
            data,redirect=self.mutate("checkout_entry","checkout_entry",url,fields,_headers(self.model,WWW,WWW+"/shop/bag"),
                                       lambda r:_head_redirect(r,path="/shop/signIn"))
            parsed=urlsplit(redirect);self.origin=parsed.scheme+"://"+parsed.netloc
            self.checkpoint("checkout_entry",checkout_host=parsed.hostname,checkout_path="/shop/signIn")
        else:
            host=self.facts.get("checkout_host")
            self.origin=safe_url("https://"+str(host))
            redirect=self.origin+"/shop/signIn"
        if "guest_login" not in self.journal:
            self.stage_start("checkout_verification","Renewing verification on the current Apple checkout host.")
            self.client.verify_session(self.origin,client_profile="native_compatible_emulation")
            self.stage_start("guest_model","Loading fresh guest checkout actions after verification.")
            self.model=parse_init_data(self.get(redirect,referer=WWW+"/shop/bag").content)
            signin=_node(self.model,"signIn")
            if _node(signin,"d").get("guestCheckoutEnabled") is not True:raise APIError("guest_login","Apple did not enable guest checkout in this session.")
            guest=_node(signin,"guestLogin")
            url=_action(guest,"guestLogin",self.origin,"/shop/signInx","signIn.guestLogin")
            self.stage_start("guest_login","Continuing as guest without an Apple ID login.")
            fields={"signIn.guestLogin.deviceID":'{"op":"DEVICEID"}'}
            continuation,next_url=self.mutate("guest_login","guest_login",url,fields,_headers(self.model,self.origin,redirect),
                lambda r:_head_redirect(r,path="/shop/checkout/start",origin=self.origin,continuation=True))
            self.checkpoint("guest_login",checkout_host=urlsplit(self.origin).hostname,checkout_path="/shop/checkout/start")
            self.stage_start("checkout_start","Loading the current Apple fulfillment model.")
            response=self.mutate("checkout_start","checkout_start",next_url,continuation["args"],
                                {"Accept":"text/html","Content-Type":"application/x-www-form-urlencoded","Origin":self.origin,"Referer":redirect},
                                lambda r:parse_init_data(r.content) if r.status==200 else (_ for _ in ()).throw(APIError("checkout_start","Apple did not open fulfillment.",r.status,ambiguous=True)))
            self.model=response
            self.checkpoint("checkout_start",checkout_host=urlsplit(self.origin).hostname,checkout_path="/shop/checkout")
        else:
            self.stage_start("checkout_reconciliation","Reading the existing checkout without replaying its prior actions.")
            self.model=parse_init_data(self.get(self.origin+"/shop/checkout").content)
            if "checkout_start" not in self.journal:
                raise APIError("checkout_reconciliation","The guest continuation needs manual reconciliation; it will not be resubmitted.")

    def fulfillment(self):
        p=self.plan
        if "select_pickup" not in self.journal:
            node=_node(self.model,"checkout","fulfillment","fulfillmentOptions")
            self.post_action("select_pickup","pickup_mode",node,"selectFulfillmentLocationAction","/shop/checkoutx/fulfillment","checkout.fulfillment.fulfillmentOptions",
                {"checkout.fulfillment.fulfillmentOptions.selectFulfillmentLocation":"RETAIL","checkout.locationConsent.locationConsent":"false"},
                confirm=lambda patch:isinstance(patch.get("checkout",{}).get("fulfillment",{}).get("pickupTab",{}).get("pickup",{}).get("storeLocator"),dict))
        if "select_store" not in self.journal:
            node=_node(self.model,"checkout","fulfillment","pickupTab","pickup","storeLocator")
            prefix="checkout.fulfillment.pickupTab.pickup.storeLocator."
            self.post_action("select_store","store_selection",node,"select","/shop/checkoutx/fulfillment","checkout.fulfillment.pickupTab.pickup.storeLocator",
                {prefix+"selectStore":p["store_id"],prefix+"searchInput":p["store_zip"],prefix+"showAllStores":"false"},
                confirm=lambda patch:patch.get("checkout",{}).get("fulfillment",{}).get("pickupTab",{}).get("pickup",{}).get("storeLocator",{}).get("d",{}).get("selectStore")==p["store_id"])
            self.checkpoint("store_selected",store_id=p["store_id"],store_confirmed=True)
        if "select_slot" not in self.journal:
            slot=choose_earliest_slot(self.model,p["store_id"])
            fields=dict(slot.form_fields());prefix="checkout.fulfillment.pickupTab.pickup.storeLocator."
            fields.update({"checkout.fulfillment.fulfillmentOptions.selectFulfillmentLocation":"RETAIL",prefix+"selectStore":p["store_id"],
                           prefix+"searchInput":p["store_zip"],prefix+"showAllStores":"false","checkout.locationConsent.locationConsent":"false"})
            node=_node(self.model,"checkout","fulfillment")
            self.post_action("select_slot","pickup_slot",node,"continueFromFulfillmentToPickupContact","/shop/checkoutx/fulfillment","checkout.fulfillment",fields,
                             confirm=lambda patch:isinstance(patch.get("checkout",{}).get("pickupContact"),dict))
            self.checkpoint("slot_selected",store_id=p["store_id"],slot_confirmed=True,pickup_confirmed=True)
            self.slot_facts=slot.safe_facts()

    def billing(self):
        if self.profile is None:raise APIError("pickup_contact","Pickup is prepared. Configure the local beta contact/billing profile to continue.")
        if "pickup_contact" not in self.journal:
            node=_node(self.model,"checkout","pickupContact")
            option=_node(node,"pickupContactOptions","d").get("selectedPickupOption")
            if not isinstance(option,str) or option.upper()!="SELF":raise APIError("pickup_contact","Only self pickup is supported by this beta.")
            prefix="checkout.pickupContact.selfPickupContact.selfContact.address."
            fields={"checkout.pickupContact.pickupContactOptions.selectedPickupOption":option,
                    prefix+"firstName":self.profile.contact["first_name"],prefix+"lastName":self.profile.contact["last_name"],
                    prefix+"emailAddress":self.profile.contact["email"],prefix+"fullDaytimePhone":self.profile.contact["phone"],
                    "checkout.locationConsent.locationConsent":"false"}
            self.post_action("pickup_contact","pickup_contact",node,"continueFromPickupContactToBilling","/shop/checkoutx","checkout.pickupContact",fields,stage="PickupContact-init",
                             confirm=lambda patch:isinstance(patch.get("checkout",{}).get("billing"),dict))
        if "select_credit" not in self.journal:
            node=_node(self.model,"checkout","billing","billingOptions")
            self.post_action("select_credit","credit_option",node,"selectBillingOptionAction","/shop/checkoutx/billing","checkout.billing.billingOptions",
                {"checkout.billing.billingOptions.selectBillingOption":"CREDIT"},stage="Billing-init",
                confirm=lambda patch:isinstance(patch.get("checkout",{}).get("billing",{}).get("billingOptions",{}).get("selectedBillingOptions",{}).get("creditCard"),dict))
        if "zip_lookup" not in self.journal:
            node=_node(self.model,"checkout","billing","billingOptions","selectedBillingOptions","creditCard","billingAddress","address","zipLookup")
            prefix="checkout.billing.billingOptions.selectedBillingOptions.creditCard.billingAddress.address.zipLookup."
            self.post_action("zip_lookup","billing_zip",node,"zipLookup","/shop/checkoutx/billing","checkout.billing.billingOptions.selectedBillingOptions.creditCard.billingAddress.address.zipLookup",
                             {prefix+"postalCode":self.profile.billing["postal_code"],prefix+"countryCode":"US"},referer_stage="Billing-init")
        if "payment_validation" in self.journal:
            if not isinstance(self.model.get("checkout",{}).get("review"),dict):raise APIError("billing_validation","Billing was already attempted; a fresh review is required before continuing.")
            return
        self.stage_start("billing_validation","Encrypting the configured beta card with the current Apple public key, then validating billing once.")
        card=_node(self.model,"checkout","billing","billingOptions","selectedBillingOptions","creditCard","cardInputs","cardInput-0","d")
        pan=_node(card,"encryption","config");cvv=_node(card,"encryption-securityCode","config")
        if pan.get("publicKey")!=cvv.get("publicKey") or pan.get("publicKeyHash")!=cvv.get("publicKeyHash"):
            raise APIError("billing_key","Current per-field Apple encryption metadata needs separate validation.")
        encrypted=encrypt_card(self.profile,pan.get("publicKey"),pan.get("publicKeyHash"))
        fields=build_us_billing_payload(self.profile,encrypted,self.model)
        node=_node(self.model,"checkout","billing")
        url=_action(node,"continueFromBillingToReview",self.origin,"/shop/checkoutx/billing","checkout.billing",stage="Billing-init")
        token=self.guard.claim("payment_validation","billing_validation")
        if not token:raise APIError("billing_validation","Billing validation was not granted by the durable guard.")
        self.journal["payment_validation"]="claimed"
        try:
            response=self.client.request("POST",url,headers=_headers(self.model,self.origin,self.origin+"/shop/checkout?_s=Billing-init"),fields=fields)
            result=classify_apple_payment_response(response.status,response.content)
            if result.kind!="review_ready":
                self.guard.complete("payment_validation",token,"rejected" if result.kind in {"payment_declined","field_validation","http_error","checkout_error"} else "unknown",{})
                self.payment_outcome=result
                raise APIError("billing_validation",result.message,response.status,ambiguous=result.kind in {"network_error","unconfirmed"})
            patch=_payload(response)
            self.model=_merge(self.model,patch)
            self.guard.complete("payment_validation",token,"confirmed",{"apple_response_confirmed":True})
            self.journal["payment_validation"]="confirmed"
            self.checkpoint("billing_validated")
        except Exception:
            if self.journal["payment_validation"]=="claimed":self.guard.complete("payment_validation",token,"unknown",{})
            raise

    def run(self):
        if type(self.allow_submit_once) is not bool:raise APIError("plan","The final submission flag must be explicit.")
        self.plan=validate_plan(self.root,self.plan)
        if getattr(self.guard,"run_id",self.plan["run_id"])!=self.plan["run_id"]:raise APIError("plan","The API plan and mutation journal belong to different runs.")
        self.load_profile()
        info=self.guard.resume_info()
        if info.get("requires_reconciliation"):raise APIError("reconciliation","An earlier API request has an unknown outcome. Reconcile it before resuming.")
        self.facts=dict(info.get("facts") or {})
        self.journal={v["mutation_key"]:v["status"] for v in info.get("mutations",[]) if isinstance(v,dict)}
        if any(s!="confirmed" for s in self.journal.values()):raise APIError("reconciliation","This run contains a rejected or incomplete mutation and cannot automatically repeat it.")
        self.client=AppleClient(self.root,self.plan["run_id"])
        if "add_to_cart" not in self.journal:self.fresh_cart()
        else:
            self.stage_start("cart_reconciliation","Reading the existing API bag without repeating Add to Bag.")
            self.model=parse_init_data(self.get(WWW+"/shop/bag").content)
            facts=validate_bag(self.model,self.plan["part"],self.plan["model"],self.plan["storage"],self.plan["color"],self.plan["max_total_cents"])
            self.checkpoint("cart_confirmed",**facts)
        self.checkout();self.fulfillment();self.billing()
        self.stage_start("review","Checking the exact phone, pickup store, quantity, no AppleCare and tax-inclusive total.")
        review=validate_review(self.model,self.plan,self.facts)
        self.checkpoint("review_ready",**review)
        if self.allow_submit_once:return final_order_hook(self.client,self.model,self.plan,self.guard,review)
        return {"status":"review_ready","stage":"review_ready","review":review,"order_placed":False,
                "message":"One phone is prepared at the selected store with the earliest valid pickup window. The verified review is within $2,300; no order was submitted."}


def run_flow(root,plan,guard,*,profile_path=None,allow_submit_once=False):
    flow=_Flow(root,plan,guard,profile_path,allow_submit_once)
    result=None
    try:
        result=flow.run()
    except (APIError,PaymentError,SlotError) as exc:
        stage=getattr(exc,"stage",flow.stage)
        if not isinstance(stage,str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}",stage):stage=flow.stage
        result={"status":"needs_attention","stage":stage,"message":str(exc),"order_placed":False}
        outcome=getattr(flow,"payment_outcome",None)
        classification=outcome.kind if outcome else "session_error" if getattr(exc,"status",None) else "unknown_error"
        result["failure"]={"classification":classification,"source":"apple" if outcome or getattr(exc,"status",None) else "local",
                           "stage":stage,"reason":outcome.message if outcome else "Apple checkout session failed"}
        if outcome and outcome.kind=="payment_declined":result["failure"]["apple_decline"]=True
        if type(getattr(exc,"status",None)) is int:result["failure"]["http_status"]=exc.status
    except Exception:
        result={"status":"needs_attention","stage":flow.stage,"order_placed":False,
                "message":"The API beta paused for inspection. Its cart and checkout mutations will not be replayed.",
                "failure":{"classification":"unknown_error","source":"local","stage":flow.stage}}
    finally:
        flow.timings[flow.stage]=round((time.monotonic()-flow.phase_started)*1000)
        if flow.client is not None:
            try:flow.client.close()
            except Exception:
                result={"status":"needs_attention","stage":"session_persistence","order_placed":False,
                        "message":"The private API session could not be saved. Inspect this run before continuing."}
    result.update(connection="direct_no_proxy",browser_required=False,
                  client_profile="native_compatible_emulation",
                  timings_ms={**flow.timings,"total":round((time.monotonic()-flow.started)*1000)})
    if hasattr(flow,"slot_facts"):result["pickup_slot"]=flow.slot_facts
    return result
