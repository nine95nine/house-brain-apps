# Changelog

## 0.3.18 — 2026-10-12

Two read-only AppArmor rules so the startup self-report can see its own mounts and AppArmor label (owner pop-up
2026-10-12 "Small release now"). Contains everything in 0.3.16 unchanged; draft PR #985 moves to 0.3.19.

- **Why:** on the Pi, 0.3.16 reported `apparmor=EACCES` and `mountinfo=EACCES` (#454 comment 6117189087). The
  existing `/proc/self/** r` rule does not match, because AppArmor checks the resolved `/proc/<pid>/` path.
- **Change:** `apparmor.txt` adds read access to `/proc/[0-9]*/mountinfo` and `/proc/[0-9]*/attr/{current,
  apparmor/current}`. The App's code is unchanged.
- **Unchanged:** no write, mapping, port, option or other permission. The profile compiles with
  `apparmor_parser`.

## 0.3.16 — 2026-10-12

A read-only startup self-report (owner pop-up 2026-10-12 "Self-report = 0.3.16"). Contains everything in 0.3.15
unchanged; draft PR #985 moves to 0.3.17.

- **Why:** the shared-writer layout proposal for the expanded Deployer needs facts only the running App can see:
  effective user/group, umask and capabilities, AppArmor label, Python/arch, and mode/owner/inode/mount facts for
  `/data`, `/homeassistant/packages`, `/share` and `/share/house_brain_writer_v1`.
- **Change:** after the "started" line, about ten `HBD-NATIVE` lines are logged once (each under 300 characters,
  readable through the Maintenance `APP_LOG_WINDOW` job).
- **Unchanged:** no write, chmod, directory creation, lock, network call, option or permission. A refused or failing
  probe logs its reason (for example `st=EACCES`), and the report can never stop the App from starting.

## 0.3.15 — 2026-10-11

Recovery never records an install as done when Home Assistant was not restarted (owner pop-up 2026-10-11 "Ask
restart again"). Found by a test that failed about 5 in 10 runs on `main`; it was a real bug, not a flaky test.
Contains everything in 0.3.14 unchanged.

- **Why:** after your restart Approve, the Deployer writes "restarting" to its journal and then asks Home Assistant
  to restart. If the App was stopped or killed between those two steps, the next start ran the health checks on the
  still-running, un-restarted Home Assistant and recorded **SUCCEEDED**. The new files stayed on disk without being
  loaded or checked, until some later restart.
- **Change:** recovery first checks the Deployer's restart marker. If Home Assistant did not restart, you get the
  restart approval again ("The Deployer stopped before it could restart Home Assistant"). Approve runs the usual
  pre-restart checks, restart and health checks. Reject or no answer puts the old files back (`ROLLED_BACK`; a
  no-answer can be asked again by a chat, as in 0.3.11). If the request changed meanwhile, the old files go back
  without asking. If Home Assistant did restart, nothing is asked and the health checks run as before.
- **Unchanged:** it never restarts without a fresh tap; every other path. The checks between Approve and restart are
  now one shared step for the normal path and recovery (same checks, same order).

## 0.3.14 — 2026-10-11

The approval link opens the running Deployer, not the old local copy. Ported from draft PR #985 (ChatGPT
Deployer chat, credited) under the owner's 2026-10-11 split ("take whatever makes it faster"); #985 keeps 0.3.13
and renumbers when it composes main. Contains everything in 0.3.12 unchanged.

- **Why:** every approval push said "Tap to open the approval page" with a fixed link to
  `local_house_brain_deployer`. Since the store install (`fa119045_house_brain_deployer`), that link opened the
  stopped legacy copy's panel, not the Deployer that is asking.
- **Change:** the link is built from this App's own container name (`<local|8-hex>-house-brain-deployer` →
  `homeassistant://navigate/<slug>?server=default`). Any other name gives no link, and the push then says
  "Use notification actions or Open Web UI." The native Approve/Reject buttons work as before.
- **Unchanged:** a tap only opens the page and never approves; approval, nonces, unlock and every other behaviour.

## 0.3.12 — 2026-10-10

Request backlog fix (owner pop-up 2026-10-10 "Small hotfix now"; draft PR #985 moves to 0.3.13). Contains
everything in 0.3.11 unchanged.

- **Why:** each check read only the first 10 request folders in `deploy/requests/` (sorted by name), counting
  folders that were already handled. Once 10 old folders were there, every newer request was never read: not
  asked, not held, not reported, and the page said "All clear". Live example 2026-10-10:
  `2026-10-10-ac-insight-1-1-r1` (11th folder) and `2026-10-10-evap-v23-5-r5-tiebreak-next-hour-r1` (13th).
- **Change:** requests not yet handled are read first, then handled ones (still read for the changed-manifest
  check), then the limit of 10 applies. Same fix as the Maintenance App (finding R2).
- **Unchanged:** one change per check, handled ids are never run again, a changed manifest under a used id is
  refused, the daily install limit, every approval.

## 0.3.11 — 2026-10-10

Re-ask after a restart approval timed out (owner pop-ups 2026-10-10 "Build 0.3.10" and "0.3.11 on main"; 0.3.10 is
draft PR #985). Contains everything in 0.3.9 unchanged.

- **Why:** a request whose files were installed but whose **restart** approval expired unanswered ends `ROLLED_BACK`
  (the old files are put back). 0.3.8 only re-asked `TIMED_OUT`, so this case needed a whole new request.
  Live example: `2026-10-10-evap-v23-5-r4-morning-report-r1` (17:05Z).
- **Change:** when the restart approval gets no answer (no tap, or a tap from anyone but the owner) and the files
  are back without a restart, the ledger entry is marked `restart_timed_out`. A re-ask
  (`house_brain_reask_request.v1`) accepts `TIMED_OUT` or that marked `ROLLED_BACK`. It runs the original again
  from the start: a fresh deploy approval, a new backup, then a fresh restart approval.
- **Still final:** a Reject of the deploy or of the restart, a failed config check, an install rolled back after
  Home Assistant restarted anyway (`ROLLED_BACK_RESTARTED`), a failed health check, `FAILED_MANUAL`. All limits are
  unchanged: at most 2 re-asks, no re-ask of a re-ask, counted against the daily install limit, never automatic.

## 0.3.9 — 2026-10-08 candidate (#981; NOT INSTALLED)

- Shared ChatGPT/Claude `house_brain_http_cleanup_request.v1`: approved hash-only inspection and fixed, locally generated removal of the obsolete literal Tailscale HTTP YAML block.
- Confirm stable Network proxy equivalence, original/candidate/UI hashes and migration warning before changes; separate deploy/restart approvals. No generic root YAML, HTTP configure/promote or repair-ignore authority.
- Preserve non-HTTP bytes and file mode; independent exclusive copy backup, conflict-safe atomic replacement and journal rollback. External edits are retained and freeze the Deployer for review.
- Use the running Core's config check for this repair, avoiding a second Core on the Pi. Detailed root check errors stay in Home Assistant. Require unchanged UI fingerprint, exact candidate and repair disappearance after restart.
- Target ARM64 image/AppArmor qualification and installation are pending. Existing package operations, credentials, provider list and re-ask behavior are retained. Deterministic candidate carrier `hbd-0.3.9.tar`; store release remains a separate gate.

## 0.3.8 — 2026-10-08

Re-ask by reference (owner pop-ups 2026-10-08 "A: Deployer re-ask (0.3.8)" and "C: Allow Grok and Gemini").
Contains everything in 0.3.7 unchanged.

- **Ask again after no answer:** a request whose deploy approval expired unanswered (`TIMED_OUT`) can be asked again
  by a new request `house_brain_reask_request.v1` that names it (`reask_of`). The Deployer re-reads the original
  manifest, requires the exact same bytes (SHA-256 recorded the first time) and runs it through the normal checks
  and a fresh approval marked **ASKED AGAIN**. A re-ask never approves anything.
- **Limits:** only `TIMED_OUT` (Reject is final), at most 2 re-asks per request, no re-ask of a re-ask or of a
  read-only lookup, counted by the daily install limit, never automatic. Each refusal names its reason
  (`REASK_UNKNOWN`, `REASK_NOT_TIMED_OUT`, `REASK_LIMIT`, `REASK_OF_REASK`, `REASK_ORIGINAL_CHANGED`, `REASK_LOOKUP`).
- **Requesters:** `grok`, `gemini` and `codex` may file requests, lookups and re-asks (with `claude`, `chatgpt`,
  `owner`).
- The result names the original (`Re-ask of`) and the original's ledger entry lists its re-asks.
- Fallback carrier `hbd-0.3.8.tar` (the store stays the normal way to update).

## 0.3.7 — 2026-10-07

Credential Autopilot R2 (owner pop-ups 2026-10-07). Contains everything in 0.3.6 and 0.3.5 unchanged.

- **Now from the House Brain store**: install and update it like any other App, without Studio Code Server or
  Terminal. The Maintenance App switches the old local Deployer over with one Approve. The carrier
  `hbd-0.3.7.tar` stays as a fallback.
- **Never two Deployers at once:** while another Deployer is active (it wrote the status in the last 15 minutes
  and did not say STOPPED), this one waits in standby and changes nothing. The status now names its instance,
  and a clean stop says STOPPED.
- **Connect to GitHub in 3 taps** (hand-off port 8096, open only during a Connect) and **old token retires itself**
  after 24 hours of GitHub App sign-ins, as in Maintenance 0.6.6.

## 0.3.6 — 2026-10-06

Credential Autopilot (owner decisions 2026-10-06, pop-ups: two GitHub Apps; Connect button with key-file upload
fallback; one update that carries 0.3.5 too). Contains everything in 0.3.5 unchanged (installing 0.3.6 installs 0.3.5's
fixes and the base refresh as well).

- **GitHub sign-in without a hand-made token.** The Deployer page has a new **GitHub connection** page (link at the
  bottom). **Connect to GitHub** creates a private GitHub App "House Brain Deployer Bot" with fixed permissions
  (Contents: Read, Issues: Read and write, Metadata: Read; no webhook, no events) and GitHub hands its key straight to
  this App. The key is kept only in `/data/github_app/key.pem` (0600), never in the options, so other Apps cannot read
  it through the options API (finding F2). The App signs a 9-minute JWT with it and gets one-hour passes limited to
  this repository and those permissions; about 24 a day, at most 30 (then it stops until midnight UTC). After a failed
  sign-in it waits 5 minutes before asking GitHub again (the real reason stays visible). The key never expires.
- **Fallback:** upload the `.pem` file GitHub downloads (Generate a private key) on the same page. **Disconnect**
  deletes the key from this App.
- **New option `github_auth`** (`auto`, default): the GitHub App when connected, else the old `github_token`. If the
  GitHub App cannot sign in, the old token stands in and the reason is shown. `github_app` = never the token;
  `pat` = the old behaviour exactly. `github_token` is now optional.
- **Clock guard:** the Pi has no clock battery; nothing is signed before the clock is past 2026-10-01 (waits for
  network time).
- **New reason codes** `GITHUB_APP_*` with plain-English fixes on the page and in the push; the 401 fix now
  recommends the GitHub App first.
- **Status entity** `sensor.house_brain_deployer_status` carries `github_auth` (mode, which credential is used,
  the old token's expiry DATE from GitHub's header, GitHub App connected/installed, last reason code; never a value).
  The Maintenance App's daily credential check reads it.
- New hash-pinned dependency `cryptography` 50.0.2 (+ `cffi` 2.1.1, `pycparser` 3.0), musllinux aarch64 wheels, for
  RS256 signing (GitHub's own documented example uses it).
- Tests: 53 GitHub App cases (+1 reason-text case) against an independent GitHub mimic (JWT verified with the public key and with openssl,
  cache, budget, clock, uninstall, revoked key, new ~520-character `ghs_` token format, redaction, Connect round trip,
  owner binding, upload, page CSP, failure back-off); 25 new mutants (all killed); L5 aarch64 container suite 7/7.

## 0.3.5 — 2026-10-06

Owner-approved 2026-10-06 (P1–P6 from `DEPLOYER_RECOVERED_AFTER_CRASH_ANALYSIS_2026-10-05.md`, plus the base
refresh). The design rule is unchanged: recovery only rolls back or finishes the health check.

- **P1 — says why an install was interrupted.** Every start records `APP_STARTED` (version, host kernel
  `boot_id`, run id). A stop signal records `STOP_REQUESTED` with the phase. A new transaction remembers the
  run and boot that opened it. A recovery result adds "interruption: ... during <PHASE>", naming one of:
  - the App was stopped (App update, options save or restart);
  - the App ended without a stop signal (killed or crashed);
  - the host rebooted;
  - an internal error <Type>;
  - unknown, for a journal written by an older version.
- **P2 — internal errors are recorded and one is closed.** An exception that ends a poll while an install is
  open is recorded as `TXN_EXCEPTION` (type, phase). `http.client.HTTPException` (`IncompleteRead`,
  `LineTooLong`, `BadStatusLine`, ...) is now a network error, so waiting for Home Assistant to come back
  after a restart no longer ends the poll.
- **P3 — a stop is clean.** The approval wait, the precondition wait and the health check (waiting for
  RUNNING and the settle loop) notice a stop request. They leave the journal exactly as it is (nothing is
  undone or approved), and the App exits within seconds instead of being killed after 30 s.
- **P4 — warning while an install is open.** The status entity (state DEPLOYING) carries a `warning`
  attribute, and the approval page shows "Install in progress": updating, restarting or reconfiguring the
  Deployer now rolls the install back.
- **P5 — recovery facts in the #561 result:** `recovery` = phase, cause, journal age in seconds, whether Home
  Assistant had restarted (marker gone), when the stop was requested, and when this App run started.
- **P6:** L1/L3/L4 tests for each case and 23 new mutants (all killed).
- **Base refresh (D8/H4, due 2026-10-31):** `base-python:3.14-alpine3.24-2026.08.0` (digest-pinned, the same
  base as the other House Brain Apps), was `2026.06.1`.

## 0.3.4 — 2026-10-01

Owner-approved (options A + C, upper limit 12 h): a deploy request can wait hours for the owner's tap.

- **Longer hold for the deploy approval.**
  - `approval_timeout_minutes` now allows 2–720 (was 2–60). The default is 360 (6 h, was 15).
  - Read-only lookups use the same option.
  - An option you have already saved is kept. To get 6 h, set 360 in the App's Configuration.
  - `restart_approval_timeout_minutes` is **unchanged** (default 10, max 30). The new files are on disk while it waits, so that window stays short.
- **A lost connection no longer ends the wait.** This covers a Core restart, an update or a network blip after the push was sent.
  - The approval page keeps deciding.
  - Every 30 s the Deployer tries to re-subscribe, which brings the push buttons back.
  - The loss is recorded in the audit `detail`. Without the page, a lost connection still fails closed.
- **The page countdown reads in hours**, for example "Expires in 5 h 59 min".
- **Unchanged:**
  - single-use code and owner-only checks;
  - no answer = Reject;
  - after approval, the re-checks that the files are unchanged, preconditions hold and the strict config check passes;
  - one request at a time (others wait until this one is decided or expires).

## 0.3.3 — 2026-09-29

Owner-approved.

- **Undo a recent install from the Deployer page.** Every successful install is recorded (files, hashes before and after, backup names, safety checks).
  - The page lists up to 5 recent installs with an **Undo this install** button.
  - An install is offered only if it is the newest install of each of its files, and every file is still exactly as it left it.
  - The owner's tap is the deploy approval: Supervisor ingress peer, owner login, single-use code, offered id only. The restart still asks.
  - The reverse install uses the hash-verified `.bak` copies on disk and runs the normal engine: partial backup, journaled no-clobber steps, strict config check, health, automatic restore. Crash recovery rebuilds the undo from its record.
  - The undone version is kept as `<file>.undone_<tag>.bak`. A created file is renamed, never deleted.
  - Undo never counts against the daily limit, and nothing an AI files can start one.
- **Optional `summary`** in deploy requests (≤ 160 characters, sanitized). Shown in the approval as `AI summary: "…"`.

## 0.3.2 — 2026-09-29

Owner-approved (options 1 and 3) after a correction day ran out of approvals.

- **The daily limit counts installs, not prompts.**
  - The restart approval of an install already approved does not count; a rollback-restart approval does not either.
  - Read-only lookups have their own, separate allowance of the same size. Flood protection stays.
  - `max_approval_requests_per_day` keeps its name and range (1–12).
- **"Allow more today".** When a request is held by the daily limit, the Deployer page shows a button that lifts the limit until local midnight.
  - Same protections as an approval: Supervisor ingress peer only, the owner's login only, and a single-use code.
  - The lift is recorded in the audit log (`LIMIT_LIFTED`) and survives App restarts.
  - Every change still needs the owner's deploy and restart approvals.
- **Wording:**
  - An expired approval now says what actually happened: deploy → nothing changed; restart → files put back; lookup → nothing read or changed.
  - The limit message no longer suggests raising a limit that is already at 12.
  - The status entity reports `installs_asked_last_24h`, `lookups_asked_last_24h` and `limit_lifted_until`.

## 0.3.1 — 2026-09-29

Owner request after a request sat silently behind the daily approval limit: always say why.

- Every hold or failure now has a plain-English reason and a "what to do". This covers:
  - daily approval limit, including when it frees up;
  - safety freeze, and how to clear it;
  - recovery of an interrupted install;
  - a changed request id;
  - an unreadable manifest;
  - practice mode;
  - GitHub problems: token rejected/expired, missing access, branch or repository not found, GitHub or
    API rate limit, DNS/internet/TLS/timeout, GitHub outage;
  - Home Assistant problems: owner login not found, notify service missing, access denied, Core
    unreachable;
  - approval pushes that could not be delivered;
  - approvals that expired.
- Where the reasons appear:
  - the Deployer page (**"Why nothing is happening"**, last check time, last result, or "All clear");
  - `sensor.house_brain_deployer_status` (`OK`/`HELD`/`ERROR` with `reasons`, `last_check`,
    `next_check`, approvals used / limit, `last_result`);
  - the log, once per change, plus a `settings:` summary at start-up.
- A held request is announced once by push and once on its tracking issue.
- An error lasting ~15 minutes is pushed once per episode.
- Result reports lead with the explanation (e.g. "the approval request could not be sent to the phone:
  notify.… failed").
- No decision, gate, endpoint or permission changed. The status entity is still never written while
  an install is pending.

## 0.3.0 — 2026-09-28

- Owner-approved **read-only lookups** (`house_brain_lookup_request.v1`): an AI files a Home Assistant
  template; the owner sees the exact template and approves; the Deployer renders it through Core's
  read-only `POST /api/template` (the only new allowlisted endpoint) and posts the scrubbed output
  (secret-shaped strings redacted, 20 000-character cap) to the tracking issue. Works in dry-run mode
  too. No writes, backups, restarts or service calls. Shares the daily approval cap.

## 0.2.1 — 2026-09-28

- Tapping the push now uses the Companion deep link `homeassistant://navigate/local_house_brain_deployer?server=default`
  (0.2.0's relative path opened Safari at the internal URL on iOS).
- Result facts: Supervisor version read from the `/info` `supervisor` key (was null); successful
  deployments also report `post_sha256` of every touched file.

## 0.2.0 — 2026-09-28

- Tap-to-open approval page (owner request after two accidental taps in the 0.1.0 practice run):
  tapping the push opens the Deployer's page inside the Home Assistant app with large Approve /
  Reject buttons. Opening the page never approves. Served only through Home Assistant ingress
  (Supervisor proxy 172.30.32.2, admin-only panel); the decision must come from the owner's
  Supervisor-supplied user id with the live single-use code. Press-and-hold buttons still work.

## 0.1.0 — 2026-09-28

- First candidate. Pull-based requests from `deploy/requests`; allowlisted `/config/packages/<slug>.yaml`
  targets; blob or SHA-enforced recipe sources; partial backup; strict Supervisor + Core config check;
  separate phone-unlock approvals for deploy and restart; journaled no-clobber file steps with
  automatic rollback; health check (versions, unchanged entities, log scan); ledger, audit log,
  status sensor and GitHub issue comments; dry-run default; persisted freeze on FAILED_MANUAL;
  restart-marker detection of Core restarts during a transaction (owner asked to finish rollback);
  starts on boot so crash recovery runs after a power loss.
