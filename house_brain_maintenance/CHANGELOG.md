# Changelog

## 0.9.36

A firmware device that is not back is watched, not a reason to pause the App (owner 2026-10-26: the Tesla car's
software update 2026.38.5.11, approved 16:08Z, took the car offline for its install; after the hour-long wait the
App called it "not back", ended `FAILED_MANUAL` and paused itself at 17:08Z, so no problem watch, no update
checks and no request ran until the owner would type the request id into `clear_freeze_for`; pop-up "Build
0.9.36"; Work Board #942 claim 6424338706). Contains 0.9.35 unchanged otherwise.

- After a firmware install, a device that has not reported back (or is back but not healthy) is put on a
  **24-hour watch** (`firmware_watch` doc): the result is `FAILED` with "this App keeps checking the device for
  24 h", nothing is paused, and every poll looks again. Back on the new version → `DONE` (counts as a success);
  back on another version → `FAILED`, that version not offered again; never back → `FAILED_MANUAL` and the pause,
  as before.
- A pause left by a firmware install (`fw-…`) clears by itself once that device is healthy again (its update
  entity found, no entity of the device unavailable): audit `FREEZE_CLEARED_DEVICE_BACK` and one line
  "### House Brain Maintenance: pause cleared" on the tracking issue. Any other pause still needs the owner's
  `clear_freeze_for`.
- Tests: `test_l4_firmware_watch.py` (watched not paused; back on the new version; back on the old version; never
  back; a stale pause clears; a pause stays while the device is away or unknown); the mimic's `firmware_away`
  (the install takes the device's entities offline and leaves the version); mutants `dw-watch-pauses-at-once`,
  `dw-watch-never-ends`, `dw-old-version-counts-as-done`, `dw-thaw-ignores-health`, `dw-thaw-any-firmware-pause`,
  `dw-watch-not-polled`.

## 0.9.35

Missed requests you can still answer (owner 2026-10-26: "you've got to figure out a way how to see what I'm saying,
because I'm missing a lot of these, not on purpose"; pop-up "Build 0.9.35"; Work Board #942 claim 6420180691). A
chat-filed request (`ADDON_SETTINGS`, `ENERGY_AI_SETTINGS`, every job that asks) used to ask on the phone only and
count a 15-minute silence as Reject, and the Maintenance page then said "cannot be resumed here". Contains 0.9.34
unchanged otherwise.

- A missed request ask (stage `JOB`) is listed under **Needs your decision** with Approve / Decline, like a missed
  update; the owner's page decision re-runs that request on the next poll (`PAGE_RERUN`), its ask is answered from
  the page (no push, not counted against the daily ask limit), and the result is posted as usual. Only a request
  the ledger holds as `REJECTED` and whose manifest is still on the request branch runs again; a Decline keeps it
  rejected. A timeout still ends the run as Reject (unchanged); the page decision is a fresh owner decision.
- The "Missed ask, open the Maintenance page" phone note now goes out for a missed request too.
- Every decision made on the page is one line on the tracking issue ("### House Brain Maintenance: page decision",
  stage, request id, the ask's title, Approved/Declined), so the chats see what was tapped. Never the ask's text.
- Tests: `test_l4_missed_requests.py` (listed and resumable with the note; approve re-runs without a push or the ask
  limit and posts the line; decline stays rejected; a DONE request never re-runs and a stale decision is dropped; a
  re-run skips the limit but a new request still waits); `test_l1_inbox.py` updated; mutants `pm-job-not-resumable`,
  `pm-rerun-done-too`, `pm-rerun-without-decision`, `pm-rerun-counts-against-limit`, `pm-missed-note-suppressed`,
  `pm-page-line-dropped`.

## 0.9.34

Two settings of the Google Drive Backup App, changed for you on one Approve (owner pop-ups 2026-10-26 "Build 0.9.34",
"06:45 Arizona", "Both"; Maintenance deep dive R2, Work Board #942 claim 6407699448). Two backup makers ran at the
same minute (Home Assistant's own automatic backup, which runs at a random moment inside about an hour of its set
time, and the Google Drive Backup App at a fixed minute): they blocked each other ("system is not running - freeze"),
the App's copies left out an App installed later, and the owner chose to keep both makers and stagger them rather
than switch to one. Contains 0.9.33 unchanged otherwise.

- New request `ADDON_SETTINGS` (`addon: google_drive_backup`; `backup_time_of_day` HH:MM and/or `include_all_addons:
  true`; or `undo: true`): reads that App's settings through the Supervisor, shows the before and after of the two
  values on the phone, and on Approve writes them back with every other option byte-identical, restarts the App
  once (the Supervisor applies options at the next start) and reads the values back. The two replaced values are
  kept for `undo`. Its password is never read into a message, never set and never shown; the owner sets that in the
  App's own screen.
- The backup problems "Home Assistant's last automatic backup did not finish" and "Your newest full backup misses
  … App(s)" get a **Fix it** when that App is installed: the same change with the owner's chosen defaults (06:45,
  every App included), through the usual Approve.
- Guard: a new Drive settings target in `hbm/ha.py` (that App's slug only; `GET info`, `POST options` with the names
  just read and only the two settings changed, `POST restart`; never nested, never another App).
- Tests: `test_l1_drive_settings.py` (manifest, guard matrix, description), `test_l4_drive_settings.py` (approve,
  undo, reject, already set, not installed, time only, a write the App does not take, Fix it, wrong slug); mutants
  `ds-slug-any-store`, `ds-guard-off`, `ds-names-free`, `ds-other-values-change`, `ds-values-unchecked`,
  `ds-target-nests`, `ds-no-approval`, `ds-undo-not-kept`, `ds-fix-through-fix-target`.

## 0.9.33

The App reads its own free-lane posts (live finding 2026-10-25 19:03Z, the first Sunday list: the lane's lockout
review of the Rest problem, posted 06:07Z, showed as "AI review: not posted yet"; Work Board #942 claim 6402027801).
The problem-review reader accepted comments from the owner's login only, and the lane posts under the GitHub App's
own `<slug>[bot]` login, so a lockout post satisfied nothing: the problem stayed in the queue and the page said
"not posted yet", while the lane (rightly) never posted it again. Contains 0.9.32 unchanged otherwise.

- `problems.parse` accepts this App's own bot login (`problems.bot_login_of(auth)`, the connected App's slug + `[bot]`)
  **only** for a comment that carries the `house_brain_free_review.v1` marker; every other author but the owner stays
  ignored, and with a personal token (no App identity) nothing changes. The owner's reviews keep precedence as before
  (newest line wins; a cloud review always replaces a free one for the same problem).
- The reader re-reads the last 3 days of the tracking issue once when its acceptance rules change (`aireview.RULES`),
  so a lane post made before this update counts as soon as 0.9.33 runs; the newest review of a problem still wins.
- Tests: the mimic's mirror of the App's posts now carries the bot login, as GitHub does; `parse` unit tests for the
  three authors; the one-time re-read; `bot_login_of`; mutants `fr-own-posts-ignored`, `fr-bot-without-marker`,
  `fr-no-reread-after-rules`.

## 0.9.32

The free lane's status where every AI can read it (owner 2026-10-25 06:0xZ: "don't you have read access? … figure out
how you can make that happen"; Work Board #942 claim 6394191603). The read-only perimeter exports a fixed attribute
list for the status sensor, so `free_reviewer` was invisible to the AI chats. Contains 0.9.31 unchanged otherwise.

- Every post on the tracking issue carries `<!-- house_brain_free_reviewer.v1 {…} -->` right after the needs-review
  stamp: mode, calls and Neurons today, drafts (and their verdicts), posted, agreement, `last_reason`, `last_at`,
  the cloud reviewer's last review time, and `last_shape` (the Companion's answer shape from a refusal: sizes, JSON
  found, key names, finish reason, tokens, JSON mode). Never any problem or model text.
- `freelane.py` keeps `last_shape` and `last_at`; Companion 0.1.6 puts the shape in its `REVIEW_UNPARSED` body.

## 0.9.31

Lockout clock fix (owner pop-up 2026-10-25 "Build 0.9.31 now"; Work Board #942 claim 6391607736; 0.9.30 is the AI
family's). Contains 0.9.30 unchanged otherwise.

- `problems.py` `cloud_at()`: when the clock field was never written (the first reads after an update see no new
  comment), it is seeded from the newest stored cloud review, so a cloud reviewer already silent at update time still
  counts as silent and lockout posting can begin. Mutant `fr-clock-not-seeded`.
## 0.9.30

Systems AI relay (AI family plan step 4; owner pop-ups 2026-10-20 "Maintenance relays it" and "Maintenance pulls
/proposals"; Work Board #942). Contains 0.9.27 unchanged otherwise. Built as 0.9.25, renumbered by owner pop-up
2026-10-24 "Mine becomes 0.9.30" after 0.9.26 merged first; 0.9.27 is the Maintenance chat's history measurement.

- **Family status sensor** `sensor.house_brain_family_status`: Systems AI's `/digest`, read on the internal
  network (its own store hostname only, no token, 16 KiB at most), written when the word changes or every 30
  minutes at most. Not reachable: `UNKNOWN`.
- **Systems AI's suggestions** (`/proposals`: restart or start a House Brain App) appear as normal problems with the
  Fix button; your tap approves, as before. One owner per problem; never a manual-start App, this App, the Scout
  or Systems AI itself. Unreadable suggestions stay as they were (never a false clear).
- Nothing happens when Systems AI is not installed.

## 0.9.29

Free reviewer lane (reviewer session message 2026-10-24 14:30Z; owner pop-ups 2026-10-24 "Go straight to lockout mode",
"Companion review route", "Its Broker key"; design `docs/architecture/HOUSE_BRAIN_FREE_REVIEWER_LANE_R1.md`; Work Board
#942 claim 6389220976). 0.9.28 is the AI family's Systems AI relay. Contains 0.9.27 unchanged otherwise.

- `hbm/freelane.py` (new): low-risk open log problems without a cloud review are drafted by the Companion's review route
  (Gemma 4 26B, Workers AI free plan; the App's Broker rotation key is its identity): WATCH, IGNORE or ESCALATE. Drafts are
  compared with the cloud review when it arrives (agreement per kind). In lockout mode (24 h without any cloud review) the
  WATCH/IGNORE drafts are posted as review lines marked "free model", repeats of earlier cloud verdicts for unchanged problems
  without a model call; a later cloud review wins; posting stops when agreement drops under 60 %. Daily count: ≤ 3,000
  Neurons, ≤ 60 calls, ≤ 4 posts.
- `problems.py`: free-model review lines carry `house_brain_free_review.v1`; `cloud()`, `cloud_before()`, `cloud_at()`; the
  reviewer watchdog counts cloud reviews only; the Messages page and the weekly list label free-model reviews.
- `aireview.py`: `cloud_at` (when the cloud reviewer last posted). `watch.py`: open records keep up to four detail lines.
- Options `free_reviewer` (off | shadow | lockout, default lockout) and `companion_url` ("" = derived; `off`).
- Status sensor: `free_reviewer` (mode, Neurons and calls today, drafts, posted, agreement).

## 0.9.27

History measurement (history archive proposal R1 step 1; owner pop-ups 2026-10-24 "Yes, measure first" and "Yes, 0.9.27
next"; request #454 6385485785; Work Board #942 claim 6385912228). Contains 0.9.26 unchanged otherwise.

- **New read-only job `HISTORY_MEASURE`** (no approval): one complete UTC day of history (every row, with attributes)
  and its 5-minute and hourly statistics, read chunk by chunk through Home Assistant's own APIs, counted, compressed
  with `xz` in memory and dropped. Reported: rows, entities, raw JSON bytes, compressed bytes (raw and in the proposed
  archive row shape), the 10 largest entities, statistics rows and bytes. Nothing is kept, written or changed.
- A chunk too large for Home Assistant is split down to one entity; an entity unreadable even alone is named. A time
  cap (default 30 minutes) ends the read early with a partial result that says so.
- The socket allowlist gains the full-day history shape and the two statistics reads, each in a closed shape.

## 0.9.26

Free checks (reviewer plan step 1; owner pop-ups 2026-10-20 "Build 0.9.24" and "Build as 0.9.26 now"; Work Board
#942 claim 6302610201). Contains 0.9.24 unchanged otherwise (0.9.25 is another chat's change).

- **Why:** the owner's weekly Claude limit ran out; most of it went to hourly reviewer runs that found nothing. The
  reviewer now runs a few times a day (once a day while the limit is used up), and this App does the plain checks
  itself, for free.
- **Its own after-update result:** about a day after an update it installed, this App posts clean / problems /
  not confirmed, with the same machine line the AI reviewer used (`house_brain_ai_after_update.v1`).
- **Needs-review count** on every post (`house_brain_needs_review.v1`, right after the heading) and on the status
  sensor (`needs_review`).
- **Reviewer watchdog:** 26 hours instead of 6.
- **Large database:** "(… GB; not growing)" once growth is 5 MB a day or less over a week (with hysteresis), and a
  plain explanation that the file shrinks only after old rows age out and the monthly repack.

## 0.9.24

Energy AI settings, delivered once with your approval (owner pop-ups 2026-10-20 "Yes, start install path", "Yes,
build 0.9.24" and "I approve the exception"; Work Board #942 claim 6283003936). Contains 0.9.23 unchanged otherwise.

- **New request `ENERGY_AI_SETTINGS`** (`READBACK` or `ACTIVATE`). It works only with the store House Brain Energy
  AI App, at the version the request names, while it is stopped. Your phone shows the App, the version and the
  settings' fingerprint; Approve needs Face ID.
- **What it does after Approve:** switches Energy AI's `setup_only` on, starts it, sends the one settings message
  (it expires after 5 minutes), reads Energy AI's one-line receipt from its log and checks it against what was
  sent, then switches `setup_only` off again whatever happened. Energy AI starts no runtime and stops by itself.
- **The settings file** sits next to the request on the private request branch, pinned by its SHA-256, and must
  come from a commit on `main`. Its contents are never logged, reported or kept.
- **The one stdin exception:** sending this one message is the only stdin call this App can make, only to that one
  App, only inside the approved job and only with the exact bytes it pinned. Stop, restart, update, uninstall and
  every other App's stdin stay refused.
- **A restart mid-delivery** switches `setup_only` off on the next check and reports the request as failed.

## 0.9.23

Messages that solve (owner pop-up 2026-10-19 "Build 0.9.23", from the reviewer chat; Work Board #942 claim
6270906867). Contains 0.9.22 unchanged otherwise.

- **Owner, 2026-10-19:** "Some of the updates tell me to go back to the messages; I need links to those messages, or
  better yet the House Brain messaging page actually provides the actual solution, or has a button where I can click
  and the AI actually fixes the issue itself."
- **The AI review on each problem:** the verdict in words, its summary, the AI's numbered steps (the reviewer's new
  `house_brain_ai_problem_steps.v1` line) and a "Full AI review" link.
- **Fix it:** a problem with a built-in fix has a "Fix it" button. It uses the same confirm screen as "Needs your
  decision"; your tap is the approval, and the result ("Fixed" / "The fix did not help") shows on the message. A fix
  you approved on the page now runs before other fixes that wait.
- **Links:** a failed job, the morning summary and the Sunday list link to their GitHub report; summaries link each
  problem they name on the page; key-change notes link the tracking issue.
- **Busy sensors named:** long entity ids in the large-database details are shown (digit runs of 6 or more as `[n]`).

## 0.9.22

Apps with a hyphen in their slug, and checks that keep running (owner pop-up 2026-10-19 "Fix + keep checks running";
Work Board #942 claim 6261400467). Contains 0.9.21 unchanged otherwise.

- **Live 2026-10-19:** after the update to 0.9.21 nothing was posted. The status sensor showed `ERROR`
  "ForbiddenCall: GET /addons/5c5…" on every check. An App with a hyphen in its slug (read since 0.9.15) had an
  update waiting, but this App's own call list still refused to read it. The update review failed, and that stopped
  the whole check: problems, messages, summaries and the first "problem details" post.
- **Now:** hyphen slugs are read, reviewed, held and requested like any other (the call list, AI reviews, the hold
  list and request files). If the update check fails, the problem check still runs, and after two failed checks in a
  row you get the problem "Maintenance cannot check for updates" (error type only). It clears once the update check
  works again.

## 0.9.21

Problem details reach the AI reviewers (owner pop-up 2026-10-18 "Build 0.9.21", from the reviewer chat; Work Board
#942 claim 6256752715). Contains 0.9.20 unchanged otherwise.

- **Reviewer request:** the reviewer could not see which sensors make the database large. The suggested steps (the
  busiest sensors by entity id and rows a day) went to the tracking issue only in the problem's first report, and
  the "waiting for AI review" post named only titles.
- **Now:** a "problem details" post (at most once a day) gives each open problem's suggested steps when they are
  new or changed, and the waiting-for-review post carries the same lines. Sensor values are never posted. Only
  while AI problem reviews are on.

## 0.9.20

Deployer safety copies are not your full backups (owner pop-up 2026-10-18 "Build 0.9.20", from the reviewer chat;
Work Board #942 claim 6254848461). Contains 0.9.19 unchanged otherwise.

- **Live 2026-10-18:** "Backups are not password-protected" cleared, then came back after each Deployer install
  (16:18Z and 17:11Z). Before every install the Deployer makes a quick safety copy ("HBD <request> pre-deploy":
  Home Assistant settings only, no database, no password). Because it includes Home Assistant, it was counted as
  your newest full backup.
- **Now:** that copy counts as a partial backup. It no longer decides "not password-protected", "misses running
  Apps" or "no recent full backup". A daily Deployer copy also can no longer hide a missing real full backup.
  Only the exact Deployer name counts, and only for a partial backup. The Deployer is unchanged.

## 0.9.19

AI problem reviews (owner pop-up 2026-10-18 "Build both as 0.9.19", from the reviewer chat's audit of #454; Work Board
#942 claim 6252353111). Contains 0.9.18 unchanged otherwise.

- **Stale review reuse.** On 2026-10-18 the morning summary showed "Your newest full backup misses ... ESPHome Device
  Builder" next to the 2026-10-13 review of the same problem key about a different App. A review now counts only
  for what the problem said when the review was posted. Each problem keeps a fingerprint of its text with numbers
  masked, so "ends in 12 days" and "ends in 11 days" stay the same problem. When it changes, the problem goes back
  into the review queue, and the reviewer watchdog counts from that new post.
- **Late queueing.** Assist Microphone (open 12 days) and the large database (7 days) first reached the review queue
  in the Sunday list. Now every alerted open problem without a counting review is named in a short "problems
  waiting for AI review" post on the tracking issue, at most once an hour, the first time it is reported or
  changes.
- After updating, problems that were already open are checked once more by the AI reviewer.

## 0.9.18

Your messages (owner 2026-10-18: "I got something but I clicked it and it disappeared"; owner pop-ups "Messages
list on Maintenance page", "Home Assistant bell", "'Handled' button on the push", "Quiet hours", "Repeat unhandled
critical", "Sunday line: open messages"; Work Board #942 claim 6251789151). Contains 0.9.17 unchanged otherwise.

- **Every message says who does what**: what happened, what was already done, what you need to do (or nothing),
  and what happens if you do nothing. AI activity messages name the AI, this week's counts and what was already
  blocked. With practice mode on, a fix is never claimed as offered.
- **Kept and viewable**: every push is kept for 90 days (at most 500) in a new Messages list on the Maintenance page.
  A tap on any push opens it. Warnings that only go into the morning summary are kept too.
- **The bell**: each open problem is also a Home Assistant notification, removed when it clears or is handled
  (at most 20).
- **No more overwriting**: each push has its own tag, grouped on the phone by area.
- **Handled**: a button on problem pushes and on the page. It only marks the message, never switches anything.
- **Quiet hours** (`quiet_hours`, default `22-7`): warnings wait for one morning push; critical always comes through.
- **Repeats**: an unhandled critical problem is pushed again every 4 hours, at most 3 times.
- **Sunday**: the weekly still-open push says how many messages are open.

## 0.9.17

Weekly AI activity (owner pop-up 2026-10-18 "Build all 4 parts (Recommended)", Work Board #942 row 72; renumbered
from 0.9.15 and 0.9.16 (owner pop-up "Mine becomes 0.9.17"), which the installed-App list fix and the AI review
pickup took). Contains 0.9.16 unchanged otherwise.

- **Source.** AI Access 0.1.6-dev publishes `sensor.house_brain_ai_activity` from AI perimeter 0.4.5: per AI
  (Claude, ChatGPT, Grok) the last 7 days' requests, answered, refused, rate-limited, tool calls and items, plus
  flags. Counts only. This App reads it like the connector sensor (one more allowlisted GET, projected: only the
  integer counts, the known flags and the window are kept).
- **Problems.** Each flag is one problem `ai_activity:<ai>:<flag>` (a warning, pushed once, in the morning summary):
  - `MANY_REFUSED`: many requests were refused this week (the kill switch, the owner stop or a limit).
  - `RATE_LIMITED`: the per-minute read limit was hit.
  - `NEW_AI`: an AI read the house for the first time.
  Each says how to stop AI reads with the AI kill switch tile in Home Assistant (no link: the owner page's address
  names the Cloudflare account, which a store copy never contains). When the sensor is unreadable or `UNKNOWN`, open
  problems are kept (never a false clear).
- **Sunday line.** The weekly health message (GitHub and phone) adds "AI this week: Claude N requests (...)".

## 0.9.16

AI reviews show up sooner (owner pop-up 2026-10-18 "All of the above"; Work Board #942 claim 6246316716). Contains
0.9.15 unchanged otherwise.

- A posted AI update review used to be read only at the hourly update check, so the page could say "AI review:
  not posted yet" for up to an hour after it was posted (live 2026-10-17: posted 14:29 UTC, still "not posted yet"
  at 15:28). Reviews are now read on every poll, at most every 10 minutes (about 6 GitHub reads an hour, $0), and
  the page shows them within about 10 minutes. Asking on the phone and installing still happen at the update check,
  as before.

## 0.9.15

Installed-App list fix (live 2026-10-18; Work Board #942 claim 6245993918). Contains 0.9.14 unchanged otherwise.

- **What broke.** From 02:18 UTC every job failed "preflight: APPS_SHAPE" (#454 6244651291): one App in the
  Supervisor's list had a name (slug) this App did not accept, and that one row failed the whole list. Update
  discovery, the problem watch, staging and key changes could not see any App.
- **Now.** Slugs with a hyphen are read, as the Supervisor allows them. Any other row this App cannot read is
  skipped and counted; every other App is watched and updated as usual. While a row is skipped, one problem says
  how many ("could not be read, so they are not watched or updated") and the status sensor shows `apps_skipped`
  by reason. The App's name is never shown. "APPS_SHAPE" now says why when the whole list is missing.

## 0.9.14

Automatic key changes: two fixes (owner pop-up 2026-10-17 "Build 0.9.13", renumbered 0.9.14; Work Board #942
claim 6235517779). Contains 0.9.13 unchanged otherwise. The "Suspected leak?" one-tap change of the house keys
runs through the same path, so it gets the same retry.

- **This App's own rotation key.** Live 2026-10-17 (#454 6232092258) its change ended ROLLED_BACK with "Broker HTTP
  401"; the old key stayed and worked. The Broker keeps its key list in a 30-second cache per instance, and the
  change's proof (signed with the new key) can reach an instance that has not seen the new key yet. A 401 there is
  now retried every few seconds for up to 90 seconds, like the other key changes already wait. Any other answer
  still stops and rolls back at once.
- **An App without a Broker key** (AI Access today) was looked at, and reported "SKIPPED", every night. It is now
  looked at once per key cycle (90 days by default); "Rotate keys now" still reaches it at once.

## 0.9.13

Suspected leak? page (Credential Autopilot follow-up, owner pop-up 2026-10-07 "Both"; Work Board #942 6235508959).
Contains 0.9.12 unchanged otherwise.

- **Suspected leak?** A new link at the bottom of the Maintenance page opens a leak checklist. Nothing on it shows a
  secret.
  - **Step 1, one tap:** "Change the house keys now" changes the Broker key of every App this App already changes by
    itself (Engineering Observer, Engineering Inspector, AI Access, Inventory Scout and this App's rotation key). It
    runs through exactly the `ROTATE_KEYS_NOW` path an AI request uses: one App at a time, each checked and undone by
    itself if it fails, nothing asked on the phone. The result is posted on the report issue (`asked_via: page_leak`).
    Owner only, single-use button, one at a time; a tap the loop has not taken within an hour lapses. With
    `key_rotation` off there is no button, and a tap that arrives anyway is REFUSED (nothing queued).
  - **Step 2, by hand:** every other credential of the house, with what to do, from the repository's credential list
    (`contracts/house_brain_credential_inventory.v1.json`, each entry's `leak_response`). The App reads the list once
    a day through its own GitHub sign-in; if the read fails it keeps the last good list, or links to it on GitHub.
    Only GitHub, the Cloudflare dashboard and claude.ai become links; all list text is escaped.
  - The tap is taken by the poll loop, never written from the page, so the key file has one writer.

## 0.9.12

Reviewer watchdog and after-update tag (owner pop-up 2026-10-14: "Build both"). Contains 0.9.11 unchanged otherwise.

- **Reviewer watchdog.** Live 2026-10-14: two House Brain store App updates waited in the AI review queue and left it
  without any AI review, while the reviewer routines were failing silently. Nothing said so. Now, when any update or
  any problem in the review queue has waited more than 6 hours without an AI review, the App raises one problem:
  "The AI reviewer has not answered for over 6 hours (N waiting)", naming up to 5 of them and how long each waited.
  - An update counts from when it was first offered; a problem from the first post that listed it for review.
  - Like the sign-in steps, it is pushed to your phone once and is in the next morning summary. It is never an
    approval ask, and it clears by itself when nothing waits too long any more.
  - It is never put in the problem review queue itself.
  - Nothing counts with `ai_review` off or in practice mode. When the reviews cannot be read from GitHub, nothing
    new is said and an open one stays (no false alarm, no false clear).
- **After-update tag.** For 24 hours after an App update is installed, a problem first seen in that window about that
  App, an App that needs it, or an integration it provides carries `"after_update": "app:<slug>@<version>"` in its
  entry of the problem review line (`house_brain_problem_queue.v1`). The newest such install wins. The App keeps at
  most 20 installs of the last 24 hours (`recent_installs`), and the tag survives a restart. In the line the `@` is
  written as `\u0040` (the same JSON string), because the report's address scrub would remove `slug@version`.

## 0.9.11

Sign-in expiry heads-up (owner pop-ups 2026-10-13: "Build it", "You, as 0.9.11", "Yes, use #454"; Work Board #942
claims 6150322900 and 6150967793). Contains 0.9.10 unchanged otherwise.

- **AI connectors.** A Claude, ChatGPT or Grok connector's House Brain sign-in ends after 90 days unused (AI
  perimeter 0.4.3). AI Access 0.1.5-dev publishes `sensor.house_brain_ai_connectors` from the perimeter's own record
  (perimeter 0.4.4, #1104). This App reads that one entity (a new pinned read) and warns 14, 7 and 1 day ahead and
  when the sign-in has ended; a refused sign-in (`SIGN_IN_FAILED`) is pushed at once. Claude's alert links to
  https://claude.ai/customize/connectors. The links are fixed in this App, never taken from the entity. Any use of
  the connector keeps it another 90 days.
- **Cloudflare control tokens.** Control 0.1.5 posts its token warnings on #454 at 30, 14, 7 and 1 days with one
  machine line (`house_brain_cloudflare_control_expiry.v1`). The Cloudflare approval relay (0.8.2) now also pushes
  each of those steps to the phone once, time-sensitive from 7 days. Until now they went only to ntfy and #454.

## 0.9.10

No false problem for the kept local Observer / Inspector (Work Board #942 claim 6148747912). Contains 0.9.9 unchanged
otherwise.

- **Fix.** After the store switches (0.9.8, 0.9.9) the local Engineering Observer and Inspector are stopped on purpose
  and kept as the rollback. Supervisor shows them as `error` after that stop, so every check reported "App stopped
  with an error: House Brain Engineering Observer" (morning summary 2026-10-13, #454 6148534927) and the problem
  reviewer queued it. While the store one is in use, the local one is no longer reported. If an undo or a restart
  puts the local one back in use, it is watched again as before.

## 0.9.9

Engineering Inspector from the store, the last step of the store move (owner pop-ups 2026-10-13: "Inspector 0.2.6
adds it", "Only in export mode"; Work Board #942 claim 6140814916). Contains 0.9.8 unchanged otherwise.

- **Store.** The Engineering Inspector is published as 0.2.6, which is 0.2.5 plus the source manifest the store needs.
- **One Approve switches it**, offered only while the local Inspector exports snapshots.
  - Its 3 settings are copied and read back by fingerprint.
  - The local Inspector is stopped (kept) and the store Inspector started.
  - DONE when its log shows `ENTITY_SNAPSHOT_STORED` (up to 15 minutes); otherwise an automatic undo.
  - No boot setting is touched: both Inspectors are manual-only. Journalled with watch-before-undo after a restart.
- **Shared code.** The Observer and Inspector switches now share one tested sequence (`observermove.ObserverMove`
  with small hooks). The Observer's behaviour is unchanged; its 14 tests pass unmodified.
- After the switch, key changes for the Inspector role use the store Inspector, recorded in `inspector_store`.
- **Fix: legacy history staging (`LEGACY_STAGE` stage, #985 M3).** Live 2026-10-13 (`legacy-stage-2026-10-13a`,
  #454 6140987570) the stage failed safely with `BACKUP_COMPONENT`, and nothing was staged. The pinned backup of the
  local Deployer 0.3.6 is about 41 MB, because a locally built App's backup holds its Docker image. The component is
  now capped at 64 MiB (`MAX_BACKUP`, the download cap), not 16 MiB. Only `addon.json` and the history files are kept,
  still capped at 16 MiB, and the strict reader and stage writer are unchanged (the Deployer pins their bytes). Owner
  decision 2026-10-13: "Ask them to include it".

## 0.9.8

Engineering Observer from the store, the second step of the store move (owner pop-ups 2026-10-13: "Observer to the
store", "One Approve does it all"; Work Board #942 claim 6138696542). Contains 0.9.7 unchanged otherwise.

- **Store.** The Engineering Observer is published in the House Brain store as 0.2.5. It is the accepted 0.2.4 with
  only the unused `run.sh` removed, which the store's privacy gate refuses.
- **One Approve switches it.** When the store Observer is installed, this App asks once. On Approve it:
  - copies the 3 settings and reads them back by fingerprint;
  - sets the local Observer to manual start and stops it (kept, never uninstalled);
  - starts the store Observer;
  - waits for `maintenance_publish=accepted` in its log (up to 10 minutes).
  Any failure puts the local Observer back automatically. The switch is journalled: after a restart it finishes
  the check, and undoes only when no report was accepted, because the Broker would refuse the old session after
  accepting the new one.
- **From then on** key changes and health checks use the store Observer, recorded so that it survives a restart.

## 0.9.7

The writer-lock test holds long enough to overlap (Deployer lane request #942 comment 6136601251; owner pop-up
2026-10-13 "Longer hold"). Contains 0.9.6 unchanged otherwise.

- **`WRITER_PROBE` `hold`: 10 minutes** instead of 60 s. In three live E6 rounds the other App's ask arrived minutes
  after the hold started, so 60 s never overlapped. The Deployer makes the same change in 0.3.22.
- The identity is still re-checked every 5 s; `try` is unchanged. The ask now says "keep it 10 minutes".
- **New:** while holding, an App stop or update lets the lock go at once (result `RELEASED_EARLY`), so a restart
  never waits up to 10 minutes.

## 0.9.6

Problem handling: every problem is followed until it is fixed (owner pop-ups 2026-10-13: "Maintenance one-tap",
"Full process"). Contains 0.9.5 unchanged otherwise.

- **One-tap fix for "The unit of sensor.… has changed".** Live 2026-10-13: six temperature sensors raised this
  repair after the Core 2026.10.1 restart. Their long-term statistics carried an old unit label; the sensors report
  °F now. One ask covers all of them: on Approve, each sensor's stored unit label is set to its current unit
  (`recorder/update_statistics_metadata`, Home Assistant's own "Update the unit … without converting" fix), then
  read back. Numbers are not changed. Statistics are **never deleted** (`recorder/clear_statistics` is refused).
  Only sensors that report a unit with a known unit class are included; any other is still reported, with no fix.
- **Nothing goes quiet.** Each open problem keeps its first-seen time.
  - **Escalation:** a problem still open after 7 days gets one push ("still not fixed after a week").
  - **Weekly list:** on Sunday after the summary hour, every problem still open is posted on the tracking issue
    with its age, its AI review and its fix, plus one phone line. Nothing is posted when nothing is open.
- **AI problem reviews.** The morning summary and the weekly list end with a machine line naming the problems
  without a review (`house_brain_problem_queue.v1`). A scheduled AI routine researches them and posts
  `house_brain_ai_problem_review.v1` (`FIX_NOW`, `OWNER_ACTION`, `WATCH` or `IGNORE`, with a summary). Only the
  owner's account counts; the review is shown next to the problem and in the fix ask. It never acts.

## 0.9.5

Deployer history move, Maintenance's steps M2 and M3 (#985 Pi test plan R1; Deployer lane handoff #942 comment
6133340527, item 3; owner pop-up 2026-10-12: "Backup + shared folder"). Contains 0.9.4 unchanged otherwise.

- **`LEGACY_STAGE` request, two owner-approved phases.**
  - `backup` makes and pins a backup of the stopped old Deployer (0.3.6) only, and records its custody.
  - `stage` re-checks that exact backup under the shared writer lock, reads it in memory, and seals its five
    history files as an `hbm.legacy_snapshot.preparation.v1` stage in `/share/house_brain_legacy_stage_v1`, for
    the Deployer's import step.
- **Reader and stage from #975.** The strict reader and stage writer are the inactive #975 preparation, unchanged
  (pinned by hash). Real App backups are PAX tar files; their archive is first narrowed to plain USTAR with only
  `addon.json` and the history files. Links, unknown PAX keys and unsafe names refuse.
- **Never kept:** the old Deployer's settings and token. **Never done:** uninstall, restore, start. AppArmor gains
  read-write on that one stage folder only.

## 0.9.4

The Maintenance half of the shared writer lock's proof (#985 enrollment plan R1 step E6; Deployer handoff #942
comment 6133340527). Contains 0.9.3 unchanged otherwise.

- **Lock identity where an AI can read it.** The App posts its view of the lock on `report_issue` ("shared writer
  lock": state, device, inode, with a machine line), once per change, and puts it on the status sensor
  (`writer_lock`). Before, the identity was only in this App's own log, which this App does not read back.
- **`WRITER_PROBE` request** (owner-approved):
  - `try` takes the lock without waiting and reports `ACQUIRED` (device, inode) or `WRITER_BUSY`, letting it go at
    once;
  - `hold` keeps it 60 s, re-checking its identity.

  It uses only the existing lock (`create=False`) and writes nothing else. The Deployer's half (0.3.21) has the
  same two operations.

## 0.9.3

Inventory Scout from the store, the pilot of moving the local House Brain Apps into the store (owner pop-up
2026-10-12: "Scout first, then others"). Contains 0.9.2 unchanged otherwise.

- **Store.** The Inventory Scout is published in the House Brain store (unchanged source, Scout 0.1.2).
- **Switch.** When the store Scout is installed, your phone asks once: "Use the store Inventory Scout?". On
  Approve, the old Scout's two settings are copied in memory and read back by fingerprint, and the store Scout
  becomes the Scout this App runs. The switch is recorded, so it survives a restart. The old local Scout is kept,
  stopped, and never uninstalled. A failed copy changes nothing. Reject is final. A missed ask can be decided on the
  Maintenance page.
- **Narrow.** Only this store's Scout can become the target. Its settings can be written only during the copy, and
  only the two known settings. Observer and Inspector are unchanged.

## 0.9.2

Who fills the history database (owner pop-up 2026-10-12: "Measure first"). Contains 0.9.1 unchanged otherwise.

- **Why.** The morning summary said the database is 7.5 GB and advised "keep fewer days (default 10)". This house
  keeps 40 days on purpose: the pool-pump analysis needs 14, the learning sensors 7. Cutting days deletes history for
  good. Nothing said which sensors fill the database.
- **What.** While the database is 2 GB or more, once a day the App counts the history rows each sensor wrote in the
  last hour. Home Assistant's `history/history_during_period` is read in chunks of 200 sensors, counts only; state
  values are dropped unread. The large-database item then lists:
  - the 15 busiest sensors;
  - the busiest sensor families (for example `sensor.*_power`).

  Both come with an estimate per day, so an exact exclude list can be prepared for your approval.
- **Advice text.** "Exclude" comes first and deletes nothing. Fewer days is named as a separate decision that deletes
  history, with no default value presented as yours.
- Nothing is changed in Home Assistant.

## 0.9.1

Shared writer lock, the Maintenance half of #985 enrollment plan R1 (step E2). Owner pop-up 2026-10-12: "All of the
above. let's get this done". Contains 0.9.0 unchanged otherwise.

- **Why.** The Deployer and this App both change the Deployer App, so they must never do it at the same time. The
  Deployer created one lock file for both, with your approval (Deployer 0.3.19, 2026-10-12).
- **What.** Every step that changes the Deployer App holds that lock: its update, undo or restore, the local -> store
  switch-over and the old Deployer's removal. If the Deployer is busy, this App changes nothing, says "the Deployer
  is writing right now" and asks again later. Updates of other Apps are unchanged.
- **Never creates the lock.** This App only uses the existing lock (the code is the Deployer's own, byte for byte).
  While no lock exists (a new install), nothing changes.
- **Start log.** One `HBM-NATIVE writer_lock` line names the lock's device and inode, so both Apps can be shown to
  share it (plan step E6).
- **AppArmor:** exactly two new lines (read the folder, lock the one file).

## 0.9.0

Approve from the page (owner pop-ups 2026-10-12: "Yes, build it", scope "All updates"; "It should let me update
everything that goes through the maintenance app that's not approved automatically"). Contains 0.8.9 unchanged
otherwise.

- **Why.** "Ask me again" could take a long time: the App sleeps `poll_seconds` (5 min) between checks, waits behind
  any other open ask (up to 15 min), backups and health watches, the ask counted against the daily limit, and you
  needed to catch a second push.
- **Needs your decision.** The Maintenance page lists every missed ask (updates, OS reboot and roll-back, fixes,
  after-update reload or undo, store move, Broker key steps) and every waiting update not asked yet, with the AI
  review. Approve or Decline there with one confirm step. The App wakes at once and runs it through the same path
  as a phone Approve (backup, health watch, automatic restore), with no new push and without using the daily ask
  limit. Decline equals Reject. An open phone ask for the same item is answered by the page too.
- **Missed push.** "Missed: ..." pushes now open the page ("Tap to approve or decline it on the Maintenance page").
  Missed fixes and other steps get one too.
- **Fix asks** now carry a `Request:` line so a decision names the exact fix.
- **Fix: AI reviews found hours late.** The first read of the tracking issue took only its oldest 100 comments
  (#454 has hundreds), so the Z-Wave JS 1.9.1 review was found about two hours late (live 2026-10-12). By then the
  phone had been asked with "AI review: none", and that ask used the day's limit. Each read now takes every page
  (at most 10 pages, 1,000 comments).
- Trust unchanged: owner user id from Home Assistant, ingress only, POST only, single-use codes,
  `CONTROL_AUTHORITY=NONE`.

## 0.8.9

Update review queue (owner pop-ups 2026-10-12: "Yes, build it", "Keep auto for low-risk"). Contains 0.8.8 unchanged
otherwise.

- **Why.** The owner waited for an AI review of waiting updates that nothing ever asked for. Low-risk App updates
  (Tailscale 0.30.2, Z-Wave JS 1.9.1) waited silently for their night install, and nothing showed any of it.
- **Updates waiting list on GitHub.** Every App, Core, OS, HACS and firmware update, with this App's review, the AI
  review and the exact reason it is (not) asked yet: held, rejected, waiting for the AI review, waiting for its
  night install, firmware waiting days, re-ask time, or the daily ask limit. It is posted when something changes,
  and a phone line goes out when a new update appears.
- **AI review before anything happens.** No phone ask and no automatic install until an AI review is posted on
  the tracking issue. `APPROVE` keeps the usual path, and a low-risk update may still install by itself at night.
  `HOLD` or `DECLINE` never installs by itself: your phone asks, with the AI's recommendation. Without a review
  after 48 hours your phone is asked anyway, and nothing installs by itself. Only reviews from the repository
  owner's account count, and a review never approves anything; your Approve tap still does. New option
  `ai_review` (default on).
- **Fix: update checks could stall.** A queued request waiting for the daily ask limit ended every poll before the
  update check and the problem watch ran. Now it waits while everything else runs.

## 0.8.8

Fixes from the first live 0.8.7 reports (2026-10-12). Contains 0.8.7 unchanged otherwise.

- **Backup report: a false "16 Apps missing".** A newer settings-only backup (Home Assistant without any App,
  0.03 GB) was taken as the newest full backup. That made every running App look missing, and the backup look
  unprotected and not off the Pi. A backup that holds Apps now wins. A settings-only one is used only when no
  backup holds any App.
- **Pi power check: "not checked" on current Pi kernels.** Kernel 6.x no longer has the `get_throttled` file. The
  App now reads under-voltage from the Pi's `rpi_volt` voltage sensor, and keeps a dip seen at any check on record
  for 7 days. The weekly score says why a reading was not checked. Three read-only AppArmor rules were added.
- **Housekeeping report wording:** alerts that never fired are fine.

## 0.8.7

Housekeeping, the last of the four feature releases (owner pop-up 2026-10-11). Contains 0.8.6 unchanged otherwise.

- **Failing automations.** An automation whose newest run ended in an error, and that failed at least twice in its
  stored runs, is a morning-summary problem with Home Assistant's own error text.
- **Missing entities.** An automation that refers to an entity that no longer exists (renamed, or its device
  removed) is named, with the missing entities. Checked once a day.
- **Error floods.** One error repeating 1,000 times a day or more (it fills the log and wears the disk).
- **Database size.** A history database of 2 GB or more, or one growing 100 MB a day, gets advice.
- **Monthly housekeeping report** on GitHub plus one phone line, including automations that are turned on but have
  not run in 90 days (new option `housekeeping`, default on).
- Read-only: nothing is turned off, edited, run or purged.

## 0.8.6

Pi health, the third of the four feature releases (owner pop-up 2026-10-11). Contains 0.8.5 unchanged otherwise.

- **Power and heat.** The Pi's own under-voltage and throttle flags and its CPU temperature (80 °C and up) are
  morning-summary problems. A weak power supply is the classic cause of random restarts and broken SD cards.
- **Disk health.** New read/write errors on the system disk (an early sign of a failing SSD, SD card or USB
  adapter) and the daily write volume.
- **App outliers.** An App at 90 % or more of its memory limit, or 90 % CPU, three reads in a row (read at most
  every 15 minutes).
- **Weekly health score.** Every Sunday after the morning-summary hour: one number out of 100 with last week's for
  the trend, on GitHub plus one phone line (new option `pi_health`, default on).
- **Pi firmware (bootloader) updates in the approval flow** (new option `pi_firmware_updates`, default on). One
  review, then one phone ask that covers the update and the one restart it needs; after the restart the App checks
  the new firmware runs. Never automatic; Reject is final for that version.
- Read-only otherwise: six narrow AppArmor read rules for the Pi's sysfs files. A file that cannot be read is
  "not checked", never a problem.

## 0.8.5

Backups you can trust, the second of the four feature releases (owner pop-up 2026-10-11). Contains 0.8.4 unchanged
otherwise.

- **Complete backups.** The newest full backup must contain every running App, this App included; missing ones
  are named in the morning summary.
- **Radio networks.** The Zigbee/Z-Wave/Matter/Thread Apps must be in a backup from the last 7 days.
- **Automatic backups.** A failed automatic backup and a backup location with an error are reported.
- **Monthly backup report** on GitHub plus one phone line (new option `backup_report`, default on); the first one
  comes right after the update.
- Not checked, and said so: whether the password opens a backup (this App never holds it).

## 0.8.4

Safer upgrades, the first of the four feature releases the owner approved on 2026-10-11 ("All of the above based on
your priority"). Contains 0.8.3 unchanged otherwise.

- **Known issues before a Core update.** The Core review and the phone ask list Home Assistant's official alerts
  for the integrations you run that affect the new version, each with its link.
- **Release notes for your integrations.** The changes in the new version that touch an integration you run, with
  breaking changes first.
- **Configuration check before a HACS integration's restart.** If the check passed before the install and fails
  after it, the old version is put back without a restart, and the new one is not offered again.
- **New option `upgrade_checks`** (default on).
- Information only: nothing is blocked or approved by these checks, and a feed that cannot be read is named as such.
- Pi firmware updates move to 0.8.6 (Pi health).

## 0.8.3

OS updates finished the way Home Assistant does them now, and a cleaner morning summary (owner pop-ups
2026-10-11: "Ask me, then reboot", clean-ups "All of the above"). Contains 0.8.2 unchanged otherwise.

- **OS install and restart are separate (the defect this fixes).** Supervisor 2026.07.0 and later installs an OS
  update into the spare slot and does not restart. Before 0.8.3 this App waited 60 minutes for a restart that never
  came, reported a failure and paused all work.
  - Now, after the install, your phone asks **"Restart to finish the OS update?"**.
  - Approve runs the configuration check, then restarts the Pi.
  - After the restart the App checks the OS version and Home Assistant's health.
- **One-tap rollback.** If Home Assistant is not healthy on the new OS, your phone offers a rollback to the
  previous OS (still in the other slot).
- **Never stuck.** Waiting for your answer never pauses the App, and a Reject keeps the OS installed without
  asking again. A restart nobody asked for (power cut) is checked the same way.
- **New permission, owner-approved.** The App may restart the Pi only to finish an OS update you approved, or to
  roll one back, each after its own Approve. Never a shutdown.
- **Pending-OS warning.** While an installed OS waits for a restart (also one installed from Settings), the
  morning summary says so: on a Raspberry Pi 4 any restart, a power cut too, switches to it.
- **Morning summary fixed** (live 2026-10-09 to 2026-10-11):
  - a problem that came and went is listed once, not twice;
  - nothing is listed as "cleared" while it is still open;
  - a problem that cleared before the summary is marked "(cleared since)".

## 0.8.2

Cloudflare control approvals on your phone (owner pop-up 2026-10-11: the ntfy pushes did not arrive, "link them up
with … the maintenance app"). Contains 0.8.1 unchanged otherwise.

- **New: Cloudflare approvals.**
  - Every poll the App reads the Cloudflare control's results on GitHub #454.
  - A request waiting for your Approve sends one notification with an **Open Approve page** button.
  - When the request ends, one more notification replaces the first.
  - **Safe:** only results posted by your account count, the button is built from the fixed control address and the
    request id, and the App approves nothing itself.
- **New options:** `cf_control_issue` (default `454`, `0` = off) and `cf_control_url` (leave empty).

## 0.8.1

Seven defects found by ChatGPT's Maintenance 0.7.5 line (PR #975), ported into the released App, plus a hold on
removing the old Deployer. Owner pop-ups 2026-10-11: "Yes, that plan" (Claude ports #975's gate-independent fixes;
ChatGPT keeps the qualification gate) and "Keep old Deployer for now". Contains 0.8.0 unchanged otherwise.

- **One-tap install (`STAGE_LOCAL_APP_SOURCE`) keeps its recovery record when the outcome is uncertain.** Before,
  the record was cleared even after an error once the source was already swapped, so Undo evidence was lost.
  - A network error after your Approve is now reported as "interrupted after your Approve; checked again", not
    "nothing changed".
  - While the recovery read fails, nothing new starts.
  - A recovery that needs you is held, not repeated, until you enter its request id in `clear_freeze_for` (the same
    switch that lifts the pause).
- **After-update check.** An integration that is missing, disabled or not loaded yet after an update no longer
  counts as "still runs". The check waits; if it never settles it closes with "could not be read or verified".
- **Update review:**
  - a permission that the update adds or removes is now flagged, not only a changed one;
  - missing permission facts are flagged;
  - a minimum Home Assistant version that cannot be compared (malformed, empty, not text) blocks that one update.
    It used to be treated as "no requirement".
- **Old Deployer kept (new option `keep_old_deployer`, default on).** The stopped local Deployer 0.3.6 is no longer
  proposed for removal 7 days after the switch to the store Deployer. Switch the option off to get the removal ask
  back.

## 0.8.0

Automatic Broker keys (owner pop-ups K1–K4, 2026-10-11: "I want to be involved as little as possible, especially with
things like swapping out keys"; design `docs/architecture/HOUSE_BRAIN_AUTOMATIC_KEY_ROTATION_R1.md`). Needs Broker
0.6.0. Contains everything in 0.7.4 unchanged. (0.7.5 is a separate lane's candidate number; this release is 0.8.0 so
the two never collide.)

- **Rotation key.** At first start the App makes its own rotation key and keeps it in `/data/keys.json` (mode 0600).
  Only its fingerprint is posted on #454 (`KEYS_SETUP`); an AI puts it into the Broker config and you tap Promote once.
- **Automatic key changes.** One App per night inside the automatic-update window, each every `key_rotate_days`
  (default 90):
  - Engineering Observer, Engineering Inspector, AI Access and the Inventory Scout;
  - then this App's own rotation key.

  For each one:
  1. The App makes a new key and the Broker accepts it next to the old one.
  2. The App's Broker-key option is set (every other option stays exactly as it was).
  3. The App is restarted, and only if it was running (the Scout is never restarted).
  4. The App checks the new key works, then the Broker commits. The old key stays accepted for 24 hours (7 days the
     first time).
- **First change asks once.** "Start automatic key changes?" is asked once, in the daytime. After Approve nothing is
  asked again; after Reject it is never asked again and nothing changes.
- **Failure is quiet first.** Any failure before the commit is undone: the old key goes back into the App, which is
  restarted only if it was running, and the Broker drops the new key. The old key keeps working throughout. One failed
  night is retried the next night without a message; the second failure in a row sends one message and appears in the
  morning summary.
- **Old keys still in use.** If something besides the App still uses an old key after a change, you get one message.
- **New request `ROTATE_KEYS_NOW`** (`role`: one role or `all`). For a suspected leak: rotates at once, outside the
  night window, one App after another. Nothing is asked (after the one first question).
- **Status sensor.** It shows each key's age, its last result and when the next change is due.
- **Options:**
  - `key_rotation` (default on);
  - `key_rotate_days` (30 to 365, default 90).
- **New permission (standing, owner K3/K4).** One more Supervisor target, only during a key change and only for those
  four Apps:
  - read that App's options;
  - write them back with the same option names;
  - restart the Observer, AI Access or Inspector right after their key changed (a stopped App is never started).

## 0.7.4

One-tap install of a House Brain App source (owner pop-ups 2026-10-10: "A: Maintenance job (Recommended)",
"Approve design, build it", "Build as 0.7.4 on top"; design `docs/architecture/MAINTENANCE_STAGE_LOCAL_APP_SOURCE_R1.md`).
Contains everything in 0.7.3 unchanged.

- **New job `STAGE_LOCAL_APP_SOURCE`.** An assistant files one request naming a carrier on `main` (path, commit,
  SHA-256), the App (Engineering Inspector, Engineering Observer or Inventory Scout only; built-in list) and the
  versions. The App checks the commit is on `main`, downloads the carrier (at most 1 MiB), checks its SHA-256,
  unpacks it into a private folder and runs the carrier's own `check`. Only when that says the App folder holds
  exactly the old version does it ask your phone, once: "Install Inspector 0.2.4 → 0.2.5 (source from GitHub
  `<commit>`, checked)".
- **On Approve:** the carrier's `stage` (verified backup, atomic swap), App store refresh, then the existing App
  update path (backup of that App only, update, health watch, restore on failure) without a second ask; the App is
  started only if it was running before. Any failure after staging runs the carrier's `undo` and refreshes the store.
- Reject is final for that version; no answer changes nothing. It counts toward `max_approval_requests_per_day`.
- **New permissions:** folders `addons` and `share` (read-write), narrowed by AppArmor to the three App folders and
  `/share/hb91-app-update`; one more Supervisor call, `POST /store/reload`, allowed only during this job.

## 0.7.3

Hold list and after-update check (owner pop-ups 2026-10-08: "Hold list", "After-update check", "Auto-hold .0 + my
list", "AI files a request"). Contains everything in 0.7.2 unchanged (it was built as 0.7.1, PR #988, and renumbered after 0.7.2 merged first).

On 2026-10-08 the owner had chosen to wait for Core 2026.10.1, but the 2026.10.0 prompt came back after its re-ask
time and was approved by mistake. GoveeLife then failed to start, although Core itself passed its health check.

- **Hold list.** A held update is never sent to the phone, on every update path. Every Core `YYYY.M.0` is held
  until a later patch is out (`hold_core_dot_zero`, default on). The AI adds or removes items with
  `HOLD_UPDATE` / `RELEASE_UPDATE` requests (no approval: they only make the App ask less). A held Core no longer
  makes an OS update wait.
- **After-update check.** The integrations running before a Core, OS or App update are compared with those running
  `after_update_minutes` (default 10) after it. Anything that broke gets a one-tap **Reload**, then an **Undo the
  update** offer (Core: Home-Assistant-only restore of the update's own full backup; App: its App backup; the
  undone version goes on the hold list) for `undo_hours` (default 24). OS updates cannot be undone and are
  reported. No new update starts while a check is open, and the problem watch does not ask twice.
- New options `hold_core_dot_zero`, `after_update_minutes`, `undo_hours`. No new Home Assistant or Supervisor
  call: the same config-entry read, entry reload and restore calls as before.

## 0.7.2

Owner-approved (pop-ups 2026-10-08, and 2026-10-10 "Port to 0.7.2 now"; built as 0.6.7, moved onto 0.7.0). Contains
everything in 0.7.0 and earlier unchanged. 0.7.1 is a separate draft (PR #975); whichever merges second carries the
other.

- **See what is waiting.** The status sensor now shows the real number of updates waiting for you
  (`pending_updates`, was always 0) and a short list (`waiting_updates`, up to 10: kind, App slug, version, when it
  was asked, when it asks again, state). No extra Supervisor calls: it is worked out from what the update check
  already read.
- **"Missed" push.** When you did not answer an App, Core or OS update ask in time, one push without buttons says
  when it asks again and that you can tap **Ask me again** on the Maintenance page. Never after Reject, never in
  dry run.
- **Ask me again** on the Maintenance page, next to each waiting update. It approves nothing: your phone asks again
  on the next poll, through exactly the same path and rules as an `ASK_UPDATE_NOW` request (Reject stays final).
  Owner only, through Home Assistant only, one at a time, lapses after one hour, counts against
  `max_approval_requests_per_day`.
- **Fix asks follow `update_reask_hours`.** A fix ask (and the Deployer switch-over ask) you did not answer is asked
  again after `update_reask_hours` (default 6) instead of a fixed 24 hours. Reject stays final.
- **More requesters.** Requests may now name `grok`, `gemini` or `codex` as `requested_by` (same rules as `claude`
  and `chatgpt`).

## 0.7.0

HACS and device-firmware updates (owner pop-ups 2026-10-08: "Yes, design it", "Approve design", "Yes, but always
ask"). Contains everything in 0.6.6 unchanged.

- **HACS updates:** cards and themes (display only) follow the App rules, so with `auto_low_risk` a low-risk
  bug-fix update installs by itself at night after the waiting period. HACS integrations always ask; one Approve
  covers the restart. A configuration backup (no database) comes first; a failed check reinstalls the previous
  version automatically and never offers the failed one again.
- **Device firmware:** always asks, never automatic, and only after a version has been offered for 30 days
  (`firmware_wait_days`). Firmware cannot be rolled back, so a device that does not come back is reported and the
  App pauses.
- New options `entity_updates` (default on), `firmware_wait_days` (default 30) and `update_hold` (default empty).
- Allowlist: read-only update-entity, HACS-list and release-note reads; one install call, one configuration
  backup and (integrations only) one Core restart per approved item, bodies compared whole.

## 0.6.6

Credential Autopilot R2 (owner pop-ups 2026-10-07: "take it off my plate"). Contains everything in 0.6.5, 0.6.4
and 0.6.3 unchanged.

- **Connect to GitHub in 3 taps, also on the iPhone:** Connect → Create GitHub App → Install (this repository is
  already selected). The 0.6.3 Connect form could not open GitHub from inside Home Assistant's frame; Connect now
  opens this App's own hand-off address on port 8097. That address exists only while a Connect is pending (30
  minutes for an untapped link, at most one hour after tapping) and answers only a valid one-time code (owner
  pop-up "Allow the temporary port"). The key-file upload stays as a fallback.
- **Old token retires itself:** after 24 hours in which the GitHub App signed every call, this App revokes its
  old hand-made token on GitHub (GitHub e-mails you) and never uses it again. New option `retire_old_token`
  (default on).
- **Backup encryption watch:** a warning when the newest full backup (not this App's own pre-update backups) is
  not password-protected, with the Google Drive Backup fix.
- **Deployer to the store:** when the store Deployer is installed next to the local one, this App asks once:
  "Switch the Deployer to the store?". After Approve it copies the settings, stops the old Deployer (it is kept),
  starts the store one and checks that it reports in, or puts everything back automatically. Seven days later it
  asks separately whether to remove the old one.

## 0.6.5

Owner-approved feature (2026-10-07). Contains everything in 0.6.4 and 0.6.3 unchanged.

- **Ask me now.** New request job `ASK_UPDATE_NOW`: a chat (or you) can have your phone ask again right away for an
  update that timed out, instead of waiting for the re-ask. Works for Core, OS and App updates, but only for the
  exact update this App already asked about and Supervisor still offers; anything else is refused. Reject stays
  final. After Approve the usual backup, update, health watch and automatic restore run. It counts against
  `max_approval_requests_per_day` (read-only requests stay exempt, as in 0.6.4).
- **No answer asks again after 6 hours instead of 24.** New option `update_reask_hours` (1–48, default 6) for App,
  Core and OS update asks. Dry-run reports stay once a day.

## 0.6.4

Fix (owner pop-up 2026-10-07 "Fix it in 0.6.4"). Contains everything in 0.6.3 unchanged.

- **Read-only requests are no longer held back by the approval limit.** `max_approval_requests_per_day` limits how
  often this App asks you to approve something. It also blocked requests that never ask you anything (the Broker
  check and the App log window): live on 2026-10-07 a log request waited 3 hours behind four unrelated approval asks.
  Those two read-only jobs now run on the next poll; jobs that ask for your approval (Scout run, key rotation) keep
  the limit exactly as before.

## 0.6.3

Credential Autopilot (owner decisions 2026-10-06, pop-ups: two GitHub Apps; Connect button with key-file upload
fallback; one update that carries 0.6.0, 0.6.1 and 0.6.2 too; credential list and daily check). Contains everything in 0.6.2, 0.6.1 and 0.6.0 unchanged.

- **GitHub sign-in without a hand-made token.** New **GitHub connection** page (link at the bottom of the Maintenance
  page). **Connect to GitHub** creates a private GitHub App "House Brain Maintenance Bot" with fixed permissions
  (Contents: Read, Issues: Read and write, Metadata: Read; no webhook, no events) and GitHub hands its key straight to
  this App. The key is kept only in `/data/github_app/key.pem` (0600), never in the options (finding F2: every
  manager-role App, including this one, can read every App's options). One-hour passes limited to this repository
  and those permissions, about 24 a day, at most 30; a failed sign-in is retried after 5 minutes, keeping its real
  reason. The key never expires. Fallback: upload the `.pem` file.
  **Disconnect** deletes the key from this App.
- **New option `github_auth`** (`auto` default / `github_app` / `pat`); `github_token` is now optional. In `auto`, a
  failing GitHub App falls back to the old token and says why.
- **Daily credential check (read-only).** Warnings at **30, 14, 7 and 1 days** before the old GitHub token of this App
  or of the Deployer expires (date read from GitHub's `GitHub-Authentication-Token-Expiration` header; the Deployer's
  from its status entity, field by field). Each step is pushed once, listed in the morning summary and on the
  tracking issue; a new step is a new warning, the same step never repeats. A failing GitHub App sign-in and "not
  connected" are warned too. Nothing is said while the GitHub App works (the old token no longer matters).
- **Relay dispatch key (new option `relay_credential_url`: empty = derived from `liveness_url`, nothing to set; `off` = disabled).** Once a day reads the
  relay Worker's public `/v1/dispatch-credential` (one word + date, from the relay's weekly self-test of
  `GITHUB_DISPATCH_TOKEN`): the same 30/14/7/1 ladder, plus a warning with the exact fix path when the key fails or
  the self-test stops. An unreadable answer never clears an open warning.
- New reason codes `GITHUB_APP_*`. Clock guard (no clock battery). New hash-pinned dependency `cryptography` 50.0.2
  (+ `cffi` 2.1.1, `pycparser` 3.0).
- Tests: the shared GitHub App cases (53) + the credential ladder (24); 30 new mutants (all killed); aarch64 image smoke.

## 0.6.2

- **UPnP dropouts: house-specific advice** (live 2026-10-03..05, owner 2026-10-06). The Orbi RBR840 UPnP
  integration dropped out once or twice a day and came back by itself, so UPnP is on; the router's
  announcements are lost on the way to the Pi. The alert is now "UPnP router dropped out (it comes back by
  itself)" and no longer says to turn UPnP on: nothing to do, and only if it lasts more than 30 minutes,
  restart the Orbi router. The owner set the Orbi advertisement period to 2 minutes (shorter dropouts) and
  declined any cabling change. Detection, severity and the network check are unchanged. (Built as 0.6.1 in parallel with the
  shared-socket 0.6.1; renumbered.)

## 0.6.1

Fix at the source (owner decision 2026-10-06, Connection Forensics chat → Maintenance chat): `sensor.connected_clients`
flipped by 1 about once a minute, around the clock (~2,500 history rows a day), hiding real phone and browser drops.

- **Cause (confirmed from code):** the problem watch (every 3 min) opened, logged in and closed **two** fresh Core
  websockets per check (`core_problems`, `backup_locations`) — about 960 sessions and ~1,900 state changes a day. The
  Engineering Inspector adds one per 5 minutes (fixed separately in Inspector 0.2.4). The Recovery Report heartbeat
  (60 s) uses REST and was not involved.
- **Fix:** the periodic reads (`core_problems`, `backup_locations`, `device_health`, `mesh_counts`) share **one**
  long-lived, authenticated Core socket. One user at a time (lock), so the watch, the Recovery Report and jobs never
  interleave. A dropped socket (for example a Core restart) reconnects on the next read, with backoff from 1 s doubling
  to 60 s, so a restarting Core is not hammered; an "error" answer from Core keeps the connection. Closed on App stop.
  The network check's short subscription (`ssdp_heard`) and approval waits keep their own short sockets (rare).
- **Unchanged:** the same reads, the same pinned WebSocket command allowlist, no new route, read-only.
- **Expected:** `connected_clients` holds a constant +1 for this App instead of flipping every few minutes.

## 0.6.0

Core/OS update gate (owner decision 2026-10-06, verbatim: "The Maintenance App would post a review whenever Core or OS
has an update; I add the release-note check. You tap Approve on your phone. The App then takes a full backup, updates
(Core and OS separately, never automatically), checks health, and restores the backup on its own if Core fails. OS
falls back to its previous version if it can't boot.").

- **Detection (read-only):** `/core/info` and the new `/os/info`, on the App-update cadence, projected to version,
  latest version and `update_available`. One review per offered version on the tracking issue (kind, from -> to,
  latest backup age, free disk, "Release-note check: by the AI in chat; approve only after it"); the iPhone asks
  about an hour later. Never automatic (`update_mode` does not apply); one Core/OS item at a time, Core first, never
  in the same run as an App update or the other one. Reject is final for that version; no answer asks again in 24 h.
- **After Approve:** free-disk check (twice the newest full backup, at least 2 GB), full backup
  `hbm-pre-<core|os>-<version>` verified by `/backups/<slug>/info` (type full, current Core version), re-check that
  the offer did not change, then `POST /core/update {"version": <to>, "backup": false}` or
  `POST /os/update {"version": <to>}`.
- **Core health:** Core answers, `/core/info` version is the new one, Core `/api/config` state `RUNNING` and not
  safe/recovery mode, no new Supervisor unhealthy reason; within 15 minutes and still after 1 more minute. On failure:
  `POST /backups/<that backup>/restore/partial {"homeassistant": true}` (Home Assistant only), old version verified ->
  `ROLLED_BACK` and the version is not offered again; if the restore does not bring it back -> `FAILED_MANUAL`, all
  work pauses (as for App updates).
- **OS:** journalled before the call; the host reboots and the App resumes from the journal: new version -> `DONE`,
  previous version -> `ROLLED_BACK` "OS fell back to previous version (RAUC A/B)"; Core health checked after the
  reboot (no automatic restore for the OS; unhealthy Core or no reboot within an hour -> `FAILED_MANUAL`).
- **Boundary:** new read-only routes `GET /os/info`, `GET /core/api/config` (projected); mutating routes only inside
  `system_target(kind, version)` with exact bodies. App-update backup/restore bodies are now pinned too (that App only,
  never Home Assistant). Still never the Supervisor, itself, a host reboot or shutdown.

## 0.5.4

App log window (owner decision 2026-10-06, Connection Forensics chat → Maintenance chat). Read-only.

- **New `APP_LOG_WINDOW` job** (no approval). A request names one installed House Brain App, a UTC window (at most
  6 hours), a boot (0 to -5) and up to 5 plain keywords. The App reads that App's verbose log for that boot
  (`/addons/<slug>/logs/boots/<n>?verbose&no_colors&lines=20000`, at most 4 MiB, 60 s), keeps the lines inside the
  window that contain a keyword, drops the host name, scrubs secrets, e-mail addresses, IPs, MACs, URL queries and long
  ids, and posts at most 200 lines (300 characters each, 40,000 in all) with the result on the tracking issue.
- **Two new read-only routes**: the App-log route above, only for slugs `local_house_brain_*` / `<8 hex>_house_brain_*`
  that are installed and are not this App, with exactly that query; and `/host/logs/boots` (boot offsets only, ids
  dropped), so the result says whether the host journal still holds the asked boot. This is the App's first host route
  (owner choice). Nothing is written to any App.
- A request may now name the App whose log is **read**. A request still can never name an App to change.

## 0.5.3

Two false warnings in the 2026-10-05 morning summary fixed, plus a Broker version check (owner decision
2026-10-05, "Both, fixes first"). All read-only.

- **Fix: "Backups are only stored on the Pi"** although the Home Assistant Google Drive Backup App copies them to
  Google Drive. That App works outside Home Assistant's own backup locations, which were the only thing checked.
  Its status entity `sensor.backup_state` is now read (one pinned GET; kept: state, date of the newest backup in
  Google Drive, how many). A copy there counts as off the Pi. Its `error` state is reported as its own warning.
  When it is not installed nothing changes.
- **Fix: "Latest full backup is much bigger than usual (2.4 GB, usually 0.0 GB)"**: settings-only backups (about
  30 MB) were compared with full backups of everything. Backups are now grouped by what they contain (full, or
  Home Assistant / Apps / folders) and compared only within a group. A group whose newest backup is older than 8
  days is not judged again.
- **New: `CHECK_BROKER`** (no approval): the live Maintenance Broker version from its public `/healthz` (no key
  sent), so a Broker deploy can be confirmed without the owner. `RUN_SCOUT_ONCE` results carry it too.

## 0.5.2

Recovery Report follow-ups (owner decision 2026-10-05, "All of the above"), all read-only:

- **Fix: no stale restart verdict.** Home Assistant answers before the Connection Forensics start marker
  runs, so the report could show the *previous* restart's CLEAN/UNCLEAN. The verdict is now used only when
  its `last_start` is not older than the outage start. The report waits up to 5 minutes for it, then says
  "verdict not yet available". The restart that installs Connection Forensics (`FIRST_RUN`) no longer pushes.
- **Planned or not:** a clean Core restart is tagged **PLANNED** with the House Brain Deployer request id when
  the Deployer was installing just before it, or published a restart result after it. Otherwise it is tagged
  **UNPLANNED_CLEAN**. UNCLEAN stays UNCLEAN. One more pinned read: `sensor.house_brain_deployer_status`
  (state, request id, dry run and last result only).
- **Restart ledger:** 90 days of Core restarts by class (PLANNED, UPDATE, INSTALL, UNPLANNED_CLEAN, UNCLEAN,
  HOST_REBOOT, HOST_UNEXPECTED, POWER_CUT, UNKNOWN). It shows 30/90-day counts and the mean time between
  unplanned failures on the page and in the status entity, plus a text block for the #152/#163 stability
  ledger.
- **Mesh rejoin:** after a Core restart, how many seconds Zigbee (ZHA), Z-Wave JS and Matter took until their
  devices were back, or how many were still unavailable after 10 minutes. Devices that were already
  unavailable before the restart are not counted. Meshes not used here are skipped. Page only, no push.

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
