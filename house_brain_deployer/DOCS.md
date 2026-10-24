# House Brain Deployer

Installs Home Assistant **package files** that an AI (Claude, ChatGPT, Codex, Grok or Gemini) has requested in the
House Brain GitHub repository — but only after **you** approve on your iPhone, twice:

**How to answer:** tap the notification. The Home Assistant app opens the **Deployer** page with a big
**Approve** and **Reject** button. Opening the page does nothing by itself. (Press-and-hold on the
notification also shows Approve / Reject.) One-time setup: on this App's **Info** tab turn on
**Show in sidebar**, so the notification can open the page.

1. **Approve deploy?** — nothing changes until you tap Approve.
2. **Approve restart?** — the new files are installed and config-checked; Home Assistant restarts
   only when you tap Approve.

No answer = No. On failure, the old files are restored while the Deployer still holds its write
lease (and, if Home Assistant was already restarted, it is restarted once more on the old files).
If the write lease is lost, it stops touching files and keeps the recovery intent for a fresh,
authorized recovery. You get a result notification.

## Lookups (read-only)

An AI can also ask to **look something up** (for example current sensor readings). You get
**"approve lookup?"** showing the exact read-only template; after you approve, the result is posted to
the GitHub issue. Lookups can never change anything.

From 0.3.29 a lookup can also read the **Z-Wave settings** (configuration parameters) of one device, named by
one of its entities. The approval message says so and names the entity. The Deployer asks Z-Wave JS for the
values it already holds (no message is sent to the device, nothing is written) and posts only each parameter's
number, name, value, unit and default.

From 0.3.34 a lookup can also ask for two more read-only parts. Each one is named in the approval message, and
nothing is read before you approve:

- **History** (`history_entities`, up to 16 entities, `history_days` 1–7): one Home Assistant history read,
  posted as compact state changes (code `HST1`).
- **Sense device tags** (`sense_device_tags: true`): one read of Sense's device list using Home Assistant's own
  Sense login. The Deployer reads only that login from Home Assistant's config entries (the only private-store
  file it may read), never logs in, refreshes or logs out, and posts device names, icons and a fixed list of
  tags (type, created and first-used dates, listed/merged/hidden flags, Sense's own name hints). No ids and no
  token leave the house. A Sense name or type is a hint, not proof of what a device is.

## Stop recording busy entities (0.3.31)

A package may contain **only** `recorder:` → `exclude:` → `entities:` and/or `entity_globs:` lists. Home Assistant
adds those to your recorder exclude list, so the named entities stop getting new history rows; **nothing is
deleted** and how many days are kept is unchanged. The approval message lists them ("stops recording N entities
(deletes nothing)"). Any other recorder setting (days kept, include, database) is still refused. To undo, use
**Undo this install** or retire the file.

## House Brain Companion set-up (0.3.30)

Two fixed requests set up the House Brain Companion (the free explain-only AI) without you typing anything:

- **Companion key:** the Deployer makes a random key on the Pi and keeps it only in its own private storage. It
  posts just the key's fingerprint (a SHA-256), which the Companion Worker uses to recognise the key. Nothing in
  Home Assistant changes, so this one does not ask you.
- **Companion set-up:** you get **"set up the Companion?"**. After you approve, the Deployer adds the llama.cpp
  integration for the Companion with that key, turns its device control **off** (and checks that it is off), and
  adds a voice assistant called **House Brain Companion**. If anything fails, it deletes the entry it made. The model
  and assistant name are fixed in the Deployer, and it accepts only the one Companion address it was built for
  (checked by fingerprint); a request cannot change them. Then, in the Home Assistant app:
  Assist, choose "House Brain Companion".

## What it can and cannot do

- Generic deployments write only `/config/packages/<name>.yaml` files and keep the old copy as `<name>.yaml[.tag].bak`.
- 0.3.9 adds one fixed repair: remove an obsolete HTTP block from `configuration.yaml` after confirming the migrated Network settings already match. The AI receives hashes, never the configuration contents. You approve inspection, then separately deployment and restart. Unsupported files or mismatched settings are refused.
- 0.3.24 adds one fixed tidy: rename `house_energy_brain_v1_1.yaml copy.bak` (the space in the name) to `house_energy_brain_v1_1.yaml.copy.bak`. Same bytes, one Approve tap, backup and checks first, no restart. A request cannot name any other file.
- Cannot touch `secrets.yaml`, `.storage`, dashboards or other Apps. No arbitrary root configuration editor or HTTP settings mutation is provided.
- 0.3.30 adds the Companion set-up above: one integration (llama.cpp), one URL, one model, no control API, one voice assistant. It cannot add or change any other integration, agent or assistant.
- Never turns anything on or off. It only sends you notifications, makes a partial backup, runs the
  configuration check and (after your second tap) restarts Home Assistant.
- Opens no port on your network. Its approval page is reachable only inside the Home Assistant app
  (ingress), and only your account can decide.
- Tapping an approval push opens this Deployer's own page (from 0.3.14 also for a store install). If the App
  cannot tell its own name, the push says "Use notification actions or Open Web UI" and the Approve/Reject
  buttons on the push still work.

## Options

For a fresh install, fill in both `notify_service` and `owner_username` before saving or starting.
They must be your actual phone notification service and Home Assistant login username; they are
required even when using GitHub App authentication. From 0.3.10, leave `github_token` as the empty
string when connecting through the GitHub App. Keep **Show in sidebar** enabled on the active
Deployer so a notification tap can open its approval page. Existing saved options stay unchanged.

| Option | What to enter |
|---|---|
| `github_repo` | Leave as `nine95nine/home-assistant-whole-home` |
| `github_token` | Optional from 0.3.6: only needed until the GitHub App is connected (see **GitHub connection**). A fine-grained token: only this repository; Contents: Read-only; Issues: Read and write |
| `github_auth` | Leave `auto` (GitHub App when connected, else the token). `github_app` = never use the token; `pat` = never use the GitHub App |
| `retire_old_token` | Default `true`: after 24 hours of GitHub App sign-ins, revoke the old `github_token` on GitHub automatically (from 0.3.7). |
| `requests_branch` | Leave as `deploy/requests` |
| `source_ref_allowlist` | Leave as is |
| `notify_service` | Your phone's notify name, e.g. `mobile_app_my_iphone` (without `notify.`) |
| `owner_username` | Your Home Assistant login username |
| `dry_run` | `true` = practice mode, nothing changes. Switch to `false` only when asked |
| `poll_seconds` | How often it checks GitHub (default 300) |
| `max_approval_requests_per_day` | Safety cap on **installs** asked per 24 h (default 4). Restart approvals do not count; read-only lookups have their own allowance of the same size. When a request is held by it, the Deployer page offers **Allow more today** (owner only, until midnight) |
| `approval_timeout_minutes` | How long a deploy (or lookup) request waits for your tap: 2–720 min, default 360 (6 h). No answer = Reject (from 0.3.8 the AI may ask again up to twice; see **Asked again after no answer**). While it waits, other requests queue behind it. A Home Assistant restart while waiting does not end the wait: the approval page still works, and the push buttons come back within about 30 s |
| `restart_approval_timeout_minutes` | How long the restart approval waits: 2–30 min, default 10. Kept short on purpose, because the new files are already in place while it waits |
| `require_phone_unlock` | Keep `true` (Approve needs Face ID / passcode) |
| `clear_freeze_for` | Leave empty. Only used if the App tells you it is frozen |
| `return_to_previous_history` | Leave `false`. Emergency switch (from 0.3.23, draft #985): when `true`, the next start sets the moved-history selection aside and uses the previous history, which was never changed. Turn it back to `false` afterwards; **Select** refuses while it is on |

## Status

Results are posted as a comment on the GitHub issue named in the request, so both AIs can read them.

**Why is nothing happening?** (0.3.1) Three places always show it:

1. **The Deployer page (sidebar).** When nothing is waiting, it lists every current reason
   (for example "daily approval-request limit is reached (10 of 10 …); it will be asked
   automatically after 3:05 PM") with what to do. It also shows the last GitHub check time and the
   last result, or "All clear".
2. **`sensor.house_brain_deployer_status`.** The state is `OK`, `HELD` or `ERROR`. Attributes:
   `reasons`, `last_check`, `next_check`, `approval_requests_last_24h`, `approval_request_limit`,
   `last_result`.
3. **The Log tab.** It shows a `settings:` summary at start and each reason once when it appears
   (and "resolved: …" when it clears).

A held request is also announced once by push ("House Brain Deployer: waiting") and once on its
GitHub issue. An error that lasts about 15 minutes (bad token, no internet, wrong notify service …)
sends one push ("House Brain Deployer: problem") until it clears.

## Updating or reconfiguring the Deployer (0.3.5)

Update, restart or save the Configuration of this App **only while its status is not DEPLOYING**. Each of
those stops the App. During an open install that means the install is rolled back on the next start.
While an install is open, the status entity carries a `warning` attribute and the approval page shows
**"Install in progress"**.

If the App is stopped anyway, 0.3.5 stops within seconds instead of being killed. It leaves the install
journal as it is, and the next start's recovery rolls back or finishes the health check (never anything
more). The #561 result then says why the install was interrupted: the App was stopped, the App ended
without a stop signal, the host rebooted, or an internal error. It also adds the interrupted phase, how
old the journal was and whether Home Assistant had restarted.

## Undo a recent install (0.3.3)

When nothing is waiting, the Deployer page lists recent installs with **Undo this install**.

1. Tap it. Only your login and the current page work. The Deployer prepares the undo within about 5 minutes.
2. It then asks **Approve restart?** as usual. Reject, or no answer, leaves the install as it is.

What Undo does and does not do:
- It puts back exactly the files that install replaced or retired, from the `.bak` copies.
- It keeps the undone version as `<file>.undone_….bak`.
- If a file was changed since that install, or a newer install touched it, the button is not shown.

## Asked again after no answer — from 0.3.8

If a deploy request expired because you did not answer (result `TIMED_OUT`), the AI may file a **re-ask** that
points at it. You then get the same approval again, marked **ASKED AGAIN**, with the same files and the same checks.
Nothing is approved by the re-ask itself: Approve installs, Reject or no answer changes nothing.

- Only requests that ran out of time can be asked again. **Reject is final**: a rejected request is never asked again.
- From 0.3.11 this also covers an unanswered **restart** approval (result `ROLLED_BACK`, the old files were put back):
  the re-ask asks for the install again from the start, then for the restart. A Reject of the restart is final too.
- At most **2** re-asks per request. A re-ask counts against the daily install limit like any install.
- The request must be byte-for-byte what you were asked the first time; if the AI changed it, it is refused and the AI
  has to file a new request instead.
- Read-only lookups are not asked again, and the Deployer never asks again by itself.
- From 0.3.15, if the App stopped right after your restart Approve but before Home Assistant restarted, you are
  asked to approve the restart again. Nothing is recorded as installed until Home Assistant has really restarted.

## Startup self-report — from 0.3.16

At each start the Deployer writes about ten log lines beginning `HBD-NATIVE`. They list the user and group it runs
as, its AppArmor profile, its Python version, and the folder modes and mounts for `/data`, `/homeassistant/packages`
and `/share`. It only reads; it writes nothing, opens no network connection and asks nothing. From 0.3.18 it can also read its own mounts and AppArmor label
(read-only). An AI can read these
lines through the Maintenance App's log window. They are the facts needed before the planned shared-writer setup,
which still needs your separate approval. You do not need to do anything with them.

## Shared writer lock — from 0.3.19

The Deployer and the Maintenance App will share one empty lock file, so they can never write at the same time.
An AI files a one-time request. You get one push: **"Create the shared writer lock?"** Approve creates only the
empty folder `/share/house_brain_writer_v1/` and its empty lock file. Reject or no answer changes nothing. Only
0.3.19 could create that folder; from 0.3.20 the Deployer can use the lock but no longer create the folder.

From 0.3.21 an AI can ask you to **test the shared writer lock**. After your Approve the Deployer either tries the
lock once or holds it for 10 minutes (from 0.3.22; 60 seconds in 0.3.21), so the Maintenance App can prove it waits. Nothing is created or changed.

From 0.3.20 the log shows one `approval asked: …` line when a push goes out and one `approval answered: …` line
when you answer or it expires (the default window is 6 hours).

## Moving the old Deployer's history — from 0.3.23 (draft #985, not install-ready)

Three one-time requests, each asked on your phone first. None names a file path.

1. **Prepare** — "House Brain: prepare the old Deployer's history?" The Deployer reads the copy Maintenance staged
   from the old Deployer's backup (read-only), checks it byte for byte, merges it with today's history and keeps a
   sealed copy in its own private folder. Nothing is selected and nothing else changes.
2. **Select** (within 7 days) — "House Brain: use the moved history from the next start?" It merges again with
   everything recorded since Prepare, so nothing is lost, and marks that history for the next start. Until the
   restart, other requests wait.
3. **Unselect** — "House Brain: cancel the moved-history selection?" Before a restart, this sets the selection aside
   (kept). After a restart, use the `return_to_previous_history` option instead.

4. **Reset** (from 0.3.25) — "House Brain: set aside a half-made moved history?" Only after a Select that did not
   finish: renames its working copy aside (kept) so Select can be sent again.

From 0.3.25 the page shows **History in use** and, while a selection exists, an owner-only **Go back to the previous
history** button (the same as Unselect or the option; it applies at the next restart). Installs made by the old
Deployer get no Undo button.

From 0.3.26, a result or install that is in **both** histories with different entries no longer stops Prepare: today's
Deployer's entry is kept, and the old Deployer's entry is saved unchanged beside the moved history. The Prepare and
Select asks say how many there were; Reject if the number looks wrong.

From 0.3.27 the move also accepts every record shape older Deployer versions wrote (for example 0.1.0's approval
answers without a channel), so an old history is not refused for its age.

After Select and a Deployer restart, the Deployer runs on the moved history with no further taps. At every start
it re-checks the selection, its folders and the shared writer lock, and holds and tells you if anything differs.
The old Deployer, your packages and Maintenance's copy are never written.

## GitHub connection (no token to renew) — from 0.3.6

Open the Deployer page and tap **GitHub connection** at the bottom. From 0.3.7 it takes three taps, also on
the iPhone:

1. **Connect to GitHub**: a new window opens and goes to GitHub with a ready-made private App "House Brain Deployer Bot" (this
   repository only; Contents: Read, Issues: Read and write).
2. **Create GitHub App** (the green button). GitHub sends you back to this App's hand-off address, which keeps the
   key inside this App (you never see it) and opens the install page.
3. **Install**: only **home-assistant-whole-home** is selected already. A page says "Done"; go back to Home
   Assistant. Within one check the GitHub connection page says "signed in with the GitHub App".

The hand-off address is this App's port 8096 at the same address you use for Home Assistant (at home or over
Tailscale). It exists only while a Connect is pending (30 minutes after the page offers it, at most one hour after
you tap it) and answers nothing but GitHub's one-time return. Keep port 8096 in the App's **Network** section.

**The old token retires itself:** once the GitHub App has signed every call for 24 hours, this App revokes its old
hand-made token on GitHub (GitHub e-mails you that it was revoked) and never uses it again. Nothing to delete by
hand; clearing `github_token` in Configuration is optional. To keep the old token, set `retire_old_token` to
false before then.

If Connect does not work (for example port 8096 is not reachable): on GitHub open
**Settings › Developer settings › GitHub Apps › House Brain Deployer Bot › Generate a private key**; the `.pem` file downloads. On this
page use **Upload key file** and pick it (type the App ID shown at the top of the GitHub page if asked).
Never paste the key anywhere else, and never screenshot the Configuration tab.

**Disconnect** deletes the key from this App only; delete the App on GitHub as well to stop it everywhere.
Passes last one hour and are limited to this repository; about 24 a day, at most 30 (then it stops until midnight
UTC; $0, GitHub Free).

## From the House Brain store — from 0.3.7

The Deployer is published in the House Brain store like the Maintenance App, so updates arrive as normal App
updates (no Studio Code Server, no Terminal). Moving from the old local Deployer:

1. Install **House Brain Deployer** from the store (do not change its settings).
2. Approve the Maintenance App's "Switch the Deployer to the store?" push. It copies the settings, stops the old one
   and starts this one.
3. Connect this Deployer to GitHub (3 taps, above).

**Never two Deployers at once:** while another Deployer is active (it wrote `sensor.house_brain_deployer_status` in
the last 15 minutes and did not say `STOPPED`), this one shows "Standby" on its page and does nothing. A clean stop
writes `STOPPED`, so the other takes over at once.

## Startup held — from 0.3.16 (draft #985, not install-ready)

If the Deployer finds a migration start file (`MIGRATED_STARTUP.json` in its data folder) that it cannot use, it
**does not start** and does nothing else: no install, no restart, no migration. You see:

- the status entity `sensor.house_brain_deployer_status` = `HELD` with the reason (only if no install is open; an open
  install's marker is never overwritten, because recovery needs it), and
- one push "House Brain Deployer: startup held" (not repeated for the same reason).

What to do: tell the Deployer chat. The file belongs to the history migration and is only created by that process; do
not delete files by hand. Nothing in Home Assistant was changed.

## Install history size — from 0.3.16

The Undo history keeps up to 500 installs. When full, eligible installs that were already undone
are dropped oldest first. The newest record that prevents an older Undo from reappearing, and
records referenced by an open recovery or freeze, are retained. If none can be dropped, the new
install still succeeds but is not offered for Undo (recorded in the log as
`INSTALL_RECORD_FAILED`); nothing older is lost.

## Migration candidate status — 0.3.16, draft #985

The directory-bound Journal, package effects, forward/recovery clients and initial selection
are implemented and tested locally. This candidate is **not install ready**. Its genuine
installed capture and authorization provider, backup custody, both Apps' shared writer and
native migration/Undo/restore qualification are still required. The approval page keeps
qualification and migration status separate from local test results. Keep the stopped legacy
Deployer installed until migration and restore have been accepted.

