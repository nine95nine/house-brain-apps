# House Brain Maintenance

This App keeps your Home Assistant Apps up to date safely. It never changes anything without a
rule you chose: every update waits for your approval on your iPhone, unless you switch on
automatic installs of low-risk bug-fix updates at night.

## What it does (0.1.2)

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

To pause everything: set `update_mode` to `ask` and simply do not approve, or stop this App.

## Approvals

The push shows Approve and Reject; Approve needs Face ID or your passcode. Tapping the push
opens the approval page; opening it never approves. Only your user counts. No answer within
the timeout counts as No.
