# Changelog

## 0.3.30 — 2026-10-19 (House Brain Companion set-up; owner pop-ups "Yes, one tap" and "Pi makes it, nobody sees it")

- Why: the owner asked that the AI do the Companion set-up so they have no steps left. The Home Assistant part was
  the llama.cpp integration (URL, key, model, control off) and a voice assistant, done by hand in the UI.
- `house_brain_companion_key_request.v1`: makes a 48-hex key in `/data/companion_key` (0600) when there is none and
  reports only its SHA-256 (`companion_key_sha256`). No Home Assistant call, no approval (lookup allowance).
  `rotate: true` makes a new key after an approval.
- `house_brain_companion_setup_request.v1` (owner approval, install allowance): Home Assistant's own config flow for
  `llama_cpp` with the Companion URL (carried by the request, accepted only if its SHA-256 equals the pinned
  `COMPANION_URL_SHA256`, because the public store refuses a concrete Cloudflare host) and the Pi key; the Companion model; then the conversation subentry is
  re-saved as exactly `{"recommended": true, "chat_model": "house-brain-companion"}` (no `llm_hass_api`, so no device
  control) and the agent must stop reporting the CONTROL feature within 30 s; then one Assist pipeline "House Brain
  Companion" with that agent (language/voice copied from the preferred pipeline, no wake word, Home Assistant's own
  sentence matching off). Any failure after the entry exists deletes that entry. An existing entry for the URL:
  `COMPANION_ALREADY_SET_UP`, nothing changed.
- Allowlist: five pinned HTTP paths (config flow, its steps, subentry flow, its steps, DELETE of one entry id), each
  with a pinned body (`_body_guard`); four WebSocket commands (`config_entries/subentries/list`,
  `config/entity_registry/list`, `assist_pipeline/pipeline/list`, `assist_pipeline/pipeline/create` with the exact
  Companion field set). Options flows, other integrations, other subentry types, pipeline update/delete stay refused.
- The key is registered as a secret for redaction and never appears in a comment, push, status or audit line (tested).
- Tests: L1 parse/guards/key file; L4 mimic of the real poll loop (set-up, control removed and verified, nothing
  without the owner, failure undoes the entry, wrong key or unreachable Worker creates nothing, second set-up finds it).
  Hand mutants 12/13 (the survivor: the re-save result check, backed by the independent CONTROL-feature check).

## 0.3.29 — 2026-10-18 (read-only Z-Wave configuration lookup; owner pop-up "This chat builds it")

- Why: the evap auto-off design (#912) needs the ZEN16 timer parameters 6–11 and 15–20. Home Assistant exposes
  no configuration entities for it (lookup #140 6198135273 found none), and no record holds the values.
- An owner-approved lookup (`house_brain_lookup_request.v1`) may add `zwave_config_entity` (an entity id). After the
  owner's Approve, and only then, the Deployer resolves that entity's device through the read-only template
  `device_id()` and sends exactly one WebSocket command, `zwave_js/get_config_parameters`, for that device.
  Z-Wave JS answers from the values it already holds: no radio traffic, no write.
- The WebSocket allowlist gains only that command, with exactly `type` and one 32-hex `device_id`. Every Z-Wave
  write or refresh command stays refused (`set_config_parameter`, `set_raw_config_parameter`,
  `refresh_node_values`, ...).
- Output is projected to parameter number (and sub-key), label, value, unit and default; descriptions and all
  other metadata are dropped; at most 200 parameters. The approval message names the entity.
- Unknown entity: reported as "no device found", no Z-Wave read. Without the new field a lookup is exactly 0.3.28.
- Tests: L1 guard/parse/projection, L4 mimic (approved read changes nothing; reject, no answer and a forged user
  read nothing; unknown entity reads nothing); mutants ZC01–ZC07.

## 0.3.28 — 2026-10-17 (AppArmor reads for the history move; owner pop-up "Build 0.3.28")

- M4 attempt 3 on 0.3.27 passed every history check and the owner's Approve, then ended
  `LEGACY_FAILED: LEGACY:RootBindingError` before writing the copy (#561 6168580600).
- Cause: the folder binding (used by Prepare, Select, the moved-history startup and the interrupted-install
  recovery) walks each path from "/" and reads each held folder's mount id from `/proc/<pid>/fdinfo/<n>`. The
  AppArmor profile allowed neither. Two read-only rules are added: `/ r,` (the root directory itself, not its
  contents) and `/proc/[0-9]*/fdinfo/[0-9]* r,`. Nothing is writable that was not before.
- A failed binding now reports its fixed code and the OS error, e.g. `LEGACY:RootBindingError:ROOT_UNAVAILABLE:EACCES`,
  never a path.
- New confined L5 test runs the real binding inside the enforced AppArmor container; L4 test for the report;
  mutants RG01–RG02.

## 0.3.27 — 2026-10-14 (history migration accepts every shape published versions wrote; owner pop-up "Build 0.3.27, all 7")

- The M4 retry on 0.3.26 refused with `LEGACY_HISTORY:APPROVAL_SCHEMA` (#561 6161027304), before asking and with
  nothing written. A sweep of every Deployer version from 0.1.0 to 0.3.26 found seven record shapes that real
  versions wrote and the migration validator (`hbd/migration/qualify_deployer_history.py`) rejected:
  1. approval decisions without `channel` (0.1.0);
  2. a recovered Undo stored with the rebuilt manifest's sha256 (0.3.3+);
  3. digest `unknown` after restart recovery (all versions);
  4. `restart_timed_out` on an `#undo` key (0.3.11+);
  5. a refused re-ask of a re-ask, recorded with its link (0.3.8+);
  6. a recovery during dry run carrying `restart_timed_out` on its `#dry` key;
  7. `undone_at` earlier than `at` after the Pi's clock stepped back.
  All seven are now accepted, exactly and nothing wider (a present `channel` must be a string; digests are still
  sha256, `undo`, `undo:<time>` or `unknown`; the flag is still exactly `true` with `ROLLED_BACK`). These records are
  history only and never approve anything; the live re-ask check still refuses running a re-ask of a re-ask.
- Tests: an end-to-end Prepare of an old history holding all seven shapes (it refuses on 0.3.26); the validator's
  unit tests changed on purpose for these shapes; mutants QS01–QS07 killed.

## 0.3.26 — 2026-10-14 (history migration: overlap rule; owner pop-up 2026-10-14 "Keep newer, save old")

- Pi step M4 (Prepare) refused on the real histories with `LEGACY_HISTORY:LEDGER_CONFLICT` (#561 6159321794): the
  same request ids are in both Deployers' histories with different entries (each Deployer stamps its own time).
  It refused before asking and wrote nothing.
- Now, for a result or install that is in both histories with a different entry, the store Deployer's entry is kept
  and the old Deployer's entry is saved unchanged in `hbd-generations-v1/LEGACY-OVERLAP-<run>.json` (0600, next to
  the PREPARED record; written at Prepare and again at Select). Equal entries are not overlaps; with no overlap the
  merge and every fingerprint are exactly as in 0.3.25. The id stays handled either way, so nothing can run again.
- The Prepare and Select asks say how many results and installs overlapped; the results report `overlap` counts and
  the side file's `overlap_sha256`. A different overlap between the ask and the lock refuses (`LEGACY_DRIFT`).
- A freeze that differs between the two histories still refuses (`FREEZE_CONFLICT`).
- Tests: 3 new L4 tests (23 in `test_l4_legacy_migrate.py`); mutants LM31–LM37 killed.

## 0.3.25 — 2026-10-13 (migration polish; owner pop-ups 2026-10-13; renumbered above the 0.3.24 package tidy)

- Installs that came from the old Deployer are kept and shown, but get no Undo button after the switch (owner:
  "Not offered by default"); Undo on such a record is also refused if asked some other way. If the list of old
  installs cannot be read, every Undo button is hidden, never extra ones shown.
- The status (`history` attribute) and the page show which history is in use: previous, prepared (Select within 7
  days), selected (takes over at the next restart), moved, or moved and set aside.
- New `house_brain_legacy_reset_request.v1`: after a Select that did not finish, one owner Approve renames the
  half-made working copy aside (kept, never deleted) so Select can be sent again. Refused while a selection exists.
- Owner-only "Go back to the previous history" button on the page while a selection exists (single-use code, owner
  account only). It sets the selection aside like Unselect / `return_to_previous_history`; the change applies at the
  next restart.
## 0.3.24 — package tidy: rename the one space-named backup (owner pop-up 2026-10-13)

- New fixed request `house_brain_package_tidy_request.v1` with one operation, `rename_space_named_backup`. It renames
  `/config/packages/house_energy_brain_v1_1.yaml copy.bak` to `house_energy_brain_v1_1.yaml.copy.bak`. Both names are
  literals in `hbd/package_tidy.py`; a request names neither (no path, no YAML, no files). An optional
  `expect_current_sha256` pins the old file's bytes.
- Same safety path as every install: one Approve tap, partial backup, configuration checks before and after, the
  existing link → verify → unlink rename steps, automatic rollback on any failure. The bytes are kept under the new
  name, never edited or deleted.
- No restart: Home Assistant does not load `.bak` files, so the job ends after the configuration check (no second
  tap). The result must show the old name gone and the new name holding the old bytes, or it rolls back
  (`TIDY_RESULT_NOT_VERIFIED`). It is not offered to the page's Undo; the bytes stay in the new name.
- The moved-history file layer (0.3.23) admits exactly that one space-named name, so the tidy works in both modes.
- Tests: `test_package_tidy.py` (17), mutants PT01–PT07 killed.

## 0.3.23 — expanded candidate preparation (NOT INSTALL READY; was 0.3.13, 0.3.16, 0.3.17, 0.3.19, 0.3.20, 0.3.21; renumbered above main's 0.3.22)

- Composes published 0.3.19 (one-tap writer lock enrollment), 0.3.20 (temporary folder-create rule removed; approval
  ask/answer log lines), 0.3.21 (writer lock probe; read-only legacy stage folder) and 0.3.22 (10-minute probe hold).
  The writer lock exists on the Pi (#561 comment 6125839870), and the E6 two-way busy test passed on 2026-10-13
  (#561 6138174545; #454 6138024149).
- Moving the old Deployer's history (#985 plan steps M4/M5; owner pop-ups 2026-10-13 "Build both",
  "Yes, /data only", "Re-merge at Select", "7 days", "No tap after Select"):
  - `house_brain_legacy_prepare_request.v1` reads Maintenance's stage read-only with a pinned copy of
    Maintenance's own reader, merges it with the live history and, after Approve and under the shared lock, writes a
    sealed copy and export inside `/data` only. Nothing is selected.
  - `house_brain_legacy_select_request.v1` (within 7 days) merges again with the latest live history and, after
    Approve, prepares the working copy and publishes `MIGRATED_STARTUP.json`. Other requests wait until the restart.
  - `house_brain_legacy_unselect_request.v1` sets the selection aside (kept) before a restart; the new option
    `return_to_previous_history` does the same at startup.
  - Normal startup on the moved history with no tap: the Select-approved selection is the authority, re-checked at
    every start with its folders and the lock.
  - Fixes found on the way: the selection reader refused the installed `/data` (0755, accepted 2026-10-12); the
    history checker refused outcomes published since 0.3.19 (`WRITER_*`).
- A busy writer lock probe now names the lock it found busy (`dev`/`ino` from a plain `stat`; the lock is never opened
  or created for this). E6 showed the Deployer's busy result could not name its inode (#561 6138174545).

- Explicit directory bindings now carry through selected Journal reads/writes, package staging,
  forward Service, recovery and initial migration publication. Each effect requires the current
  writer lease and fresh phase authority; package directory permissions are preserved.
- Losing the writer lease leaves recovery intent untouched, including audit and freeze files.
  History pruning preserves the newest Undo tombstones and pending/frozen provenance.
- The installed capture/provider/issuer, selected-backup custody, actual shared-writer enrollment
  and Maintenance's separate staging authority remain unfinished. Local mimic results do not
  qualify native installation or genuine migration/Undo/restore.
- The installed-instance reader accepts this candidate's own version (it still allowed only the
  retired 0.3.13 draft label, so a renumbered candidate could not observe itself) plus published
  0.3.14/0.3.15/0.3.16/0.3.18/0.3.19/0.3.20. 0.3.13 and 0.3.17 were never published and are refused. Compatibility only, never qualification.
- Owner-tap recovery session (owner pop-up 2026-10-12, design R1 option A; not wired into startup yet):
  one authenticated phone Approve opens a recovery-only session for one exact interrupted install, at most
  2 hours. Every step re-checks the session; a changed binding, lost lock, App restart or expiry holds. The
  old 30-second grant could not cover a real Core restart wait or the restart re-ask. Never authorizes
  forward work, initial import or Maintenance staging.
- Installed `/data` is 0755 (native 0.3.16 self-report, #454 6117189087); the migration root rule required
  0700 and would have refused the real install. Owner pop-up 2026-10-12 "Accept 0755 as observed": the data
  root may be 0700 or 0755 exactly as observed, never chmod-ed, pinned for the run; every migration folder
  inside it stays 0700 and is checked on every open. All other private roots stay 0700.
- Layout R1, Deployer half (owner pop-up 2026-10-12 "Approve R1 for #985"): `/share` mapped read-write, and
  AppArmor allows only the shared writer directory and its lock file. Creating them stays a separate
  enrollment action. The Maintenance half belongs to its own lane.
- Startup adapter for the owner-tap session (owner "Build on #985 now"): `owner_recovery_authorize` plugs the
  issuer into `startup_hold`'s recovery provider, and `installed_root_specs` takes every root mode exactly as
  observed. Not yet constructed by `main`: that needs the enrolled writer lock and the pending request's
  manifest source.
- Selected-backup custody: one Supervisor partial backup holding only the stopped legacy App, pinned by its
  Supervisor metadata and re-verified before use. It never deletes, restores or downloads; the archive bytes
  are not hashed, because this App cannot read them.
- Startup wiring (owner "all of the above", 2026-10-12): `build_service` builds the owner-tap provider only when no
  explicit provider is given. With no selection marker nothing new runs. With a v2 marker, the installed roots
  are opened from observed modes and the enrolled lock is taken without creating it. A missing piece keeps today's
  `STARTUP_PROVIDER_UNAVAILABLE` hold, and nothing is asked. Migrated recovery has no manifest source, so it takes
  the existing conservative path (old files restored).
- Writer enrollment (plan R1, step E1):
  - `WriterLease(create=False)` refuses a missing lock (`WRITER_NOT_ENROLLED`); the installed forward path uses it.
  - `enroll_writer()` creates only an absent 0700 directory and an absent empty 0600 lock.
  - It refuses foreign, partial or mismatched shapes before creating anything, and never repairs them.

- D6 safety items (Claude, hand-off 6110157412; owner decisions 2026-10-11), each for joint review:
  - a migrated-runtime refusal holds this App run in memory (no write through the shared journal; page shows "Held");
  - a lost write lease never rolls files back: the install stays as it is, the transaction is kept, FAILED_MANUAL
    with the exact remedy (owner: "Freeze, don't touch files");
  - an unusable migration start file is reported once (HELD status, never over an open install's marker, plus one
    push) and the App stays stopped: no service, no migration;
  - install history is capped at 500 records; only Undo-resolved records are pruned, otherwise recording fails closed.
- Composed on top of published 0.3.18 (0.3.14 approval link, 0.3.15 restart recovery, 0.3.16 read-only
  startup self-report and 0.3.18 AppArmor read rules kept).

- Three-way composition retains published0.3.11 restart-timeout re-ask behavior and draft985 approval routing, defaults, complete install history, bounded audit cache, honest qualification display and inactive writer/migration pathways.
- Source preparation only; production provider/issuer/startup integration and native/live acceptance remain unfinished. Historical0.3.10 carrier/test seals do not qualify these changed bytes. No0.3.13 carrier or store publication. Current-main composition preserves the accepted0.3.12 request-backlog hotfix before the poll cap.

- Private startup selection and recovery-only startup hold are source-tested; authentic installed publisher/provider and forward startup remain unfinished.
## 0.3.22 — 2026-10-13

The lock test's `hold` now lasts 10 minutes instead of 60 seconds (owner pop-up 2026-10-13 "Longer hold"). In three live
tries the Maintenance App's prompt arrived minutes after the Deployer's, so its `try` never fell inside a 60-second hold.
Holding only makes the other App wait; nothing is created or changed. Everything else in 0.3.21 is unchanged; draft
PR #985 moves to 0.3.23.

## 0.3.21 — 2026-10-13

A one-tap test of the shared writer lock, and read-only access to the legacy history stage (owner pop-up 2026-10-13
"Small 0.3.21 first"; #985 enrollment plan R1 step E6 and Pi test plan M4 groundwork). Everything in 0.3.20 is
unchanged; draft PR #985 moves to 0.3.22.

- **Lock test:** an AI can file `house_brain_writer_probe_request.v1` with `operation` `try` or `hold`. After your Approve
  the Deployer either takes the shared lock once and lets it go (`ACQUIRED`, or `WRITER_BUSY` when the Maintenance App
  holds it), or holds it for a fixed 60 seconds so the Maintenance App's own test must report busy. It never creates
  the lock, writes nothing and reports only the lock's device/inode. Together with the Maintenance App's `WRITER_PROBE`
  job (0.9.4) this proves the two Apps can never write at the same time.
- **Legacy stage:** AppArmor adds read-only `/share/house_brain_legacy_stage_v1/` and its contents, the folder the
  Maintenance App (0.9.5) fills with the old Deployer's history. Nothing in this release reads it yet.
- **Unchanged:** every install, lookup, re-ask, HTTP and enrollment path, and the approval log lines.

## 0.3.20 — 2026-10-12

Removes the temporary folder-create permission and logs each approval ask (owner pop-up 2026-10-12 "E5 + ask/answer
log"; #985 enrollment plan R1 step E5). Contains everything in 0.3.19 unchanged otherwise. Draft PR #985 moves to
0.3.21.

- **Why:** the shared writer lock now exists on the Pi (#561 comment 6125839870), so the folder-create rule is no
  longer needed. On 2026-10-12, nobody could tell from the log when the enrollment prompt went out.
- **Change:**
  - `apparmor.txt` drops `/share/house_brain_writer_v1/ w,`. The folder and lock rules stay as they were.
  - The log gets two lines for every approval: `approval asked: <request> stage=<stage> expires in <N> min`, then
    `approval answered: <request> stage=<stage> outcome=<APPROVE|REJECT|TIMEOUT|UNAVAILABLE|STOPPED> channel=<push|page|->`.
    They hold the request id and stage only, never the message text.
- **Unchanged:** every install, lookup, re-ask, HTTP and enrollment path. A repeat enrollment request still reports
  the existing folder and lock without changing them.

## 0.3.19 — 2026-10-12

One-tap creation of the shared writer lock (owner pop-up 2026-10-12 "Build it now"; #985 enrollment plan R1 step
E3). Contains everything in 0.3.18 unchanged. Draft PR #985 moves to 0.3.20.

- **Why:** the Deployer and the Maintenance App must never write at the same time. They will share one lock file
  in `/share/house_brain_writer_v1/`. Neither App could create that folder until now.
- **Change:**
  - `/share` is mapped. AppArmor allows only that one folder and its lock file, plus a **temporary** rule that lets
    the folder be created. The next release removes that rule.
  - A new request, `house_brain_writer_enroll_request.v1`, can be filed by an AI. It names no path. After your
    Approve, the Deployer creates only the empty private folder (0700) and the empty lock file (0600). Anything
    already there in another shape is left as it is and reported.
  - The startup self-report then shows both paths.
- **Unchanged:** every install, lookup, re-ask and HTTP path. Nothing else in `/share` is readable or writable.

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
