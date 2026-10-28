# Checkout beta: live evidence and current limits

Examined **27 October 2026** using the installed REA skill/runtime workflow, connected REA browser tools, and a separate diagnostic Chrome profile on port 9225. The user authorized one ordinary in-stock iPhone as a cart/pickup diagnostic. No login, invented contact details, card entry, payment authorization, or order submission occurred. The corrected production limit is **$2,300 including tax**, quantity one, no AppleCare; current monitoring covers 19 NJ/Manhattan stores.

## Observed product controls

Both exact Duo pages loaded successfully and showed **$1,999.00** before tax, with the correct checked color/storage and hidden part:

- Night Sky: `MK1F4LL/A`, `/shop/buy-iphone/iphone-duo/7.6-inch-display-256gb-night-sky-unlocked`.
- Star White: `MK1E4LL/A`, corresponding `256gb-star-white-unlocked` path.

| Fact/control | Observed DOM anchor |
| --- | --- |
| Exact part | Nonempty values of `input[type="hidden"][name="product"]`; ignore the empty `#productPartNumber` field. |
| 256GB | `input[data-autom="dimensionCapacity256gb"]`, verify `checked`. |
| Color | `input[data-autom="dimensionColornightsky"]` / `dimensionColorstarwhite`. |
| No trade-in | `input[data-autom="choose-noTradeIn"]`. |
| Upfront purchase | `input[data-autom="purchaseGroupOptionfullprice"]`. |
| Authoritative selected upfront price | `[data-autom="purchaseGroupOptionfullprice_price"]`; separate from monthly financing text. |
| Unlocked | `input[data-autom="carrierModelUNLOCKED/US"]`. |
| No AppleCare | `input[data-autom="noapplecare"]`; this was initially **unchecked**, leaving Add to Bag disabled. |
| Add to Bag | `button[data-autom="add-to-cart"]`, verify enabled. |

React-generated IDs vary. A hidden color radio resisted pointer clicks; its associated label worked. Declared keyboard Space actions on the six product radios successfully configured the generic 17e page, verified all selections, and enabled Add to Bag. Do not hardcode generated IDs or select the first dollar amount in the page.

## Actual ordinary-phone test

Apple's live pickup response confirmed **iPhone 17e 256GB Black, `MHRN4LL/A`**, available at **World Trade Center `R654`**, SoHo, Grand Central, and other stores. The exact configured page loaded at:

`https://www.apple.com/shop/buy-iphone/iphone-17e/6.1-inch-display-256gb-black-unlocked`

Its selected upfront price was **$699.00**, corroborated by product JSON `partNumber` and `price.fullPrice`. This diagnostic part is separate from the monitor's Duo catalog/alerts.

1. A real native Add to Bag click submitted to the variant path and reached Apple's **page-not-found** screen. The bag was then inspected in the same isolated session: **“Your bag is empty.”**
2. After that reconciliation, a single same-origin request using the reference helper's **base product path** and the observed page token returned **HTTP 541**, without a confirmed `step=attach` destination. This action used the diagnostic native-CDP script; it was not a REA-declared action. A subsequent REA bag capture again confirmed an empty bag. There was no blind retry.

Follow-up: the user supplied a screenshot from their regular browser showing **iPhone 17e 256GB Black** at Apple's secure checkout fulfillment page, with pickup options and a displayed order summary of **$761.04**. This is user-supplied evidence of that browser reaching pickup, not a completed order or independently verified final charge.

A new direct-connection test used the generic product page, selected all six native controls, verified the exact part and $699 price, and dispatched a real pointer click on Apple's enabled Add to Bag button. The document request returned **HTTP 541**, displayed Page Not Found, and the same browser's bag was subsequently confirmed empty. That establishes that the isolated session also fails with native form submission; it does not establish why Apple rejects it. No new proxies were used. Raw sanitized test results stay local in `.runtime/native-local-result.json`.

The browser adapter now uses the native button rather than reconstructing the helper's request or reading Apple's session token. A missing/covered/ambiguous button cannot dispatch input, and an uncertain click is never retried. Checkout remains fixed to the direct connection.

## What is genuinely unknown

No populated-bag product/quantity controls, pickup-store chooser, contact/authentication step, final tax-inclusive total, or confirmation/order controls were reached. No selectors for those steps are claimed. The observed empty-bag anchor is `h1` with exact text **“Your bag is empty.”** Product price alone cannot establish the final checkout total.

The review-only companion in `beta/browser.mjs` / `beta/cdp.mjs` uses Node built-ins, a dedicated visible profile on port 9226, and a fixed direct connection (`--no-proxy-server`). It verifies observed product fields, checks the base-price cap, attempts one bag addition, and reconciles the bag. It returns **`needs_attention`** when pickup/quantity/tax review cannot be verified; it does not fabricate `review_ready` facts or contain a final payment/order action. Four Node boundary tests pass.

The integrated `/api/beta/test` run confirmed live stock at World Trade Center, selected the correct diagnostic part/store, opened the dedicated profile, verified the empty bag and product configuration, attempted one addition, received HTTP 541, and reconciled the bag as empty. It persisted `needs_attention` and released the worker while keeping the run locked against rearming. Monitoring stayed at 19/19 stores. A separate isolated test using one supplied authenticated proxy also returned HTTP 541 and an empty bag; its credentials stayed in local configuration and memory. No automatic route switching or repeated addition occurred inside either run.

## Retained sources

Full evidence is local and ignored: `beta-checkout-investigation-evidence.json` (10 REA records), `beta-apple-scenario-evidence.json`, `beta-apple-flow-evidence.json`, selected HTML/accessibility captures, `beta-test-17e-stock.json`, and the native-CDP diagnostic result/script. Raw captures can contain anonymous session values and should remain uncommitted.

- Public pages and original empty bag: REA `ev_e8823cab37beae3fb725e5aaf576f44f54d8b5d1e7b45bd13216099d77cdb9e3`.
- Exact configured 17e: `ev_af75e593091ed7bc32c4027bdb5abbf12763a23cecbed6a8f94c4fe1c8d65069`.
- Native Add to Bag/page-not-found: `ev_7139e2a3909a2074e48c18d5a9f22348fc4936507c9c163892c6364bfbd981fa`; empty-bag reconciliation `ev_3e9f6bccdf32582c5694160f1c087525da72706186b5972c279a8eda9f295afd`.
- Successful keyboard configuration: `ev_9bf56a82d3f11f7e87d20e0386adf3135ff48f1dd498f85867175937d4cdfd7a`.
- Bag reconciliation after HTTP 541: `ev_bb387cdaadd02babd3029ffe4f54555643cd0ebcf24ce888c3e9c2332ce5419b`.
