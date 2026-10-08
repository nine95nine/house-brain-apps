# House Brain Maintenance

This App keeps your Home Assistant Apps up to date safely and tells you about problems with a
suggested fix. It never changes anything without a rule you chose: every update and every fix
waits for your approval on your iPhone, unless you switch on automatic installs of low-risk
bug-fix updates at night.

## What it does (0.7.0)

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
5. **OS:** it updates the OS to exactly the approved version; the Pi reboots. When this App starts again it
   reads the OS version: the new one is DONE; the old one means the new OS could not boot and the Pi fell
   back to the previous version by itself (A/B), reported as ROLLED_BACK. Core health is checked after the
   reboot; there is no other automatic restore for the OS, so if Core is not healthy it pauses and tells you.

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

Skipped versions (Skip in Home Assistant's update dialog), items that update themselves, and items listed in
`update_hold` are never asked. Reject is final, as for Apps. Results go to the tracking issue as `UPDATE_HACS` or
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
any House Brain chat (Claude, ChatGPT) or you can file an `ASK_UPDATE_NOW` request and your phone asks again on
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
| App log window (0.5.4, no approval) | Reads one House Brain App's log for one boot (this boot or up to 5 before), keeps only the lines in a UTC time window (at most 6 hours) that contain one of up to 5 words, removes secrets, addresses and long ids, and posts at most 200 lines to the tracking issue. Only Apps named `local_house_brain_…` or `<store>_house_brain_…` that are installed; never this App. Nothing is written to any App. |

## What it can never do

- Update Home Assistant Core or the OS without your Approve, automatically, or to another version than
  the one you approved; update Core and the OS together.
- Update the Supervisor, or update itself.
- Reboot or shut down the Pi; the only reboot is the one the OS update you approved does.
- Update more than one App at a time, or any App without a backup of it first.
- Uninstall, stop or rebuild Apps, change other Apps' settings (except the Scout's key), make
  full backups except right before a Core/OS update you approved, or restore anything but the one App
  it just updated or, after a failed Core update, Home Assistant only from the backup it just made.
- Keep, log or report other Apps' settings or passwords (it reads only versions and states).
- Touch files, restart Home Assistant (except as part of the Core update or restore above), or control
  any device.

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
| `health_check_minutes` | How long it watches an App after updating it (default 3). |
| `update_reask_hours` | From 0.6.5: an App, Core or OS update ask you did not answer is asked again after this many hours (1–48, default 6; was fixed at 24). Reject is never asked again. |
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
days later it asks separately whether to remove the stopped old Deployer. Then connect the store Deployer to GitHub
on its page (3 taps).
