# House Brain Maintenance

This App keeps your Home Assistant Apps up to date safely and tells you about problems with a
suggested fix. It never changes anything without a rule you chose: every update and every fix
waits for your approval on your iPhone, unless you switch on automatic installs of low-risk
bug-fix updates at night.

## What it does (0.4.1)

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

If you tap Reject, that version is not offered again. If you do not answer, it asks again the
next day.

**Problem alerts** (`issue_checks`, on by default). Every check it looks, without changing
anything, at: Home Assistant **Repairs**, Supervisor health and its own suggested fixes, **Apps**
that crashed or stopped although they start at boot, **integrations** that failed to start or keep
retrying, **errors in the log**, **disk space** and **backups** (warns if no backup including Home
Assistant in 7 days). A problem must be seen twice in a row before you hear about it.

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

## What it can never do

- Update Home Assistant Core, the OS or the Supervisor, or update itself.
- Update more than one App at a time, or any App without a backup of it first.
- Uninstall, stop or rebuild Apps, change other Apps' settings (except the Scout's key), make
  full backups, or restore anything but the one App it just updated.
- Keep, log or report other Apps' settings or passwords (it reads only versions and states).
- Touch files, restart Home Assistant, or control any device.

## Settings

| Option | Meaning |
|---|---|
| `github_token` | Its own fine-grained token: this repository only, Contents read-only, Issues read and write. |
| `notify_service` | Your phone, for example `mobile_app_my_iphone`. |
| `owner_username` | Your Home Assistant user name. Only this user can approve. |
| `dry_run` | Starts as `true`: updates are reviewed and reported, nothing is asked or changed. |
| `update_mode` | `ask` (default): every update asks you. `auto_low_risk`: bug-fix updates reviewed as low risk install at night without asking, but only after 3 updates you approved went well. Everything else still asks. |
| `auto_window_start_hour`, `auto_window_end_hour` | The night window for automatic installs (local time, default 2 to 5). |
| `update_check_minutes` | How often it looks for updates (default 60). |
| `health_check_minutes` | How long it watches an App after updating it (default 3). |
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
