# Duo Tracker investigation

Target: <https://duo-tracker-six.vercel.app/>. Examined 27 October 2026. Public shipped assets and unauthenticated GET responses only. No push registrations, purchase actions, or private server resources were invoked.

## REA execution and retained evidence

REA **6.8.0** was cloned from `morluto/rea`, installed using locked npm dependencies, and built successfully. Its `reverse-engineer-anything` skill and JavaScript application guide were followed. Static analysis used the built REA CLI; this does not establish an active Codex MCP connection.

Command, from the workspace, with the portable Node directory on PATH:

```powershell
node rea/scripts/rea.mjs analyze-javascript-application "$PWD/research" `
  --source-path 0xuypb9i7gi-8.js --source-path 1dibk0bicgbb7.js --source-path sw.js `
  --detail summary --evidence_output "$PWD/research/rea-reference-evidence.json" --format json
```

The complete parent Evidence ID is:

`ev_ec29fb57d1943e71c945c70461abc1a11d1fe0420496706a6601a6b2f55a7823`

The summary Evidence ID is:

`ev_1049c6736f50845d0a6e33894ed9ed08e8abfc281bde42f630206ce46060ccf1`

Files:

- `rea-reference-evidence.json`: complete canonical REA evidence bundle, about 12.7 MB.
- `rea-reference-summary.json`: REA summary with scope and limitations.
- `rea-findings.json`: compact endpoint/timer observations, source hashes, locations, and scope derivation.
- `rea-state-trace.json`: an unsuccessful optional feature-trace diagnostic. The exported-record trace was rejected with `evidence_integrity_mismatch`; no successful feature trace is claimed.

REA parsed all **3 selected JavaScript files**, reading **74,944 bytes**, visiting **9,696 AST nodes**, with **0 parse failures**. The source selection intentionally excluded framework/runtime bundles and other inventoried files. Application and semantic graph coverage are partial: unresolved dynamic expressions and unselected-source relationships remain unknown. The analysis treated the scripts as inert text and did not execute their bootstrap code.

## Findings

### 1. Storage and part mapping — answered

The shipped product catalog in `0xuypb9i7gi-8.js` explicitly declares:

| Part | Storage | Color |
| --- | --- | --- |
| `MK1F4LL/A` | **256GB** | Night Sky |
| `MK1E4LL/A` | **256GB** | Star White |

It also declares 512GB, 1TB, and 2TB variants. The requested monitor should select only the two rows above. `25GB` is a typo, not a declared storage option.

Independent live Apple GET snapshots in `apple-response.json` and `apple-*.json` contain `messageTypes.regular.storePickupProductTitle` values `iPhone Duo 256GB Night Sky` and `iPhone Duo 256GB Star White`. Those are external Apple observations, separate from REA's static catalog evidence.

### 2. Frontend and backend polling — answered with a server-side limit

The page advertises five-second availability checking. Its **browser actually polls `/api/state` every 3,000 ms**, while visible, and immediately rechecks when visibility returns. REA's timer observation is in `1dibk0bicgbb7.js`, line 17, columns 14741–14803, with `delay_milliseconds: 3000`. The UI's clock updates separately every second.

History refreshes every **30,000 ms** while visible. REA timer location: `1dibk0bicgbb7.js`, line 1, columns 11104–11166.

The private backend's scheduler and Apple request implementation were not shipped in these assets. Five-second backend checking is an advertised behavior, not established by frontend source alone. The replacement monitor implements its own actual five-second Apple polling cadence.

### 3. Public API contract — answered for visible consumers

REA discovered these frontend fetch targets in its application graph:

| Endpoint | Visible use | Evidence location in `1dibk0bicgbb7.js` |
| --- | --- | --- |
| `GET /api/state` | Load stock, recent events, backend sweep status, with `cache: "no-store"` | line 17, columns 13507–13545 |
| `GET /api/history` | Load event history and statistics | line 1, columns 10042–10082 |
| `GET /api/history?before=...` | Page older events using the last event's `started_at` | line 1, columns 10888–10952 |
| `POST /api/push` | Save browser subscription and model/store preferences | line 1, columns 3054–3221 |
| `DELETE /api/push` | Remove a subscription by endpoint | line 1, columns 3744–3867 |
| `POST /api/push/test` | Request a browser test notification | line 1, columns 3491–3617 |

The state snapshot returned HTTP 200 at **2026-10-27 22:32:38 UTC**, with:

```json
{
  "stock": [],
  "events": ["recent stock transition records"],
  "status": {
    "last_sweep_at": "2026-10-27T22:32:37.616Z",
    "stores_seen": 29,
    "requests": 3,
    "failures": 0,
    "duration_ms": 1896,
    "last_ok_at": "2026-10-27T22:32:37.616Z",
    "last_error": null
  }
}
```

Event fields observed in real state/history replies are `id`, `store_id`, `part_number`, `started_at`, and nullable `ended_at`. Stock consumers expect `store_id`, `part_number`, and `changed_at`. History returns `events`, `hasMore`, and `stats` containing `drops`, `stores`, `median_s`, and `byHour`.

Only the state/history GET endpoints were exercised. Static discovery does not establish authorization, reachability, or success of the push mutations.

### 4. Browser push delivery — answered for client behavior

The original uses browser Web Push, not Discord. It registers `/sw.js`, obtains or creates a `pushManager` subscription with a public VAPID application-server key, and sends:

```json
{
  "subscription": "PushSubscription.toJSON()",
  "parts": ["selected part numbers"],
  "storeIds": ["selected stores, or null for all in the region"],
  "openIn": "app or safari",
  "test": "true on first subscription"
}
```

Selections and checkout preference are saved in localStorage. On iOS, the UI checks whether the site runs as an installed standalone app before enabling supported notifications.

The service worker handles `install`, `activate`, `push`, and `notificationclick`. Push messages use a notification object with a title, body, and navigation destination. Clicking closes the notification and opens that URL. The remote notification sender's deduplication rules, database, and delivery implementation remain unknown.

The replacement's Discord private-channel webhook is a separate delivery implementation. A Discord webhook posts to its channel; it is not a bot DM.

### 5. Demo fallback and freshness — answered statically

For **HTTP 503 specifically**, the state loader constructs fabricated stock, timestamps, recent events, and a healthy-looking sweep status, and enables its demo flag. The history loader also generates sample events/statistics on 503. These are explicit fallback branches in the delivered bundle; they were not triggered during the recorded successful GETs.

The UI labels sweeps older than 60 seconds as paused and detects incomplete store coverage against its regional list. A network exception returns without replacing the previous state.

The replacement should preserve unknown/error state explicitly rather than interpreting these sample records as Apple inventory.

### 6. Region and NJ coverage — answered

The catalog defines a **40-mile radius** from **40.754, -73.984**, with seed ZIPs `07470`, `08837`, and `06830`. Filtering its catalog using the shipped distance formula reproduces **29 stores**. Only **9 are in NJ**:

American Dream, Bridgewater, Freehold Raceway Mall, Garden State Plaza, Menlo Park, Rockaway, Short Hills, Tice's Corner, and Willowbrook.

It excludes the following NJ stores because they lie outside that radius:

| Store | ID | Approximate distance from reference center |
| --- | --- | --- |
| Quaker Bridge | `R549` | 48.6 miles |
| Sagemore | `R088` | 78.0 miles |
| Cherry Hill | `R206` | 78.4 miles |

The replacement therefore needs its own complete 12-store NJ allowlist and southern NJ query coverage. Copying the reference's 29-store region would miss three required stores.

### 7. Checkout links — answered statically

The exact reference URL builder uses:

```text
https://www.apple.com/shop/buy-iphone/iphone-duo/7.6-inch-display-256gb-night-sky-unlocked
https://www.apple.com/shop/buy-iphone/iphone-duo/7.6-inch-display-256gb-star-white-unlocked
```

Its Safari variant appends `?skipul=1#PART_NUMBER`; notification navigation uses `x-safari-https:` for Safari or `applestore://` for the Apple Store app. These URL constructors were observed in code; successful checkout was not tested.

A separate user-triggered Safari bookmarklet reads a product part and Apple's `as_atb` cookie, requests Apple's add-to-bag flow with `purchaseOption=fullPrice`, unlocked configuration, and an add-to-cart parameter, then navigates to the bag if accepted. The monitor task does not require executing or reproducing that purchase mutation.

## Source identity

| File | SHA-256 |
| --- | --- |
| `0xuypb9i7gi-8.js` | `62f57ada2a1b58f2e1963d10dccda01cad7a73761f944629a13314194de514ee` |
| `1dibk0bicgbb7.js` | `efea99bb585d2d6bcdb244efaa821e93b47c3d29dbc3a50d74f9e707225700b1` |
| `sw.js` | `388149087864b88b1dc8c3a20f6f36b11c59ec8e066006472487dd8621fbf954` |
| `state.json` | `9aad714035ebf823b7a3024e1fb4e0dcc2e2143f4573635d4391d044426c2a16` |
| `history.json` | `181e0b4bda7a2a5a6db4bb4b2a1c103437d300722ba5fc40e7b25734eec87dfa` |

The report's static findings refer to the parent REA Evidence above and those exact scripts. GET results and derived regional distances are separately labeled. Private backend code, database schema, notification sender, deployment secrets, and exact scheduler implementation remain unresolved; this is a functional reconstruction from public evidence, not recovery of original server source.
