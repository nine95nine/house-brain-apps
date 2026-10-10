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

No answer = No. If anything fails, the old files are put back automatically (and, if Home Assistant
was already restarted, it is restarted once more on the old files). You get a result notification.

## Lookups (read-only)

An AI can also ask to **look something up** (for example current sensor readings). You get
**"approve lookup?"** showing the exact read-only template; after you approve, the result is posted to
the GitHub issue. Lookups can never change anything.

## What it can and cannot do

- Generic deployments write only `/config/packages/<name>.yaml` files and keep the old copy as `<name>.yaml[.tag].bak`.
- 0.3.9 adds one fixed repair: remove an obsolete HTTP block from `configuration.yaml` after confirming the migrated Network settings already match. The AI receives hashes, never the configuration contents. You approve inspection, then separately deployment and restart. Unsupported files or mismatched settings are refused.
- Cannot touch `secrets.yaml`, `.storage`, dashboards or other Apps. No arbitrary root configuration editor or HTTP settings mutation is provided.
- Never turns anything on or off. It only sends you notifications, makes a partial backup, runs the
  configuration check and (after your second tap) restarts Home Assistant.
- Opens no port on your network. Its approval page is reachable only inside the Home Assistant app
  (ingress), and only your account can decide.

## Options

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
