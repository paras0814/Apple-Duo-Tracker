# Apple-Duo-Tracker

Track Apple iPhone availability across **all 12 New Jersey stores and 7 Manhattan stores**. A local background monitor checks Apple's pickup endpoint every **5 seconds**, verifies **256GB**, and alerts a Discord channel on initial available stock and subsequent restocks.

## Run on Windows

Double-click **Start.cmd**. It opens the dashboard at **http://127.0.0.1:8765** and keeps the monitor running in the background. On a new checkout it downloads a checksum-verified portable Python runtime from python.org and creates `config.local.json` from the example. No system-wide Python packages are required; the Apple transport is installed into the private `.runtime` directory.

Put your Discord webhook in **config.local.json**, then restart with **Stop.cmd**, followed by **Start.cmd**. Monitoring defaults to a persistent verified direct Apple session on your local connection. Start.cmd installs its checksum-pinned private transport when needed. The Windows monitor prevents idle system sleep while it runs; keep the PC powered and connected. Closing the dashboard or Codex does not stop the independent monitor. It makes no OpenAI requests and consumes no Codex usage allowance.

```powershell
.\Start.cmd --no-browser
.\Start.cmd --test-discord
.\Start.cmd --once
.\Start.cmd --test
.\Stop.cmd
```

With an existing Python 3.11+ installation, use `python launcher.py --no-browser`, or run `python monitor.py` in the foreground.

## Products and coverage

| Color | Storage | Part |
| --- | --- | --- |
| Night Sky | 256GB | `MK1F4LL/A` |
| Star White | 256GB | `MK1E4LL/A` |

`25GB` was a typo. Both the reference catalog and live Apple product titles confirmed **256GB**. Every check validates the Apple product title before treating a pickup status as available.

**New Jersey:** American Dream, Bridgewater, Cherry Hill, Freehold Raceway Mall, Garden State Plaza, Menlo Park, Quaker Bridge, Rockaway, Sagemore, Short Hills, Tice's Corner, Willowbrook.

**Manhattan:** World Trade Center, SoHo, West 14th Street, Grand Central, Fifth Avenue, Upper West Side, Upper East Side. Bronx, Queens, Brooklyn, and Staten Island are excluded.

The dashboard includes region/availability filters, store maps, current verification and coverage, alert delivery status, and the latest 100 stock events. Store IDs, ZIPs, and retail links are in `catalog.json`.

## Discord and quick checkout

This integration is a **private-channel incoming webhook**, not a bot DM. Restrict channel visibility in Discord. Alerts include the store name, a clickable Apple retail page, and a prominent exact-model purchase link with color, storage, unlocked configuration, full-price intent, and no-AppleCare intent.

The dashboard provides a repaired **Add to Bag bookmarklet**. Create a browser bookmark, copy the shortcut, and paste it into that bookmark's URL. Open the model link from Discord, run the bookmarklet on apple.com, select the pickup store, and complete checkout. It requests `purchaseOption=fullPrice`, `cppart=UNLOCKED/US`, and `acpart=none`, using the Apple session token already on that page. It supports only the two monitored 256GB parts and never submits payment.

Apple's site may still show its own selection/payment prompts. Product links and the helper do not reserve inventory, force store selection, or guarantee a successful add. The helper's parameters and control flow were verified with browser fixtures; no real order was placed.

## Local configuration

Copy `config.example.json` to `config.local.json`. Credentials belong only in the local file, which is ignored by Git. It is never served by the dashboard. Proxy URLs use this format:

```text
http://USER:PASSWORD@proxy.example:10000
```

The default `"monitor_connection": "direct"` ignores configured proxies and keeps one verified Apple session, renewing it on expiration and attempting one bounded recovery after a session rejection. It does not let an exhausted proxy pool block all ZIPs. HTTP 429 and explicit `Retry-After` still trigger backoff. The dashboard reports active proxies separately from configured proxies.

To use the persistent verified proxy pool, set `"monitor_connection": "proxy"`. Percent-encode special characters in proxy usernames/passwords. Each route keeps its own TLS connection and cookie jar; bootstrap, verification and inventory requests all use that same proxy. A working route is reused for each ZIP, and failed routes receive cooldowns with bounded failover. Rejected or broken sessions are discarded, including their private cookies, so a route gets fresh verification when it returns from cooldown. Route failures do not add a separate exponential ZIP delay; explicit Apple rate limits and `Retry-After` still apply. The eligible proxy count includes untested routes and is not a count of verified working IP addresses. Checkout and Discord delivery still connect directly. Credentials and cookie values stay out of logs, and inherited environment/system proxies are disabled.

## Polling and duplicate suppression

The endpoint is `https://www.apple.com/shop/retail/pickup-message?pl=true&location=ZIP&parts.0=MK1F4LL%2FA&parts.1=MK1E4LL%2FA`. The monitor reads `body.stores[].partsAvailability[part].pickupDisplay` and accepts only catalogued store IDs with the expected NJ/NY state. Three regional ZIPs cover the area, with targeted ZIP fallback for missing stores.

Sweeps start every five seconds under normal conditions and never overlap. Slow requests can extend the interval; Apple errors trigger backoff and `Retry-After` is honored. HTTP 429 responses trigger at least a one-minute cooldown. Direct session rejections get one bounded renewal attempt per sweep, followed by another check on the configured interval; proxy-specific rejections can use another ready route. Persistent HTTP 541 means Apple is refusing the requests: the process can remain running while stock coverage is unavailable. Failed, missing, stale, or unverified data is **unknown**, never fabricated stock or a sold-out transition.

SQLite state in `.data/monitor.sqlite3` suppresses repeated alerts per **store + part** across polls and restarts. Only confirmed unavailable → available transitions rearm a restock alert. Initial available stock also alerts. Pending alerts are retried after definite failures and expire when stock disappears. A send with an ambiguous outcome is marked `uncertain` and is not automatically resent, preventing blind duplicates; the UI exposes that status.

## Validation

```powershell
python -m unittest discover -s tests -v
node --test tests/test_checkout.js tests/test_beta_browser.js
```

Tests cover product/storage/state validation, initial/restock transitions, restart deduplication, cooldowns, authenticated HTTP CONNECT proxies, fixture HTTP responses, Discord message links, local credential isolation, and the checkout helper. Browser QA covers desktop/mobile, both regions, filters, maps, clipboard fallback, and disconnection/recovery. See `research/browser-qa.json`.

## REA investigation

The public reference was investigated with [REA](https://github.com/morluto/rea), its reverse-engineering skill, and its static JavaScript analyzer. See **[the evidence-backed report](research/REVERSE-ENGINEERING.md)**.

The reference's browser polls every **3 seconds** while visible; its five-second backend cadence is advertised, with private implementation unavailable. Its 29-store region includes only **9 NJ stores**, so this monitor adds the missing NJ locations and the requested Manhattan stores. The reference also has a 503 demo-stock fallback; this monitor exposes real failures.

REA 6.8.0 was installed through `npx rea-agents@latest setup` after plan approval, and its Codex registration/skill and an actual stdio MCP handshake were verified (229 tools). The REA MCP tools are now connected in the active Codex session. The separate `rea/` source checkout was also built and used for the investigation; it is excluded from this repository. `Run-REA.cmd` uses that existing checkout/runtime. A fresh clone can install REA through the upstream instructions.

Downloaded reference scripts, full generated evidence, screenshots, local setup diagnostics, runtime binaries, credentials, and monitoring data stay local. The compact findings and provenance report are versioned.

## Checkout beta

The beta prepares one phone in a **separate Chrome profile**, with a **$2,300 tax-inclusive cap**, quantity **one**, unlocked/full-price purchase, no trade-in, and **no AppleCare**. Payment submission is not implemented. Stock monitoring and Discord delivery continue independently.

1. Run **Setup-Beta.cmd** once to prepare the checksum-verified portable Node runtime; Chrome must also be installed.
2. Open the dashboard. In **Checkout beta**, choose any watched store or a specific store and either Duo color or a specific color.
3. Click **Arm Duo beta**. The next current, verified available-stock observation selects one matching phone and opens the dedicated Apple browser.
4. Alternatively, **Test iPhone 17e** performs a fresh stock check for the diagnostic **256GB Black iPhone 17e (`MHRN4LL/A`)**, then runs the same preparation path. It never adds this part to Duo monitoring or Discord stock alerts.
5. Use **Stop beta** to halt automation. Inspect and clear the separate browser bag, check the empty-bag box, and **Reset beta** before another run. The browser remains open for inspection. Restarting the monitor disarms a waiting run and preserves an interrupted preparation as needing attention.

The companion verifies an empty bag, exact part, all product options, and the selected upfront price before one add attempt. It never blindly retries an ambiguous add. A durable run lock prevents concurrent phones or automatic rearming. Browser cookies/payment information stay in Apple's local browser profile under ignored `.data/`; do not put card details in chat or this repository.

**Current live-test limitation:** Apple rejected the isolated diagnostic session with **HTTP 541**, including a native Add to Bag click over the direct connection; the bag was confirmed empty afterward. The user's regular browser did reach pickup selection. The beta therefore pauses with **Needs attention**. Pickup selection, populated-bag quantity controls, final tax-inclusive totals, and checkout are **not yet automated or verified**. A `review_ready` state requires every product/store/quantity/AppleCare/currency/tax/total fact to be verified within the limit; the current browser adapter does not invent those facts.

**Connection recovery:** the direct connection repeatedly returned HTTP 541. Persistent verified proxy sessions subsequently recovered all 19 stores and 38 products across repeated live five-second sweeps. This is a verified recovery, not an uptime guarantee; use the coverage and last successful check on the dashboard to judge whether current inventory is known.

**Later API investigation:** an explicitly authorized API test did complete the iPhone 17e pickup/billing/Place Order path and received a real Apple payment decline, followed by a private-channel failure webhook. The reusable API modules and durable guards are experimental; they are not wired into the dashboard's browser beta, and the subsequent iPhone 17 timed test was not completed. Beta development is paused while the stock monitor runs independently. No unattended order submission is enabled by default.

The browser companion now clicks Apple's native button instead of reconstructing an add request. Checkout uses the local connection with no configured monitoring proxies. A separate [REA investigation of Refract's Apple HTTP workflow](research/REFRACT-APPLE.md) records session bootstrap, cart-route branches and pickup model actions for the API beta; these findings are not a claim of successful automated checkout.

See the [live REA evidence and test results](research/BETA-CHECKOUT-EVIDENCE.md) and [checkout investigation](research/CHECKOUT-AUTOMATION.md).
