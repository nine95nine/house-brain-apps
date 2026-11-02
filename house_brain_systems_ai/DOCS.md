# House Brain Systems AI

Version 0.3.2 (shadow). 0.3.2 fixes two things seen on the first live 0.3.1 page:
- **Patched versions:** an advisory whose fix is already installed (your version is at or past its patched version) reads "not affected", even when GitHub's affected range is open-ended.
- **Investigations:** each changing thing is one line ("changed 3 times shortly before"), with updates and App states ranked above routine status flips.

Version 0.3.1 (shadow). 0.3.1 fixes two things seen on the first live 0.3.0 page:
- **ESPHome:** an ESPHome add-on that is not installed still answers with no version; that now counts as "not installed" and the next ID is tried, so the beta Device Builder is found.
- **Investigations:** they open only while a problem is still active (or has come back), and live ones are listed first. The closed items the 0.3.0 install opened for already-cleared cases are cleared once.

Version 0.3.0 (shadow). 0.3.0 adds **investigations**, the first step of the approved agentic layer (design R1 step A1):
- **When one opens:** automatically, for a serious problem, one that came back, or a warning still open after an hour.
- **What it is:** a bounded work packet: case, question, budget, a 7-day expiry, read-only research permission and no
  execution authority.
- **How it answers:** today only Systems AI's own rule agent (T0) answers, from the evidence:
  - the problems and the changes seen before them;
  - the same problem in earlier cases (case memory) and how it ended;
  - possible causes listed side by side;
  - the fix Systems AI already suggests, if any (your tap);
  - otherwise "needs an engineer".
- **O&M's part:** each case also carries an empty repair record that only O&M will fill in.
- **Unchanged:** no new read, outside request, model or authority.
0.2.3: when the AI kill switch is missing or stale (never on FULL_STOP), this App still makes
one read-only Supervisor read of AI Access every 5 minutes. AI Access publishes the switch, and if it stops, every
House Brain AI holds (seen live on 2026-11-01). If AI Access is stopped, the page and the family line say so, and
after 15 minutes Maintenance shows a "Start AI Access" suggestion, carried out only on your tap. Nothing else runs while
held.
Version 0.2.2 (shadow). Boot is manual (turn on Start on boot in the App's page; it is read-only, so that is safe).
Only `SHADOW` mode exists. 0.2.2 reads the ESPHome Device Builder's version (its Supervisor info, options dropped),
so ESPHome security advisories are matched to the version you run, and the System Monitor CPU and load sensors
when you enable them. No new outside read. 0.2.1 added the **World Change Radar**:
public security news that touches what runs here (see below). It is this App's first and only outside read,
10 requests a day at most, with no key and $0. 0.2.0 added **cases**: each incident is
followed from first sign to a verified fix and kept after the incident has rolled off (see below); no new
read, route or authority. 0.1.4 also watches Water AI's own health
block (Water AI serves it from 0.1.0); nothing else changes. 0.1.3 opened a fresh Core WebSocket for each
cycle and retries a command once on a fresh socket when a kept one has gone stale, so the WebSocket read no
longer fails every other cycle; the four read-only commands are unchanged. 0.1.2 added one read-only, internal-network
route, `GET /archive/<day>.json`, so the coming Cooling AI can read archived days (see Outputs), and
watches Cooling AI's own health block once it is installed. 0.1.1 added the Arbiter's decision,
reason, owner, kind and shadow selection, the A/C's real call (`binary_sensor.ac_compressor_cooling`), four
eligibility flags, the modelled evap supply temperature, the vent floor and the APS price to the cooling
archive (31 entities). The new entities' 40 days are copied first after the update (about 9 hours).

## What it does

- **Reads, never writes.** It makes three kinds of request:
  - `GET` to the Supervisor's `info` routes (`/info`, `/host/info`, `/resolution/info`, `/addons/self/info` and `/addons/<House Brain App>/info`), with role `default`;
  - `GET /core/api/states`;
  - `GET /core/api/history/period/...` for the 31 cooling archive entities only (states, no attributes, one day at a time);
  - four read-only Core WebSocket commands (`system_log/list`, `repairs/list_issues`, `system_health/info`, `recorder/list_statistic_ids`). From 0.1.3 the socket is closed at the end of every cycle, and a command that fails on a socket kept from earlier in the cycle is tried once more on a fresh one. A failure on a fresh socket is a real failure.

  There is no POST, state write or service call anywhere in the App. Sibling App info is cut down to slug, name, version, state, boot, hostname, ingress port, watchdog and update flag before anything is kept; their options are never stored.
- **Finds its siblings without settings.** It reads its own slug, then checks the known House Brain App names under the same store prefix and under `local_`, once an hour.
- **Self-health.** It reads each App that serves `house_brain_app_health.v1` (Energy AI 0.1.2 and later; Cooling AI and Water AI from 0.1.0) at `http://<hostname>:<ingress port>/health`. It checks the block with the shared validator: unknown, stale, invalid or unreachable is never OK.
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
- **World changes (0.2.1).**
  - **Sources:** the CISA list of vulnerabilities attackers are using now, and GitHub security advisories for Home Assistant Core, OS, the Supervisor and ESPHome.
  - **Reads:** at 03:20 and 15:20, one source a minute, at most 2 attempts per source a day (10 requests a day). A failed read is retried once, 30 minutes later. No read happens while the kill switch holds this App. Every attempt is saved before it is sent, so a restart never repeats one.
  - **Matching:** an advisory touches you when the installed version is in its affected range. A recent CISA entry (30 days) for a product you run touches you until that component is updated. An unknown version is "version unknown", never "not affected". ESPHome's version comes from its add-on (stable, beta or dev, from ESPHome's own repository); if none is found, its advisories show as version unknown.
  - **Output:** a match raises `WORLD_CHANGE_TOUCHES_YOU` (a problem, so it joins incidents and cases) and suggests the matching update in Maintenance. Systems AI changes nothing.
  - **Failures:** a source that fails keeps its last good result with its age. One that was never read says "not checked", never "no threats".
  - **GitHub data:** the advisories show "not yet proven on live data" until the first live read parses.
- **Cases (0.2.0).** Each incident becomes a case, kept in this App's own database:
  - `OPEN` while a problem of it is open; `ACTION_SUGGESTED` when Systems AI also suggests a fix for one of them (still only your tap in Maintenance or the Deployer);
  - `WATCHING` for 24 hours after every problem cleared, then `FIXED_VERIFIED` if none came back;
  - `REOPENED`, linked to the newer case, if one came back during the watch.
  - Four repair checks: *service restored* (measured when the problems clear), *prevention proven* (none back for 7 days; `FAILED` if one came back), and *cause corrected* and *side effects reconciled*, which this App cannot measure and always shows as `NOT_MEASURED`, never as passed.
  - An open case whose incident rolled off the 60 kept is marked "no longer measured", never fixed.
  - While the kill switch holds Systems AI, cases do not move. A problem that cleared during a hold is checked again from the end of the hold. It counts as restored only after a full 24-hour watch with it absent.
  - Kept 90 days after it closes, 200 at most (oldest closed first); the page shows the newest 20 and counts by status.
- **Error budgets.** Core API, Supervisor API, WebSocket, the kill switch and each running House Brain App each get a 99.5 % 30-day budget, with 1 h / 6 h burn rates. Unknown counts as bad.
- **Keeps the cooling season's detail before Home Assistant forgets it.** Home Assistant keeps full detail for 40 days. Once started, this App copies the recorder history of 18 cooling plant entities (outdoor and indoor temperature, humidity and dew point, the evap relays and relay mode, the Arbiter phase, the eligibility code, AC power, the attic thermometers), one entity-day a minute, oldest day first; the 40 days are copied in about 12 hours, then each new day after midnight. It keeps 5-minute time-weighted values for numbers and exact change times for relays, phases and codes, for 120 days (about 10 MB a season). A steady reading counts as idle, not missing; only `unknown`/`unavailable` are gaps. Nothing about people, presence or location is read. Any day downloads as CSV from the page. Your Home Assistant backups include it.
- **Advice.** "Good time to ask" is the Cooling Arbiter `IDLE` and outside 22:00–07:00. There is also a shared list of readings no AI should trust.

## Outputs

- **Ingress page** (sidebar): plain words first, full evidence behind "tap to open".
- **Internal network only** (the /23 holding the Supervisor proxy address, and loopback; never the LAN):
  - `GET /health`: this App's own `house_brain_app_health.v1`.
  - `GET /digest`: one family status line of at most 255 characters, plus one line per App. Maintenance relays it into the one family sensor for Companion; owner decision 2026-10-20, built on the Maintenance side as a separate release.
  - `GET /archive/YYYY-MM-DD.json` (0.1.2): one local day of the cooling archive, rows `[entity, time, value, text, kind]`, plus each entity's read status for that day (`DONE`, `EMPTY`, `FAILED` or `NOT_READ`, and whether the purge had cut into it), so a gap is never read as a value. Schema `house_brain_systems_ai_archive_day.v1`. Read by Cooling AI (owner decision 2026-10-25 "Read Systems AI's archive"). Anything but an exact day is refused.
  - `GET /proposals`: at most 10 suggestions, only `start_app` or `restart_app` for a House Brain App slug, each open at least 15 minutes and not flapping, expiring after 1 hour. Maintenance may show them under "Needs your decision"; nothing happens without the owner's Approve. They are counted in a shadow scorecard (`executed: false`).
- Any other method gets HTTP 405 `READ_ONLY_APP`.

## Kill switch

Every fast cycle reads `sensor.house_brain_ai_kill_switch_effective`. FULL_STOP holds all collection and shows `HELD`; that is not a problem. A missing, stale, future-dated or unknown state also holds, and is reported as a problem. `READ_ONLY` (the owner's steady state) allows everything this App does, because it never writes. During a hold, `/proposals` is empty.

## Bounds

- About 8,000 local requests a day, all inside the house: states each minute, Supervisor and sibling reads each 5 minutes, database size hourly, statistic ids daily. While the archive catches up, one more read a minute (about 700 reads once); after that 18 a day. From 0.2.2, up to 3 more an hour to find the ESPHome Device Builder (stable, beta, dev; 1 once found).
- Outside requests (0.2.1): at most 10 a day, only to `www.cisa.gov` (one exact URL) and `api.github.com` (four exact URLs). They send no key or token, refuse redirects, use the system TLS store and are capped at 6 MB. No paid API.
- Python standard library only.
- 256 MiB address cap. App-owned SQLite (mode 0600): 7 days raw, a year of hourly rollups.
- 2,000 log templates at most, 60 incidents, 200 cases, 200 changes, 400 frozen forecasts per metric.

## Not in 0.1

- The busiest-entity ranking and the owner tap ledger wait for Maintenance to share what it already measures.
- Feeding the offline evaluators is a later release.
- The outside Pi-freeze check cannot be seen from the Pi.
- Disk, CPU and load System Monitor sensors and the per-App Supervisor CPU/memory sensors are disabled in Home Assistant by default. Turning them on is the owner's UI step. From 0.2.2 this App reads `sensor.system_monitor_processor_use` and `sensor.system_monitor_load_15_min` when they exist; until then they read as missing, never OK. Disk space comes from the Supervisor.

## Recovery

Stop the App; it holds nothing that needs undoing. Its database can be deleted to start learning again, but that also deletes the cooling archive: download the days you want first.
