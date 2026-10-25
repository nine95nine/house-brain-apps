# House Brain Systems AI

Unreleased source candidate (0.1.0, shadow). Store publication, installation and every action stay
separate owner gates. Boot is manual. Only `SHADOW` mode exists.

## What it does

- **Reads, never writes.** It makes three kinds of request:
  - `GET` to the Supervisor's `info` routes (`/info`, `/host/info`, `/resolution/info`, `/addons/self/info` and `/addons/<House Brain App>/info`), with role `default`;
  - `GET /core/api/states`;
  - `GET /core/api/history/period/...` for the 18 cooling archive entities only (states, no attributes, one day at a time);
  - four read-only Core WebSocket commands (`system_log/list`, `repairs/list_issues`, `system_health/info`, `recorder/list_statistic_ids`) on one long-lived socket.

  There is no POST, state write or service call anywhere in the App. Sibling App info is cut down to slug, name, version, state, boot, hostname, ingress port, watchdog and update flag before anything is kept; their options are never stored.
- **Finds its siblings without settings.** It reads its own slug, then checks the known House Brain App names under the same store prefix and under `local_`, once an hour.
- **Self-health.** It reads each App that serves `house_brain_app_health.v1` (Energy AI 0.1.2 and later) at `http://<hostname>:<ingress port>/health`. It checks the block with the shared validator: unknown, stale, invalid or unreachable is never OK.
- **Learns:**
  - database size (hourly, from `system_health/info`), disk free (`/host/info`), memory free and swap (System Monitor sensors);
  - errors per integration per day, against its own 14-day median and spread;
  - message kinds in the system log (masked templates; numbers, addresses, ids and e-mails become placeholders);
  - Repairs issues, Supervisor resolution issues, and App state and version changes.
- **Forecasts with honest bands.** A robust trend (Theil-Sen) with a split-conformal 90 % band. Each forecast (1, 7 and 30 days) is frozen when made and scored later against the "no change" baseline. It is called a forecast only after it has beaten that baseline on at least 5 scored days.
- **One alert per problem:**
  - Problems are fingerprinted.
  - Maintenance's own findings are counted, not raised again.
  - A parent problem (Core or the Supervisor unreachable) hides the problems it explains.
  - Flapping is held back (Nagios-style weighted state change, 21 cycles, 50 %/25 % hysteresis).
  - Problems that open within 15 minutes of each other form one incident with a timeline, the changes seen in the 24 hours before it, and a draft postmortem whose causes are marked as hypotheses.
- **Error budgets.** Core API, Supervisor API, WebSocket, the kill switch and each running House Brain App each get a 99.5 % 30-day budget, with 1 h / 6 h burn rates. Unknown counts as bad.
- **Keeps the cooling season's detail before Home Assistant forgets it.** Home Assistant keeps full detail for 40 days. Once started, this App copies the recorder history of 18 cooling plant entities (outdoor and indoor temperature, humidity and dew point, the evap relays and relay mode, the Arbiter phase, the eligibility code, AC power, the attic thermometers), one entity-day a minute, oldest day first; the 40 days are copied in about 12 hours, then each new day after midnight. It keeps 5-minute time-weighted values for numbers and exact change times for relays, phases and codes, for 120 days (about 10 MB a season). A steady reading counts as idle, not missing; only `unknown`/`unavailable` are gaps. Nothing about people, presence or location is read. Any day downloads as CSV from the page. Your Home Assistant backups include it.
- **Advice.** "Good time to ask" is the Cooling Arbiter `IDLE` and outside 22:00–07:00. There is also a shared list of readings no AI should trust.

## Outputs

- **Ingress page** (sidebar): plain words first, full evidence behind "tap to open".
- **Internal network only** (the /23 holding the Supervisor proxy address, and loopback; never the LAN):
  - `GET /health`: this App's own `house_brain_app_health.v1`.
  - `GET /digest`: one family status line of at most 255 characters, plus one line per App. Maintenance relays it into the one family sensor for Companion; owner decision 2026-10-20, built on the Maintenance side as a separate release.
  - `GET /proposals`: at most 10 suggestions, only `start_app` or `restart_app` for a House Brain App slug, each open at least 15 minutes and not flapping, expiring after 1 hour. Maintenance may show them under "Needs your decision"; nothing happens without the owner's Approve. They are counted in a shadow scorecard (`executed: false`).
- Any other method gets HTTP 405 `READ_ONLY_APP`.

## Kill switch

Every fast cycle reads `sensor.house_brain_ai_kill_switch_effective`. FULL_STOP holds all collection and shows `HELD`; that is not a problem. A missing, stale, future-dated or unknown state also holds, and is reported as a problem. `READ_ONLY` (the owner's steady state) allows everything this App does, because it never writes. During a hold, `/proposals` is empty.

## Bounds

- About 8,000 local requests a day, all inside the house: states each minute, Supervisor and sibling reads each 5 minutes, database size hourly, statistic ids daily. While the archive catches up, one more read a minute (about 700 reads once); after that 18 a day. No external calls and no paid API.
- Python standard library only.
- 256 MiB address cap. App-owned SQLite (mode 0600): 7 days raw, a year of hourly rollups.
- 2,000 log templates at most, 60 incidents, 200 changes, 400 frozen forecasts per metric.

## Not in 0.1

- The busiest-entity ranking and the owner tap ledger wait for Maintenance to share what it already measures.
- Feeding the offline evaluators is a later release.
- The outside Pi-freeze check cannot be seen from the Pi.
- Disk, CPU and load System Monitor sensors and the per-App Supervisor CPU/memory sensors are disabled in Home Assistant today. Turning them on is the owner's UI step; until then they read as missing, never OK.

## Recovery

Stop the App; it holds nothing that needs undoing. Its database can be deleted to start learning again, but that also deletes the cooling archive: download the days you want first.
