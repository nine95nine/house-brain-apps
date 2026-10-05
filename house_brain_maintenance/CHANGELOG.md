# Changelog

## 0.5.1

- **Fix (live 2026-10-05):** Cloudflare refused the Scout key switch with HTTP 403, because the App sent Python's
  default "Python-urllib" user agent; the Scout and Observer always named themselves. Every request now says
  `house-brain-maintenance/<version>`. This also fixes the Recovery Report's outside liveness ping. The key switch
  had failed safely: the Scout kept its working key.
- Owner decision 2026-10-05 ("All of the above"), all read-only:
  - **Re-login needed**: an integration waiting for you to log in again (Tesla, Ring, Google, ecobee ...) is
    named, with the steps. A reload is no longer offered for it, because it cannot help. One more read-only
    WebSocket command, `config_entries/flow/progress`, keeps only the entry ids of pending re-login flows.
  - **Liveness ping failing**: if the outside "home reachable" ping keeps failing for 30 minutes, you are told.
  - **App stopped for 30+ days**: said once, with how to uninstall it if it is no longer needed. Nothing is
    removed. Counting starts when 0.5.1 is installed.
  - **Backup size jump**: the newest full backup is under half, or over double (and at least 1 GB more than),
    the usual size of the previous ones.

## 0.5.0

Owner decision 2026-10-04 ("all of the above"):
- **Waiting period for automatic installs** (`auto_wait_days`, default 3): with `auto_low_risk`, a low-risk
  bug-fix update installs by itself only after the App first saw it offered this many days ago, at night.
  While such an update waits for its time or the night window it is **not asked** (0.4.x asked when it was
  found outside the window). Risky updates and `ask` mode are unchanged.
- **Backup copy off the Pi**: warns when no recent backup is stored anywhere but the Pi.
- **Low batteries and devices offline for days** (about every 6 hours; phones and tablets skipped).
- **Disk filling too fast**: a forecast from twice-daily free-space readings (urgent when full within 7 days).
- Four more read-only Core WebSocket commands (`backup/info`, `get_states`, entity and device registry lists),
  projected at once to the few fields these checks need.

## 0.4.2

- Smarter morning summary (owner decision 2026-10-02, after the first two summaries): log errors that only show
  the internet or DNS being unreachable (for example Sense, NWS alerts, ecobee and the relay in one blip) are
  merged into one line, "Internet or DNS dropped briefly", listing what was affected. Repairs and other errors are
  never merged. The "cleared" list no longer repeats the same item.
- Unchanged by owner choice: updates for Apps set to start by hand (for example the AquaRite bridge) are still
  offered for approval.

## 0.4.1

- **UPnP router not found: the real cause instead of a reload that cannot work** (live finding 2026-10-02:
  "Device not discovered" for the Orbi RBR840; the offered reload ran and the problem stayed). Home Assistant
  sets UPnP up only after it hears the router's network announcement (SSDP), so a reload repeats the same wait.
- For that case the App now reads, read-only and for a few seconds, what Home Assistant currently hears on the
  network (one new read-only WebSocket subscription; addresses, locations and headers are dropped at once) and says:
  **router stopped announcing UPnP** (turn UPnP on / restart the router), **router came back with a new identity**
  (add the discovered router, delete the old entry), **Home Assistant hears nothing at all** (its network adapter),
  or **router heard again** (only then the reload is offered).
- An already-reported problem is reported again once when its cause is first found or changes (at most every
  6 hours). Only counts (devices heard, gateways heard, IGD versions) go to the tracking issue.

## 0.4.0

- **One-tap power cycle of the pool Wi-Fi extender** (owner-approved design, 2026-10-02). When iAquaLink has
  reported "offline" for at least 10 minutes, the phone asks "power-cycle the pool Wi-Fi extender?". After
  **Approve** (Face ID) the App turns the plug `extender_plug_entity` (default
  `switch.iaqualink_wifi_plug_socket_1`) off, waits 10 seconds, turns it back on and checks it reports on; then it
  waits up to 10 minutes for iAquaLink to reconnect and reports **fixed** or **not fixed**.
- If the plug does not report back on, it tries again and sends an **urgent** notification to turn it on by hand.
- Only that one switch, only for iAquaLink offline, never without a tap, never in practice mode or automatically;
  at most 3 power cycles a day, at least 30 minutes apart. Clear `extender_plug_entity` to switch it off.

## 0.3.0

Owner-approved design, 2026-10-01 (decisions D1-D6, `docs/architecture/HA_RECOVERY_REPORT_R1_APP_AND_WORKER_DESIGN.md`).

- **Recovery Report: what happened while Home Assistant was down, and what to check.** (`recovery_report`, on by default; read-only.)
  - A background check every minute notices when Home Assistant Core stops answering, when the host rebooted while the App was away, and network or power outages (the #73 outage sensors and the UPS).
  - When it is over, it records how long it lasted and the likely cause with how sure it is:
    - planned update or restart;
    - power cut with the UPS running out;
    - unexpected host reboot;
    - crash, with the error lines from just before it;
    - network only;
    - or "could not tell".
  - It uses the Connection Forensics clean/unclean verdict when that package is installed.
  - A 30-minute checklist follows: smoke/CO sensors first, then the UPS recharging, the thermostat, Sense and solar.
  - The **Maintenance page** shows the latest report, the checklist, what to do, and the earlier outages, with a **Got it** button. "Got it" is owner only and single use.
  - A persistent notification appears in Home Assistant at once.
- **One push** to `notify_service` once the internet is back. If it cannot be sent, it is retried every 5 min for up to 24 h and is never repeated after it arrives.
  - Short planned restarts (a clean shutdown, under 10 min, for example a Deployer install) are only shown on the page.
  - If the smoke/CO sensors are still not back after 15 min, a time-sensitive push goes to every phone in the new `safety_notify_services` option, once.
- **Optional off-site "still alive" ping** to your own liveness Worker (`liveness_url`, `liveness_key`, `liveness_interval_minutes`; off by default). It sends a signed timestamp and counter only, so the Worker can tell you "home not reachable" during an outage.
- **New read-only routes** (exact, in the one allowlist):
  - previous-boot and current Core log tail (`/core/logs/boots/-1`, `/core/logs`, `?lines=` only);
  - the state of 11 named entities.
  - Plus create/dismiss of its own persistent notification (`hbm_recovery_report`) only.
  - No privilege change: the App is already `manager`.

## 0.2.2

- **Privacy fix (live, 2026-09-30):** a "fix not fixed" report put an integration title (the owner's
  e-mail address, used as the iAquaLink entry title) into the GitHub report heading and fix label.
  The whole report is now scrubbed as the last step. App names are no longer cut as "long ids"
  (only strings with digits are).
- **Known fix for this house:** when iAquaLink reports the pool system offline, the alert says to unplug
  the Wi-Fi extender by the pool equipment for 10 seconds (owner's proven fix) instead of offering a reload.

## 0.2.1

- **Safety fix (live, 2026-09-30):** 0.2.0 offered to restart a crashed App without reading its boot
  setting. The AquaRite commissioning bridge (`boot: manual_only`, kept deliberately non-running) was
  offered a restart, the owner approved it, and it started. Now an App set to start by hand (`manual`
  or `manual_only`) is only reported (warning, no fix), and the restart/start route is refused again at
  fix time unless the App starts at boot.

## 0.2.0

- **Problem alerts and one-tap fixes** (owner decisions 2026-09-30). Every check (5 min) reads, without
  changing anything: Home Assistant Repairs, Supervisor health/issues/suggestions, crashed or stopped Apps,
  integrations that failed to start, errors in the log, disk space and backups.
- Serious problems notify at once; warnings, log errors and "cleared" notes come in one summary at
  `digest_hour` (default 8). Each problem is reported once (log errors at most once a week); details go to
  the tracking issue with secrets, IP/MAC/e-mail addresses and long identifiers removed.
- Safe fixes only after Approve (Face ID): restart/start one App, reload one integration, or apply one of
  Supervisor's own repair/reload/App-restart suggestions; verified afterwards (FIXED / NOT FIXED + next steps).
  Never reboot, stop, remove, clear backups, disk changes, Core restart or updates as a "fix".
- A Home Assistant automatic backup (stored as `partial` with Home Assistant included) counts as a full backup.
- New options: `issue_checks` (default on), `digest_hour` (default 8). `max_approval_requests_per_day`
  now also limits fix questions.

## 0.1.2

- The Log tab now shows each step of a job (approval asked, backup, update, health watch,
  result), with the same redacted content as the App's audit file.
- Practice mode (`dry_run: true`) reports each App version once per day instead of at every
  hourly check (seen live 2026-09-30: the same NUT review was posted every hour).
- No change to permissions, options, allowlist, review, backup/update/restore behaviour.

## 0.1.1

- Fix (first live install, 2026-09-30): when the job-request branch was missing, or GitHub
  failed, every poll logged `poll failed ... Branch not found` and the update check never ran.
  A missing request branch now means "no requests" (logged once), and any GitHub fault while
  checking requests is logged once and no longer blocks update checks.
- No change to permissions, options, allowlist, update review or restore behaviour.

## 0.1.0

- First release (owner decisions D1-D9 and 2026-09-29 Maintenance-chat decisions).
- App updates: hourly check, deterministic review (low risk / risky / held), iPhone approval,
  verified single-App backup, update, health watch (App state, Core API, Supervisor health,
  dependent Apps), automatic restore of that App's backup on failure with version quarantine,
  crash-safe journal. Optional `update_mode: auto_low_risk` for bug-fix updates in a night
  window after 3 approved successes. Never Core/OS/Supervisor or itself.
- Scout jobs: `ROTATE_SCOUT_KEY` (prepare -> Broker learns the fingerprint -> activate only when
  the Broker already accepts the new key) and `RUN_SCOUT_ONCE` (result read from the Scout's
  latest-run log; only parsed fields are reported).
- Approval engine, ingress page, GitHub client, journal and HTTP helper from House Brain
  Deployer 0.2.1 (lineage in the design record).
- Base image `3.14-alpine3.24-2026.08.0` (estate base).
