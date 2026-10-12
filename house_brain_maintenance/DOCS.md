# House Brain Maintenance

This App keeps your Home Assistant Apps up to date safely and tells you about problems with a
suggested fix. It never changes anything without a rule you chose: every update and every fix
waits for your approval on your iPhone, unless you switch on automatic installs of low-risk
bug-fix updates at night.

## What it does (0.8.4)

**App updates.** About once an hour it looks for Apps with an update waiting, one at a time:

1. **Review.** It reads the release notes and the facts Home Assistant reports, then labels the
   update **low risk**, **risky** or **held**:
   - *held* (not offered): the new version needs a newer Home Assistant than you run, the store
     says it cannot be installed, Home Assistant reports it is unhealthy, or this exact version
     already failed here;
   - *risky*: a major version jump, release notes that mention breaking changes or migrations,
     no release notes, new permissions, an experimental version, or other Apps rely on it
     (for example Zigbee2MQTT relies on the Mosquitto broker);
   - *low risk*: none of the above.
2. **Ask.** Your phone shows the App, versions, the review and the plan. Approve needs Face ID.
3. **Backup.** A backup of **that App only** is made and checked before anything changes.
4. **Update.** Only that App is updated. Home Assistant itself is not updated or restarted.
5. **Health watch** (3 minutes by default). The App must be running (if it was running before),
   Home Assistant must answer and stay healthy, and Apps that rely on it must still be running.
6. **Automatic restore.** If the watch fails, that App's backup is restored and the version is
   never offered again. If even the restore fails, the App pauses all work and tells you.

If you tap Reject, that version is not offered again. If you do not answer, it asks again after
`update_reask_hours` (default 6 hours; until 0.6.4 it was the next day), or right away when you ask for it
(**Ask me now**, below).

**Home Assistant Core and OS updates** (new in 0.6.0, owner decision 2026-10-06). On the same hourly check it
reads whether Core or the OS has an update. It never installs one by itself, also not with `auto_low_risk`:

1. **Review.** It posts one review on the tracking issue (`report_issue`): the kind (CORE or OS), from and to
   version, how old the latest backup with Home Assistant is, the free disk, and "Release-note check: by the AI
   in chat; approve only after it". The House Brain AI adds its release-note check there.
2. **Ask.** About an hour later your phone asks, with the same facts. Approve needs Face ID. Reject means that
   version is not offered again; no answer asks again after `update_reask_hours` (default 6 hours), or right
   away with **Ask me now** (below). Only one Core or OS update is asked at a
   time (Core first), and never in the same run as an App update or the other one.
3. **Full backup.** After Approve it makes a full backup (`hbm-pre-core-<version>` or `hbm-pre-os-<version>`)
   and checks it exists. If there is not enough free disk, or the backup fails, it stops: nothing is updated.
4. **Core:** it updates Core to exactly the approved version and checks that Core answers, reports that
   version, runs normally (not safe mode) and that Home Assistant did not become unhealthy. If that fails, it
   **restores Home Assistant only** from that backup by itself (not your Apps or folders), checks the old
   version is back and reports ROLLED_BACK. If even the restore fails, it pauses all work and tells you.
5. **OS (changed in 0.8.3).** Since Supervisor 2026.07 an OS update is *installed* into the Pi's spare slot
   without a restart; the old OS keeps running until the next restart. So:
   - It installs exactly the approved version. Nothing restarts yet.
   - Your phone asks **"Restart to finish the OS update?"** (Face ID). On Approve it runs Home Assistant's
     configuration check (a failed check means no restart; it asks again later) and then restarts the Pi.
   - After the restart: the new OS and a healthy Home Assistant is DONE. The old OS means the new one could not
     boot and the Pi fell back by itself (A/B): ROLLED_BACK, and that version is not offered again.
   - New OS but Home Assistant not healthy: your phone offers **"Roll back the OS?"**. Approve restarts the Pi
     into the previous OS (still in the other slot). Reject keeps the new OS.
   - No answer asks again after `update_reask_hours`. **Reject** keeps the OS installed and does not ask again.
   - Waiting never pauses this App: other updates, the problem watch and key changes go on.
   - **On a Raspberry Pi 4 any restart, a power cut too, switches to an installed OS.** The App checks such a
     restart the same way, and the morning summary warns while an installed OS waits (also one installed from
     Settings).
   - An older Supervisor that restarts during the install is handled as before.

**Known issues and release notes before a Core update** (new in 0.8.4, owner pop-up 2026-10-11). The Core
review on GitHub and the phone ask now also say:
- **Known issues for your integrations in that version**, from Home Assistant's own alerts feed
  (`alerts.home-assistant.io`, the feed behind Settings → Repairs alerts), checked against the *new* version and
  only for integrations you run, with a link to each alert;
- **Release notes for your integrations**: the changes in that version that touch an integration you run, with
  the breaking ones listed first (from the version's GitHub release notes).

This is information only: it never blocks or approves anything. If a feed cannot be read, the review says so.
Switch it off with `upgrade_checks: false`.

**Configuration check before a restart** (new in 0.8.4). After a HACS integration update, and before Home
Assistant restarts, the App runs Home Assistant's configuration check. If the check passed before the install and
fails after it, the old version is put back, Home Assistant is **not** restarted, and that version is not offered
again. (0.8.3 already checks the configuration before the restart that finishes an OS update.)

**HACS and device-firmware updates** (new in 0.7.0, owner decisions 2026-10-08: "have the maintenance app
handle any and all applications"). On the same hourly check it reads every Home Assistant update item that the
Supervisor does not own (Apps, Core and OS keep their own paths above) and handles one at a time:

| Kind | Examples | What happens |
|---|---|---|
| HACS card or theme | Mushroom | Reviewed like an App. With `update_mode: auto_low_risk` a low-risk bug-fix update installs by itself in the night window after `auto_wait_days`; anything bigger asks. |
| HACS integration (and other HACS code) | B-hyve, HACS itself | **Always asks.** One Approve covers the Home Assistant restart it needs and, if the check fails, the rollback restart. |
| Device firmware | Z-Wave relays, the Z-Wave stick, a router | **Always asks, never automatic**, and only after the version has been offered for `firmware_wait_days` (default 30), so other people find a bad firmware first. |

1. **Review.** The release notes Home Assistant shows for that item, the version jump and, for HACS, the minimum
   Home Assistant version it declares: **held** (needs a newer Home Assistant, failed here before, already
   installing, Home Assistant unhealthy), **risky** (major jump, breaking/migration wording, no notes) or **low risk**.
2. **Backup (HACS only).** A backup of the Home Assistant configuration without its database (it holds the HACS
   files), named `hbm-pre-<item>-<version>`. Firmware cannot be backed up.
3. **Install** exactly that version (firmware: the version the device offers), through Home Assistant's own
   update service. HACS integrations: then Home Assistant restarts.
4. **Health check.** Home Assistant must run normally; the item must report the new version; an integration must
   load again and its entities must not be more unavailable than before; a device must come back.
5. **Automatic rollback (HACS).** If the check fails, the previous version is reinstalled (and Home Assistant
   restarted again for an integration) and the failed version is never offered again. **Firmware cannot be
   rolled back**: a device that does not come back is reported and the App pauses (`FAILED_MANUAL`).

**Hold list** (new in 0.7.3, owner decisions 2026-10-08: "Hold list", "Auto-hold .0 + my list", "AI files a
request"). An update on hold is **never sent to your phone**, so a tap cannot approve it by mistake. Every update
path respects it (Apps, Core, OS, HACS, firmware and `ASK_UPDATE_NOW`).

- **Automatic:** every new Core `YYYY.M.0` is held until a later patch (`.1` or newer) is offered. It is your
  standing rule "wait for .1" (option `hold_core_dot_zero`, default on). The App says so once on the tracking issue.
- **Your list:** tell the AI in chat. It files a `HOLD_UPDATE` or `RELEASE_UPDATE` request, and the App confirms
  on the tracking issue with the whole list. Items: `core`, `os`, `core@2026.10.0`, `os@18.4`, `app:<slug>`,
  `app:<slug>@<version>`, `update.<item>`. `RELEASE_UPDATE core@2026.10.0` lets one held `.0` through.
- Nothing in Home Assistant changes; the list lives in this App's own storage.

**After-update check** (new in 0.7.3, owner decision 2026-10-08). Before every Core, OS or App update the App notes
which integrations are running. `after_update_minutes` (default 10) after a successful update it compares:

1. If everything that ran before still runs, it posts one line on the tracking issue.
2. If an integration that ran before now fails to start (as GoveeLife did after Core 2026.10.0), your phone asks
   **Reload** and the App reloads exactly those integrations and checks them.
3. If any still fail, your phone asks **Undo the update**:
   - Core goes back by restoring Home Assistant only from the full backup the update took. Anything changed in
     Home Assistant since then is lost.
   - An App is restored from the App backup the update took.
   - The undone version goes on the hold list.
   - Reject keeps the update.
4. An OS update cannot be undone. The App reports it, and the problem watch keeps offering fixes.

Undo is offered for `undo_hours` (default 24). While a check is open, no new update starts, and the problem watch
does not ask about the same integrations, so you get one prompt, not two.

Skipped versions (Skip in Home Assistant's update dialog), items that update themselves, and items listed in
`update_hold` or the hold list are never asked. Reject is final, as for Apps. Results go to the tracking issue as `UPDATE_HACS` or
`UPDATE_FIRMWARE`.

**Problem alerts** (`issue_checks`, on by default). Every check it looks, without changing
anything, at: Home Assistant **Repairs**, Supervisor health and its own suggested fixes, **Apps**
that crashed or stopped although they start at boot, **integrations** that failed to start or keep
retrying, **errors in the log**, **disk space** and **backups** (warns if no backup including Home
Assistant in 7 days). A problem must be seen twice in a row before you hear about it.

New in 0.5.0, all read-only:
- **Backup copy off the Pi**: warns when no backup in the last 7 days is stored anywhere but the Pi
  (Home Assistant Cloud, Google Drive, OneDrive or a network share count). A dead SD card or SSD
  takes the copies on the Pi with it.
- **Low batteries and offline devices**: about every 6 hours it reads device states and lists devices
  at 15% battery or less (cleared above 25%) and devices whose every entity has been unavailable for
  3 days. Phones and tablets are skipped. Only the device name and the number go into the summary.
- **Disk filling too fast**: it notes the free space twice a day and warns when, at the current rate,
  the disk would be full within 30 days (urgent within 7). Home Assistant does not report the size of
  its history database itself; that database is the usual cause, and the advice says how to shrink it.

New in 0.5.1, all read-only:
- **Re-login needed**: an integration waiting for you to log in again is named, with the steps. No reload is
  offered for it, because a reload cannot fix a login.
- **Liveness ping failing**: when the optional outside "home reachable" ping has failed for 30 minutes.
- **App stopped for 30+ days**: said once, so you can uninstall Apps you no longer need. Apps kept stopped on
  purpose can be ignored, and nothing is removed.
- **Backup size jump**: the newest full backup is much smaller than usual (something left out) or much bigger
  (something growing fast).

New in 0.5.2 (Recovery Report), all read-only:
- **Right verdict for each restart**: Connection Forensics' clean/unclean verdict is used only once it was
  written for this restart. The report waits up to 5 minutes, then says "verdict not yet available". Installing
  Connection Forensics does not send a push.
- **PLANNED or UNPLANNED_CLEAN**: a clean restart caused by a House Brain Deployer install shows its request id.
- **Restart ledger**: restarts in the last 30 and 90 days by kind, and the mean time between unplanned failures
  (crashes, unexpected reboots, power cuts). A text block is ready to paste into the stability ledger.
- **Mesh rejoin**: how long Zigbee (ZHA), Z-Wave and Matter devices took to come back after a restart.

New in 0.5.3 (fixes for two false warnings seen on 2026-10-05), all read-only:
- **Google Drive Backup App counts as off the Pi**: if you use the Home Assistant Google Drive Backup App, its
  copies in Google Drive count. It reads only that App's status entity (`sensor.backup_state`): its state, the
  date of the newest backup in Google Drive and how many are there. If that App reports an error, you are told
  to open it (usually Google needs you to sign in again).
- **Backup sizes compared like with like**: a settings-only backup (a few MB) is no longer compared with a full
  backup of everything (GB). Each kind is compared with earlier backups of the same kind.
- **Broker check**: the House Brain AI can ask which Maintenance Broker version is live (`CHECK_BROKER`). It reads
  the Broker's public health page only, needs no approval and changes nothing. A Scout run reports it too.

**Ask me now** (new in 0.6.5, owner decision 2026-10-07). When an update ask timed out (you did not tap in time),
any House Brain chat (Claude, ChatGPT, Grok, Gemini, Codex) or you can file an `ASK_UPDATE_NOW` request and your phone asks again on
the next poll instead of after the re-ask wait. It works for a Core, OS or App update and only for the exact
update this App already asked you about that is still waiting right now (Core/OS: its review is already on the
tracking issue and the hour for the release-note check has passed). Anything else is refused and nothing is
asked: another version, an update you rejected (Reject is final, also here), a version that failed before, this
App itself, the OS while a Core update waits, or an unhealthy Home Assistant. After Approve exactly the same steps
run as always (backup, update, health watch, automatic restore). It counts against
`max_approval_requests_per_day` like every ask. The request (on the `requests_branch`, default `maint/requests`, file
`maint/requests/<id>/manifest.json`):

```json
{"schema": "house_brain_maintenance_request.v1", "request_id": "ask-core-2026-10-07a",
 "job": "ASK_UPDATE_NOW", "requested_by": "claude", "tracking_issue": 454,
 "params": {"kind": "core", "version": "2026.9.4"}}
```

`kind` is `core`, `os` or `app`; for `app` add `"slug"` (for example `"core_mosquitto"`) and use the version it
updates **to**. The result is posted on the tracking issue.

- Serious problems: one notification right away, with what to do.
- Safe fixes (only for Apps set to start at boot; an App set to start by hand, such as the
  AquaRite bridge, is only reported and never restarted): restart or start that App, reload that integration, or let Home Assistant run its own
  repair. The notification has **Approve** (Face ID) and **Reject**; nothing runs without Approve.
  Afterwards you get **fixed** or **not fixed** with next steps. Reject means it will not ask again
  while that problem stays.
- Everything else, and problems that cleared, come in one summary at `digest_hour` (8 am).
- Each problem is reported once; details go to the tracking issue (`report_issue`) with passwords,
  tokens, IP, MAC and e-mail addresses removed.
- It never reboots, stops, removes or updates anything as a fix, and never touches itself.
- **Pool Wi-Fi extender** (`extender_plug_entity`): when iAquaLink has been offline for 10 minutes it can
  offer to power-cycle the extender's smart plug (off 10 s, on, checked). Only that plug, only after your
  Approve, at most 3 times a day. Leave the option empty to get the manual steps instead.
- **UPnP router not found** (0.4.1): when the UPnP/IGD integration can't find the router, it reads, for a few
  seconds and without changing anything, what Home Assistant hears announced on the network. The alert then says
  the real cause and the right steps: the router stopped announcing UPnP (turn UPnP on / restart the router), the
  router came back with a new identity (add the discovered one, delete the old), Home Assistant hears nothing at
  all (its network adapter), or the router is heard again (then a reload is offered). Only counts are reported.
  For this house a router that drops out and comes back by itself is known (0.6.2): the alert says so and only
  asks for a router restart if it lasts more than 30 minutes.

**Recovery Report** (`recovery_report`, on by default; 0.3.0). When Home Assistant comes back from
being down, or a network or power outage ends, the **Maintenance** page (sidebar) shows:

- how long it lasted;
- the likely cause and how sure it is (planned update or restart, power cut with the UPS running
  out, unexpected reboot, crash with the last error lines, network only, or "could not tell");
- a checklist (smoke/CO sensors first, UPS recharging, thermostat, Sense, solar);
- what to do, and the earlier outages.

You also get one notification on your phone once the internet is back. Short planned restarts,
such as a Deployer install, are only shown on the page. If the smoke/CO sensors are still not
back after 15 minutes, every phone in `safety_notify_services` gets an urgent notification.
Tap **Got it** on the page when you have read it. It only reads; it never fixes or restarts anything.

**Scout jobs** (proposed by the House Brain AI, each needs your approval):

| Job | What happens |
|---|---|
| Run the Scout once | The Inventory Scout lists your Apps and sends the list to the Maintenance Broker. |
| Rotate the Scout key | A new key is made here (only its fingerprint leaves this Home Assistant). The Scout switches to it only after the Broker already accepts it, so it never stops working. |
| Check the Broker (0.5.3, no approval) | Reads the Broker's public health page and reports its version. Nothing is sent, started or changed. |
| Install a House Brain App source (0.7.4, one approval) | See "One-tap install of a House Brain App" below. |
| App log window (0.5.4, no approval) | Reads one House Brain App's log for one boot (this boot or up to 5 before), keeps only the lines in a UTC time window (at most 6 hours) that contain one of up to 5 words, removes secrets, addresses and long ids, and posts at most 200 lines to the tracking issue. Only Apps named `local_house_brain_…` or `<store>_house_brain_…` that are installed; never this App. Nothing is written to any App. |

## What it can never do

- Update Home Assistant Core or the OS without your Approve, automatically, or to another version than
  the one you approved; update Core and the OS together.
- Update the Supervisor, or update itself.
- Reboot or shut down the Pi. The only exceptions (0.8.3, owner pop-up 2026-10-11 "Ask me, then reboot"): the
  restart that finishes an OS update, after its own Approve, and the restart into the previous OS after you
  approve a rollback. It never shuts the Pi down.
- Update more than one App at a time, or any App without a backup of it first.
- Uninstall, stop or rebuild Apps, change other Apps' settings (except one Broker key option of the Observer,
  Inspector, AI Access and Scout during an automatic key change, from 0.8.0), make
  full backups except right before a Core/OS update you approved, or restore anything but the one App
  it just updated or, after a failed Core update, Home Assistant only from the backup it just made.
- Keep, log or report other Apps' settings or passwords (it reads only versions and states).
- Touch files, restart Home Assistant (except as part of the Core update or restore above), or control
  any device. The one exception (0.7.4): after your Approve of a one-tap install, the carrier's own script writes
  the new source of the Inspector, Observer or Inventory Scout into that one App folder and keeps its backup under
  `/share/hb91-app-update`. Nothing else in `/addons` or `/share` is written, and `/config` is never touched.

## One-tap install of a House Brain App — from 0.7.4

Owner decision 2026-10-10 ("Approve design, build it"). Before 0.7.4 a new Engineering Inspector, Engineering
Observer or Inventory Scout version needed six steps: download the carrier, upload it in Studio Code Server, paste a
command, refresh the store, tap Update, start the App. Now an assistant files one `STAGE_LOCAL_APP_SOURCE` request
and your phone asks once:

> Install Inspector 0.2.4 → 0.2.5 (source from GitHub `1e54ebb`, checked). Approve / Reject

Before it asks, the App checks everything and stops **without asking** when anything is off:
- the App must be one of those three local Apps, installed at the version the request names;
- the carrier must be a `deploy/carriers/hb<n>-<name>.tar` file at a commit on `main`, at most 1 MiB, with exactly
  the SHA-256 in the request;
- the carrier's own `check` must say the App folder holds exactly the old version and is ready.

After your Approve: the carrier's own script puts the new source in place (verified backup of the old one under
`/share/hb91-app-update`, one atomic swap), the App store is refreshed, then the normal App update runs (backup of
that App only, update, health watch, restore on failure) without a second ask. The App is started again only if it
was running before. If anything fails after the source was put in place, the carrier's `undo` puts the old source
back and the store is refreshed. The result is posted on the tracking issue.

Reject is final for that version. No answer changes nothing; ask again with a new request. It counts toward
`max_approval_requests_per_day`. The App needs the folder permissions `addons` and `share` (read-write) for this;
its AppArmor profile lets it write only those three App folders and the backup folder.

## Automatic Broker keys — from 0.8.0

Owner decisions 2026-10-11 (K1–K4): "Once it's set up, I want to be involved as little as possible, especially with
things like swapping out keys."

**What changes by itself.** The Engineering Observer, Engineering Inspector, AI Access and the Inventory Scout each
use a key to talk to the Maintenance Broker. This App changes those keys by itself, then its own rotation key:
- one App per night, inside the automatic-update window;
- each one every `key_rotate_days` (default 90).

**One change, step by step:**
1. **New key.** The App makes a new random key (384 bits) and keeps it in `/data/keys.json` (readable by this App
   only). It never appears in a log, on GitHub or on your phone; only a short fingerprint does.
2. **Broker accepts both.** The Broker accepts the new key next to the old one. Only this App's rotation key can ask
   for that; a stolen App key cannot.
3. **Switch.** The App's key option is set to the new key; every other option stays exactly as it was. The App is
   restarted, but only if it was running. A stopped App is never started; it uses the new key at its next start. The
   Scout is never restarted.
4. **Check.** The Observer and Inspector must actually use the new key within 10 minutes. AI Access and the Scout are
   checked against the Broker.
5. **Commit.** The Broker makes the new key the current one. The old key keeps working for 24 hours (7 days after
   the very first change), so nothing that still uses it is cut off mid-change.

**If something fails.** Anything before step 5 is undone:
- the old key goes back into the App (restarted again only if it was running);
- the Broker forgets the new key;
- the old key works throughout.

One failed night is simply tried again the next night. Two failures in a row send you one message and appear in the
morning summary. If something besides the App still uses an old key after a change, you get one message too.

**What you are asked.** Once, in the daytime, before the very first change: "Start automatic key changes?"
- Approve: you are never asked again.
- Reject: nothing changes and it never asks again.
- No answer: it asks again the next day.

**Setup (one time).**
1. At its first start the App posts its rotation key's fingerprint on the tracking issue (`KEYS_SETUP`).
2. An assistant puts that fingerprint into the Broker's config.
3. You tap Promote on the Broker update.

Until then nothing changes.

**Emergency.** An assistant can file `ROTATE_KEYS_NOW` (`role`: one key or `all`) when a key may have leaked. The
change then runs at once, one App after another, without asking.

**Status.** The status sensor shows each key's age, last result and next change.

**Permission (standing, owner K3/K4).** During a key change only, for those four Apps only, this App may:
- read that App's options;
- write them back with the same option names;
- restart the Observer, AI Access or Inspector right after their key changed, or to undo that change.

## Cloudflare approvals on your phone — from 0.8.2

When a chat files a Cloudflare control request (for example "promote the Broker"), the control Worker posts its plan
on GitHub #454 and waits for your Approve. From 0.8.2 this App reads #454 every poll and sends you an ordinary Home
Assistant notification:

1. **"Cloudflare: approve?"** with the Worker, the planned steps and the time left. Tap **Open Approve page**
   (or the notification). Cloudflare may ask you to sign in, then tap **Approve** or **Reject** on that page.
2. When that request ends, a second notification replaces the first: done, failed, rejected, expired, and so on.

- **Safe by design.** This App only reads GitHub and notifies. It cannot approve anything; the tap on the
  Cloudflare page (behind Cloudflare Access) is the only approval.
- **Links can't be faked.** Only your account's control results count, and the button always opens the control's
  own address for that request id. Nothing in a comment chooses where the button goes.
- **No old news.** A request is announced only while it is still waiting (6 hours). Requests that end before
  the next poll are not announced.
- **Cost.** One GitHub read per poll (288 a day at the default 5 minutes), $0.
- **Off.** Set `cf_control_issue` to `0`.

## Settings

| Option | Meaning |
|---|---|
| `github_token` | Optional from 0.6.3: only needed until the GitHub App is connected (see **GitHub connection**). Its own fine-grained token: this repository only, Contents read-only, Issues read and write. |
| `github_auth` | Leave `auto` (GitHub App when connected, else the token). `github_app` = never use the token; `pat` = never use the GitHub App. |
| `retire_old_token` | Default `true`: after 24 hours of GitHub App sign-ins, revoke the old `github_token` on GitHub automatically (from 0.6.6). |
| `notify_service` | Your phone, for example `mobile_app_my_iphone`. |
| `owner_username` | Your Home Assistant user name. Only this user can approve. |
| `dry_run` | Starts as `true`: updates are reviewed and reported, nothing is asked or changed. |
| `update_mode` | `ask` (default): every update asks you. `auto_low_risk`: bug-fix updates reviewed as low risk install at night without asking, but only after 3 updates you approved went well. Everything else still asks. |
| `auto_window_start_hour`, `auto_window_end_hour` | The night window for automatic installs (local time, default 2 to 5). |
| `auto_wait_days` | Waiting period for automatic installs (default 3 days, 0 to 30): a low-risk update installs by itself only after this App first saw it offered this long ago, so other people find a bad release first. While it waits, it is not asked. Updates you approve install when you approve them. |
| `update_check_minutes` | How often it looks for App, Core, OS, HACS and firmware updates (default 60). |
| `entity_updates` | From 0.7.0: HACS and device-firmware updates (default `true`). `false` = Apps, Core and OS only. |
| `firmware_wait_days` | From 0.7.0: a firmware version is asked only after it has been offered this long (0–90, default 30). |
| `update_hold` | From 0.7.0: update items never to ask about or install, for example `update.backyard_zen_16_multirelay_firmware`. |
| `hold_core_dot_zero` | From 0.7.3: hold every Core `YYYY.M.0` until a later patch is offered (default `true`). |
| `after_update_minutes` | From 0.7.3: after-update check delay in minutes (0–120, default 10; `0` = off). |
| `undo_hours` | From 0.7.3: how long the after-update check offers Undo (1–72, default 24). |
| `key_rotation` | From 0.8.0: automatic Broker key changes (default `true`; see **Automatic Broker keys**). |
| `key_rotate_days` | From 0.8.0: each Broker key is changed this often (30–365, default 90). |
| `keep_old_deployer` | From 0.8.1: keep the stopped old local Deployer and never ask to remove it (default `true`; owner decision 2026-10-11). `false` = ask once, 7 days after the switch, as before. |
| `cf_control_issue` | From 0.8.2: the GitHub issue where the Cloudflare control posts its requests (default `454`; `0` = no Cloudflare pushes). See **Cloudflare approvals on your phone**. |
| `cf_control_url` | From 0.8.2: the Cloudflare control address the button opens. Leave empty: the App uses the control on the same Cloudflare account as the Broker the Scout reports to. |
| `upgrade_checks` | From 0.8.4: known issues and release notes for your integrations in Core update reviews and asks (default `true`; `false` = no reads). |
| `health_check_minutes` | How long it watches an App after updating it (default 3). |
| `update_reask_hours` | From 0.6.5: an App, Core or OS update ask you did not answer is asked again after this many hours (1–48, default 6; was fixed at 24). From 0.7.2 the same wait applies to fix asks and the Deployer switch-over ask (were fixed at 24 hours). Reject is never asked again. |
| `report_issue` | Optional GitHub issue number for result reports (0 = none). |
| `scout_slug`, `observer_slug` | The Scout and Observer Apps. Leave the defaults. |
| `clear_freeze_for` | After a `FAILED_MANUAL` result the App pauses until you enter that request id here. |
| `recovery_report` | The Recovery Report (default on). |
| `safety_notify_services` | Phones for urgent smoke/CO "not back" notifications, for example `mobile_app_my_iphone` (up to 4). Empty = only `notify_service`. |
| `liveness_url`, `liveness_key` | Optional off-site "still alive" ping to your own liveness Worker (both or neither; empty = off). The key is a password field. |
| `liveness_interval_minutes` | How often the ping is sent (2–30, default 2; the Worker's free tier allows about 1,000 writes a day). |

To pause everything: set `update_mode` to `ask` and simply do not approve, or stop this App.

## Approvals

The push shows Approve and Reject; Approve needs Face ID or your passcode. Tapping the push
opens the approval page; opening it never approves. Only your user counts. No answer within
the timeout counts as No.

From 0.7.2, when an update ask (App, Core or OS) ends with no answer, one more push without buttons tells you:
"Missed: update Home Assistant Core 2026.10.1. I'll ask again at 14:05 (local), or tap Ask me again on the
Maintenance page." It is sent once per missed ask, never after Reject and never in dry run.

## Updates waiting and "Ask me again" — from 0.7.2

The Maintenance page lists the updates that are waiting for you: asked and not answered (`waiting_reask`, with the
time it asks again), or a Core/OS review that is posted and waits for the release-note check hour
(`waiting_review_lead`). Rejected updates and versions that failed before are not listed. Each has an
**Ask me again** button.

**Ask me again approves nothing.** It only puts that update first in line: on the next poll (`poll_seconds`, default
5 minutes) your phone asks again, through exactly the same path as an `ASK_UPDATE_NOW` request, so every rule still
applies (Reject is final, the release-note hour for Core/OS, Core before the OS, a healthy Home Assistant, versions
that failed before, never this App itself) and the usual backup, update, health watch and automatic restore follow
your Approve. It counts against `max_approval_requests_per_day`; when today's limit is reached nothing is asked and
the result says so. Only your Home Assistant user can press it, only through Home Assistant (ingress), one at a
time; an unused one lapses after one hour. The result is posted on `report_issue` like other updates
(`asked_via: "page"`).

The status sensor `sensor.house_brain_maintenance_status` carries the same list: `pending_updates` (how many) and
`waiting_updates` (up to 10: kind `app`/`core`/`os`, slug for Apps, version, `asked_at`, `next_reask_at` in UTC,
state). Until 0.6.6 `pending_updates` was always 0.

## GitHub connection (no token to renew) — from 0.6.3

Open the Maintenance page and tap **GitHub connection** at the bottom. From 0.6.6 it takes three taps, also on
the iPhone:

1. **Connect to GitHub**: a new window opens and goes to GitHub with a ready-made private App "House Brain Maintenance Bot" (this
   repository only; Contents: Read, Issues: Read and write).
2. **Create GitHub App** (the green button). GitHub sends you back to this App's hand-off address, which keeps the
   key inside this App (you never see it) and opens the install page.
3. **Install**: only **home-assistant-whole-home** is selected already. A page says "Done"; go back to Home
   Assistant. Within one check the GitHub connection page says "signed in with the GitHub App".

The hand-off address is this App's port 8097 at the same address you use for Home Assistant (at home or over
Tailscale). It exists only while a Connect is pending (30 minutes after the page offers it, at most one hour after
you tap it) and answers nothing but GitHub's one-time return. Keep port 8097 in the App's **Network** section.

**The old token retires itself:** once the GitHub App has signed every call for 24 hours, this App revokes its old
hand-made token on GitHub (GitHub e-mails you that it was revoked) and never uses it again. Nothing to delete by
hand; clearing `github_token` in Configuration is optional. To keep the old token, set `retire_old_token` to
false before then.

If Connect does not work (for example port 8097 is not reachable): on GitHub open
**Settings › Developer settings › GitHub Apps › House Brain Maintenance Bot › Generate a private key**; the `.pem` file downloads. On this
page use **Upload key file** and pick it (type the App ID shown at the top of the GitHub page if asked).
Never paste the key anywhere else, and never screenshot the Configuration tab.

**Disconnect** deletes the key from this App only; delete the App on GitHub as well to stop it everywhere.
Passes last one hour and are limited to this repository; about 24 a day, at most 30 (then it stops until midnight
UTC; $0, GitHub Free).

## Credential warnings — from 0.6.3

Once a day this App checks GitHub sign-in for itself and for the Deployer (from the Deployer's status, never its
token). While an old hand-made token is still in use, it warns **30, 14, 7 and 1 days** before it expires: one push
per step, also in the morning summary and on the tracking issue. A failing GitHub App sign-in and "not connected"
are warned too. Once the GitHub App works, nothing is said about the old token any more.

It also reads the relay Worker's weekly self-test of its GitHub dispatch key (option `relay_credential_url`: leave it
empty and it is derived from `liveness_url`; `off` turns it off) and uses the same ladder; a failing key or a stopped self-test is a warning
with the exact fix path.

## Backup password watch — from 0.6.6

Backups hold every App's data, including the GitHub App keys. When the newest full backup (not this App's own
pre-update backups) is **not password-protected**, this App warns once with the fix: Google Drive Backup → Open web UI
→ Settings → backup password. It reads only Home Assistant's "protected" flag, never a backup's contents or name.

## Deployer switch to the store — from 0.6.6

When the store **House Brain Deployer** is installed next to the old local one, this App asks once: "Switch the
Deployer to the store?". After **Approve** it:

1. copies the old Deployer's settings into the store Deployer (on the Pi; the old token too, which the new one
   retires by itself after its GitHub App works);
2. sets the old Deployer to start manually and stops it (it is kept);
3. starts the store Deployer and waits until it reports in (up to 20 minutes; an old Deployer counts as active for 15
   minutes after its last report).

Any problem puts the old Deployer back automatically. It never runs while the Deployer reports `DEPLOYING`. Seven
days later it asks separately whether to remove the stopped old Deployer. From 0.8.1 it keeps the old Deployer and does not ask while `keep_old_deployer` is on (the default; owner decision 2026-10-11). Then connect the store Deployer to GitHub
on its page (3 taps).
