# Automatic checkout: bounded exploration

**Status: live automatic ordering is not implemented or enabled.** The review-only preparation beta is implemented; the [follow-up live test](BETA-CHECKOUT-EVIDENCE.md) attempted a diagnostic phone addition and stopped after Apple rejected it. No payment or order was submitted.

**Requested limits:** one 256GB iPhone Duo at a time; either Night Sky `MK1F4LL/A` or Star White `MK1E4LL/A`; unlocked, paid upfront, no AppleCare; pickup at one of the 19 monitored NJ/Manhattan stores; **maximum final order total $2,300 USD including tax and mandatory fees** (corrected by the user). The implementation allows one preparation run at a time and requires an empty-bag check and explicit reset before another.

## What the current implementation establishes

The user-run [bookmarklet](../web/bookmarklet.js) checks the Apple hostname/product page, accepts only the two monitored parts, reads the existing page token, and requests `purchaseOption=fullPrice`, `cppart=UNLOCKED/US`, and `acpart=none`. It opens the bag after HTTP success and a `step=attach` response URL. These confirmed code paths express the requested configuration; live bag contents and purchase success remain unverified.

The helper does not choose a pickup store, verify a final price, enforce quantity one, enter checkout, authorize payment, or confirm an order. Alerts identify the store and exact model; users still select pickup and complete Apple's checkout.

## Evidence still needed

| Question | Current evidence and next observation |
| --- | --- |
| Pickup selection | Public availability exposes store IDs; the helper has no store-selection request. Observe the user's Apple bag/checkout UI, identify its actual pickup controls, then verify the selected store's name/address against the catalog. |
| Product and price | Part mapping is confirmed. Live bag contents, quantity, upsells, pickup fulfillment, and tax-inclusive final review have not been inspected. Reject unknown totals, other bag items, financing, AppleCare, or a total over 130,000 cents. |
| Authentication | Login, Apple Pay/user-presence prompts, issuer 3-D Secure, and other challenges were not observed. Required interactive steps must pause for the user. |
| Order completion | Submission, confirmation, order IDs, and order-history recovery remain unobserved. A redirect or cleared bag alone cannot establish success. |

## Proposed implementation strategy

1. **Inspect before enabling purchase.** Use the user's signed-in Apple browser and saved Apple payment method. Record the flow through final review without submitting. Keep passwords, payment credentials, cookies, and tokens in Apple's browser session; no card data in chat or this repository. Establish whether authentication requires user participation.
2. **Validate authoritative review.** On fresh stock, choose an allowlisted store and eligible part. Require exactly one matching phone and no other bag items. Immediately before submission verify 256GB, approved color/part, unlocked, full upfront payment, no AppleCare, quantity one, correct pickup store, USD, and final tax/fee-inclusive total ≤$2,300. Missing or changed fields stop the run.
3. **Lock one armed run durably.** Acquire a purchase lock and persist its unique run ID before cart mutation: `DISARMED → ARMED → PREPARING → REVIEW_VERIFIED → SUBMITTING → CONFIRMED → DISARMED`. Duplicate stock events and process restarts cannot start a second purchase.
4. **Submit once; reconcile ambiguity.** Persist `SUBMITTING` before the final action. Never blindly retry an ambiguous bag addition or submission. Inspect the bag or Apple's confirmation/order history first; unresolved outcomes block further attempts, including after restart. Require a stable order ID matching item, quantity, store, and total; otherwise stop for manual review.
5. **Disarm on confirmed success.** Record the order reference and require manual rearming. Later restocks only alert. Test price changes, quantity drift, lost stock, authentication pauses, duplicate events, interrupted submission, and restart recovery with fixtures before live submission is enabled.

**Next step/blocker:** obtain the user-owned browser checkout trace for pickup selection, authoritative totals, authentication, and confirmation. Then implement preparation/review with those observed controls. Full unattended completion remains unverified.

## Sources

- [Local helper source](../web/bookmarklet.js), especially the part allowlist, add-to-bag parameters, response check, and ambiguous-request message.
- [REA findings](REVERSE-ENGINEERING.md), sections 1, 3, and 7; public frontend source `1dibk0bicgbb7.js` contains the original user-triggered bookmarklet. Parent REA Evidence: `ev_ec29fb57d1943e71c945c70461abc1a11d1fe0420496706a6601a6b2f55a7823`. Static syntax analysis has partial graph coverage and does not establish checkout execution.
- [Current catalog](../catalog.json) and [browser QA](browser-qa.json): 19 monitored stores, 38 store/part variants, correct encoded helper-copy flow. Clipboard/UI fixtures do not establish a live purchase.
