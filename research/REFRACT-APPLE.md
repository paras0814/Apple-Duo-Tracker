# Refract Apple workflow investigation

27 October 2026. The user selected their installed Refract application for REA analysis to investigate an HTTP-driven Apple pickup beta. This report describes static findings, not a successful automated checkout.

## Targets and method

Refract's installed `resources/app.asar` identifies version **1.55.0**. SHA-256: `2438eff8129543599104c4c8c3a1e2fdbe0cc74c643a2560230cd4d317a15718`.

Its Electron backend is obfuscated Bytenode `build/main/main.jsc`; the renderer source map exposes IPC wrappers, not the Apple request implementation. Task execution and opening a task browser are separate commands. Inferring a pure HTTP workflow from an invisible browser would be insufficient.

An existing child process of Refract identified the separate native engine. Its on-disk PE was read and copied for offline analysis; Refract was not restarted, instrumented, or given new tasks. Engine SHA-256: `9b6a145ca58ad64f8cc26b5802f42eb26eb97a9ef24826bb7fbe7bf33e45da3a` (67,338,752 bytes).

REA reports Go **1.26.4**, module `scripts/cmd/gui`, main-module version `v0.15.88-0.20261027140712-7be9c4876d20`. Its dependencies include custom PrismAIO HTTP, requests, and TLS packages, as well as browser tooling and Rod. A validated PE/Go metadata parser recovered 60,807 function entries. Selected x86-64 functions were decoded offline with Capstone and bound to that exact image; these are ordinary static-tool observations, distinct from REA Evidence records.

The copied engine was also submitted to REA/Ghidra using checksum-verified portable Ghidra 12.1.4 and JDK 21 under ignored `.runtime/`. Whole-program analysis did not complete within its 20-minute client deadline and its owned process was cleaned up. Focused native instruction captures were subsequently obtained through REA/Rizin without executing Refract. No global Java, PATH, Codex registration, or Refract installation changes were needed.

## Established HTTP architecture

Decoded direct calls establish this path:

```text
Apple getProductPage
  -> Apple requestWrapper.Get
  -> requests.BaseWrapper.Request
  -> go-http.Client.do
```

Concrete call instructions are at preferred VAs `0x141549e44`, `0x14154acae`, and `0x1409dda4a`. This establishes an Apple HTTP implementation in the shipped engine. It does not establish that every mode avoids browsers: explicit browser handoffs exist for manual use and 3-D Secure, and indirect calls remain outside this bounded trace.

## Workflow recovered so far

| Stage | Static evidence | Implication for this beta |
| --- | --- | --- |
| Session bootstrap | `bootstrapSession` calls `getSession`, verification, cart acquisition, document-cookie application, and AOS-session extraction. The session wrapper references `/shop/beacon/atb`. | Establish a reusable Apple session before attempting cart mutations. A bare add request is incomplete. |
| Session verification | `verifyShld` calls challenge acquisition, a solver, submission, and a cookie check. An earlier verification call depends on `preSolveShld`. | Handle the actual session/challenge response. Rotating proxies or changing delays does not substitute for this step. No challenge solution or service credential was copied. |
| Product resolution | Product JSON-LD, metrics/product-selection data, and configured product hardcodes are separate resolution paths. | Verify the exact SKU, 256GB storage, color, and price from the selected product. Do not assume one URL template selects every configuration. |
| Cart request | `Task.addToCart` calls the token/query builders, selects a path, then its request wrapper. The wrapper concatenates path, `?`, and query before calling `Get`. | The observed request is GET; the selected route depends on product metadata. |
| Checkout entry | `initCheckout` references `/shop/bagx/checkout_now?_a=checkout&_m=shoppingCart.actions` and Apple model/session header names. | Follow checkout state and returned host/actions rather than hardcoding the screenshot's `secure7` host. |
| Guest checkout | The guest-login wrapper constructs `/shop/signInx?ssi=%s&up=true&_a=guestLogin&_m=signIn.guestLogin`. | The sign-in/session identifier comes from the current flow. Do not invent it or reuse a captured personal identifier. |
| Pickup mode | Fulfillment selection uses `RETAIL` and the fulfillment-options action. | Select pickup explicitly and read the resulting checkout model. |
| Store selection | `selectStore` builds the store payload and invokes the store-locator action. | Select the monitored store ID within the current checkout session. |
| Pickup date/contact | Payload builders include current date/time-slot identifiers and signature fields. | Read the current options and signatures; a fixed replay payload is insufficient. |
| Review/order | Separate payment, review, order-submission, and status routines exist. | The present beta continues to stop for review. Their presence is not permission to submit an order or evidence of a verified final total. |

### Cart route and query branches

`Task.addToCart` initially builds the storefront prefix plus `/shop/bag/add`. A nonempty product extra-fields slice switches the path to the product link before its `?`. The corresponding query-builder branch appends `add-to-cart=add-to-cart`, `step=select`, and the product's extra fields. The generic branch includes an API-key parameter and merges configured extra fields. The embedded key value is excluded from this report and is not copied into our monitor.

The builder references `product`, `atbtoken`, `purchaseOption`, and `carrierModel`. Decoded assignments establish `purchaseOption=fullPrice` and, in the US branch, `carrierModel=UNLOCKED/US`. This does **not** establish that Apple's `cppart` parameter is universally interchangeable or that every product takes the generic bag-add path.

The bounded ATC trace does not establish an explicit no-AppleCare or quantity-one assignment for every branch. Our **one phone / no AppleCare / $2,300 including tax** requirements must therefore remain independent checks, including populated-bag and final-review verification.

### Store and fulfillment actions

The store request uses:

```text
_a=select&_m=checkout.fulfillment.pickupTab.pickup.storeLocator
```

Its payload builder references:

```text
checkout.fulfillment.pickupTab.pickup.storeLocator.selectStore
checkout.fulfillment.pickupTab.pickup.storeLocator.searchInput
checkout.fulfillment.pickupTab.pickup.storeLocator.showAllStores
```

The recovered constant for `showAllStores` is `false`. Pickup mode references `selectFulfillmentLocation=RETAIL`. A separate action is:

```text
_a=selectPickupMethod&_m=checkout.fulfillment.pickupTab.pickup.pickupMethod
```

These operate against current checkout state (`Fulfillment-init`). Header-name references include `x-aos-stk`, `x-aos-model-page`, `modelversion`, `syntax`, and `x-requested-with`; values, origins, and session binding still require current-response verification before implementation.

## Useful native locations

All addresses are static preferred-image addresses, not observed runtime-ASLR addresses.

| Function | VA |
| --- | --- |
| Session bootstrap | `0x141536580` |
| Task add-to-cart | `0x14150d1a0` |
| Cart query builder | `0x14150ea40` |
| Cart request wrapper | `0x14150f9a0` |
| Enter checkout | `0x14151de20` |
| Initialize checkout request | `0x141520720` |
| Select store | `0x14153ad00` |
| Store payload builder | `0x14153be20` |
| Select pickup method | `0x14153b0a0` |
| Checkout flow | `0x14155bd00` |

## Relationship to our failures

The user's regular-browser screenshot reached pickup selection for the **iPhone 17e 256GB Black**. Our separate direct browser received HTTP 541 even through a native Add to Bag click and subsequently had an empty bag. These observations establish different outcomes, not the exact rejection cause.

Refract's session/bootstrap and verification code gives a concrete next investigation beyond proxy rotation. A direct API beta should implement observed session transitions, reconcile one cart mutation, and then validate current store/slot/price state. It must not be marked operational until an actual authorized test reaches those stages.

### Live read-only bootstrap probes

REA browser scenarios connected to our dedicated direct diagnostic session performed declared GET requests, without a cart mutation:

- `/shop/beacon/atb` returned **200**.
- `/shop/shld/work/s3/q` without a query returned **404**.
- `/shop/shld/work/s3/q?jr=30` returned **200**, JSON with version **3**, algorithm **SHA-384**, and fields `challenge_version`, `timeout`, `salt`, `t`, `algorithm`, `challenge`, `signature`, `X`.
- Apple's current `/shop/shld/s3/verify.js` returned **200** and references the same verification path. Captured response-body SHA-256: `e6161985fc319c5f4799f8ed21ebc86f90e5e204d05e06f87eb59ef2238e606c`.

The `jr=30` default is independently supported by the native normalization function's query check and integer formatting. The successful query validates endpoint acquisition, **not** a completed verification handshake or authorized cart session. Challenge values stay local; no solutions or authentication data appear in this report. These browser-context probes are not yet a standalone HTTP client test.

## Evidence and limits

The separately authorized direct-API iPhone 17e test subsequently reached World Trade Center pickup, selected the earliest available slot, submitted contact/billing data with current-key encrypted card fields, verified a **$761.04** tax-inclusive review, and sent **one** Place Order request. Apple returned processing, then its advertised status poll returned a confirmed payment decline. A decline webhook was delivered to the configured private channel. Personal data, card fields and raw responses remain local and are excluded from this report.

This manually developed flow is not a completed fresh timed test of the later iPhone 17 White URL. The user paused beta work to prioritize stock-monitor reliability. The main monitor's direct verified session was independently confirmed against four ZIPs at **19 stores / 38 products**, and its proxy-cooldown fix was activated by restarting the local service.

- REA ASAR parent: `ev_2967e3c9e642015988798d50a38923abe7cee2ce64e2966a857ed67992774647` (inventory plus selected manifest analysis; unselected sources remain unknown).
- REA native Go metadata: `ev_5f949a0baa90438504e80daf48175c585a545d914c895fd5ebc1dd7a2affacb8`.
- REA dependency view: `ev_45fc95d68032cd85e36addb906eb7e0963f78a014e59b9e7e483a677fdd97c2a`.
- REA Electron fuse inspection: `ev_cd0cf2a81d46a9f5cd1063069cf6cbd12719ac6ceeaf5b72aee4e35b878b3285`.
- Live beacon/default-route probe: `ev_400db53fd487a8be61b23a085330b1922007fcfffa1af7875ec4213bada071fc`.
- Current public verification script: `ev_5ecbd092637eaa9601bb6c0d5d6091f6c58e784e83843e4613f525ea7958ffa8`.
- Verification query with `jr=30`: `ev_613a0569852ce49d9a3f576be0a65880c0ee7205ca9ff734bc708b30c95f6102`.
- Focused REA/Rizin ATC-token instructions: `ev_c5f5e5ecfca7913f51b88b8e0c7617b2d234434dc9e56f70d2a3bb81cfd4a020`.
- Focused REA/Rizin guest-login instructions: `ev_194351612fa71de12c86abafb5fb0f3e08d1aefc660bb27fc20982382de1fb77`.
- Four unpacked notifier-executable integrity mismatches were retained as observed-untrusted. They were not executed or silently accepted as matching the ASAR declarations.

Full inventories, native bytes, disassembly, source-map contents, local logs and tool binaries remain ignored. Only this finding summary is intended for GitHub. No user credentials, card details, cookies, personal checkout payloads, or Refract proprietary implementation are included.
