# Apple-Duo-Tracker

Track Apple iPhone availability across **7 New Jersey stores**. A local background monitor checks Apple's pickup endpoint every **5 seconds**, verifies **256GB**, and alerts a Discord channel on initial available stock and subsequent restocks.

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

**New Jersey:** American Dream, Bridgewater, Garden State Plaza, Menlo Park, Rockaway, Short Hills, and Willowbrook.

No New York City locations are monitored. Cherry Hill, Freehold, Quaker Bridge, Sagemore, and Tice's Corner are excluded.

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

The endpoint is `https://www.apple.com/shop/retail/pickup-message?pl=true&location=ZIP&parts.0=MK1F4LL%2FA&parts.1=MK1E4LL%2FA`. The monitor reads `body.stores[].partsAvailability[part].pickupDisplay` and accepts only catalogued store IDs with the expected NJ state. Three regional ZIPs cover the remaining stores, with targeted ZIP fallback for missing stores.

Sweeps start every five seconds under normal conditions and never overlap. Slow requests can extend the interval; Apple errors trigger backoff and `Retry-After` is honored. HTTP 429 responses trigger at least a one-minute cooldown. Direct session rejections get one bounded renewal attempt per sweep, followed by another check on the configured interval; proxy-specific rejections can use another ready route. Persistent HTTP 541 means Apple is refusing the requests: the process can remain running while stock coverage is unavailable. Failed, missing, stale, or unverified data is **unknown**, never fabricated stock or a sold-out transition.

SQLite state in `.data/monitor.sqlite3` suppresses repeated alerts per **store + part** across polls and restarts. Only confirmed unavailable → available transitions rearm a restock alert. Initial available stock also alerts. Pending alerts are retried after definite failures and expire when stock disappears. A send with an ambiguous outcome is marked `uncertain` and is not automatically resent, preventing blind duplicates; the UI exposes that status.

## Validation

```powershell
python -m unittest discover -s tests -v
```

Tests cover product/storage/state validation, initial/restock transitions, restart deduplication, cooldowns, authenticated HTTP CONNECT proxies, fixture HTTP responses, Discord message links, local credential isolation, and the bookmarklet helper. Browser QA covers desktop/mobile, store filters, maps, clipboard fallback, and disconnection/recovery. See `research/browser-qa.json`.

## REA investigation

The public reference was investigated with [REA](https://github.com/morluto/rea), its reverse-engineering skill, and its static JavaScript analyzer. See **[the evidence-backed report](research/REVERSE-ENGINEERING.md)**.

The reference's browser polls every **3 seconds** while visible; its five-second backend cadence is advertised, with private implementation unavailable. This monitor focuses on New Jersey and exposes real failures instead of demo stock.

REA 6.8.0 was installed through `npx rea-agents@latest setup` after plan approval, and its Codex registration/skill and an actual stdio MCP handshake were verified (229 tools). The REA MCP tools are now connected in the active Codex session. The separate `rea/` source checkout was also built and used for the investigation; it is excluded from this repository. `Run-REA.cmd` uses that existing checkout/runtime. A fresh clone can install REA through the upstream instructions.

Downloaded reference scripts, full generated evidence, screenshots, local setup diagnostics, runtime binaries, credentials, and monitoring data stay local. The compact findings and provenance report are versioned.
