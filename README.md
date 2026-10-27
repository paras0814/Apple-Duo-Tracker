# Apple-Duo-Tracker

Track Apple iPhone availability across **all 12 New Jersey stores and 7 Manhattan stores**. A local background monitor checks Apple's pickup endpoint every **5 seconds**, verifies **256GB**, and alerts a Discord channel on initial available stock and subsequent restocks.

## Run on Windows

Double-click **Start.cmd**. It opens the dashboard at **http://127.0.0.1:8765** and keeps the monitor running in the background. On a new checkout it downloads a checksum-verified portable Python runtime from python.org and creates `config.local.json` from the example. No pip packages are required.

Put your Discord webhook and optional HTTP proxies in **config.local.json**, then restart with **Stop.cmd**, followed by **Start.cmd**. Keep the PC awake and connected. Normal Wi-Fi works when Apple accepts direct requests; proxies are optional. Closing the dashboard does not stop monitoring.

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

Percent-encode special characters in proxy usernames/passwords. The proxy pool is used for Apple inventory requests; Discord delivery connects directly. Credential-bearing transport errors are not exposed in logs. A working proxy is reused for each ZIP; transport failures pause that route for 30?300 seconds and allow one alternate proxy within the request budget. HTTP 403/541 pause that proxy for at least five minutes while the ZIP Retry-After remains in force. The dashboard shows how many proxies are ready. If overlapping successful responses still verify every product, failed upstream checks remain visible under connection-recovery details instead of being labeled an inventory outage. The monitor disables inherited system/environment proxies so this local setting is authoritative.

## Polling and duplicate suppression

The endpoint is `https://www.apple.com/shop/retail/pickup-message?pl=true&location=ZIP&parts.0=MK1F4LL%2FA&parts.1=MK1E4LL%2FA`. The monitor reads `body.stores[].partsAvailability[part].pickupDisplay` and accepts only catalogued store IDs with the expected NJ/NY state. Three regional ZIPs cover the area, with targeted ZIP fallback for missing stores.

Sweeps start every five seconds under normal conditions and never overlap. Slow requests can extend the interval; Apple errors trigger backoff and `Retry-After` is honored. HTTP 403/429/541 trigger at least a one-minute cooldown. Failed, missing, stale, or unverified data is **unknown**, never fabricated stock or a sold-out transition.

SQLite state in `.data/monitor.sqlite3` suppresses repeated alerts per **store + part** across polls and restarts. Only confirmed unavailable → available transitions rearm a restock alert. Initial available stock also alerts. Pending alerts are retried after definite failures and expire when stock disappears. A send with an ambiguous outcome is marked `uncertain` and is not automatically resent, preventing blind duplicates; the UI exposes that status.

## Validation

```powershell
python -m unittest discover -s tests -v
node --test tests/test_checkout.js
```

Tests cover product/storage/state validation, initial/restock transitions, restart deduplication, cooldowns, authenticated HTTP CONNECT proxies, fixture HTTP responses, Discord message links, local credential isolation, and the checkout helper. Browser QA covers desktop/mobile, both regions, filters, maps, clipboard fallback, and disconnection/recovery. See `research/browser-qa.json`.

## REA investigation

The public reference was investigated with [REA](https://github.com/morluto/rea), its reverse-engineering skill, and its static JavaScript analyzer. See **[the evidence-backed report](research/REVERSE-ENGINEERING.md)**.

The reference's browser polls every **3 seconds** while visible; its five-second backend cadence is advertised, with private implementation unavailable. Its 29-store region includes only **9 NJ stores**, so this monitor adds the missing NJ locations and the requested Manhattan stores. The reference also has a 503 demo-stock fallback; this monitor exposes real failures.

REA 6.8.0 was installed through `npx rea-agents@latest setup` after plan approval, and its Codex registration/skill and an actual stdio MCP handshake were verified (229 tools). The REA MCP tools are now connected in the active Codex session. The separate `rea/` source checkout was also built and used for the investigation; it is excluded from this repository. `Run-REA.cmd` uses that existing checkout/runtime. A fresh clone can install REA through the upstream instructions.

Downloaded reference scripts, full generated evidence, screenshots, local setup diagnostics, runtime binaries, credentials, and monitoring data stay local. The compact findings and provenance report are versioned.

## Ordering automation status

Live automatic purchasing is **disabled**. The requested exploration limits are **$1,300 maximum per phone including tax**, **one phone at a time**, either monitored 256GB color, no AppleCare, and selection from the watched stores. The proposed implementation permits one successful order per armed run and requires manual rearming. Apple account/payment authentication, authoritative final-total verification, and order confirmation/recovery must be implemented and tested before unattended ordering can be enabled. Card details should be entered in Apple's own payment interface and must not be committed here. See the [automatic checkout exploration](research/CHECKOUT-AUTOMATION.md) for the next implementation steps and remaining runtime evidence.
