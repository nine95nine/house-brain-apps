# Changelog

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
