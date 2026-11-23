# House Brain Systems AI

Version 0.3.25 (shadow). 0.3.25 watches the **Dashboard Steward** (installed 2026-11-22), which it did not see at all before:
- It is in the App family and is read like Water AI: `house_brain_app_health.v1` at `/health` on its ingress port.
- Without a local model, the Steward's own block is DEGRADED with exactly `AI_DEGRADED`. The owner has not set up a model yet, so that one reason set is an **INFO** problem and reads `OK` on the family line (`apps:`). Any other reason, an extra reason, or a failed, stale, invalid or unreachable block keeps the normal warning or error.
- The rule is one fixed table (`ACCEPTED_HEALTH_REASONS`), for this App only. Changing it is a reviewed release.
- **Rule before configuring a Steward model (owner pop-up 2026-11-23 "Keep as built + rule", R3 finding F1):** first remove `house_brain_dashboard_steward` from Systems AI `ACCEPTED_HEALTH_REASONS` in a reviewed Systems AI release. Only then set the Steward's `model_url`/`model_name`. The Steward reports `AI_DEGRADED` both with no model and with a broken model, so while the entry exists a broken model would read as INFO and `OK`.

Version 0.3.24 (shadow). 0.3.24 is 0.3.23 with the embedded read policy 1.15.0: the family status sensor (`sensor.house_brain_family_status`) joins the history list, so read-only AI connectors can see 24 hours of its state word (OK, WATCH, PROBLEMS and the rest). Its text stays current-only. No code change, no new route, field or entity.

Version 0.3.23 (shadow). 0.3.23 makes the status words in the `apps:` list on the family line (`sensor.house_brain_family_status`) agree with the problem list:

- **A short blip reads `degraded`, not `failed`.** Since 0.3.21 a failed cycle with a good cycle no more than 3 cycles earlier is a warning (`app health degraded`). The `apps:` list now prints the same word. If the App stays failed, both say `failed`.
- **A stopped App you had running reads `stopped`.** An App seen started at least once and now stopped is listed as `stopped`, whatever its Start on boot setting (House Health, Water, Pool and Cooling start manually, so before this a stop was invisible). It is display only: no new problem, no new alert and no start proposal. An App that was never seen running stays quiet. After the update, an App counts as seen started if Systems AI's change log shows it started.
- **Since when.** An entry that is not OK adds ` since HH:MMZ` (UTC; ` since MM-DD` after 24 hours), the time it first went bad. The times are left out when they would push an App into `+N more`.
- Example: `apps: Systems 0.3.23, House Health 0.1.1 stopped since 14:05Z`.
- Unchanged: the 255-character line, the order (versions changed in 72 hours first), checked words only, no new route, field, schema or entity.

Version 0.3.22 (shadow). 0.3.22 makes the version list on the family line (`sensor.house_brain_family_status`) about versions again:

- **`apps:`** lists the Apps whose **version changed in the last 72 hours**, newest first, each with its status word if it is not OK. Then it lists **every App that is not OK now**. Example: `apps: Systems 0.3.22, Maintenance 0.9.63, Pool 0.1.1 degraded`.
- An App whose health went bad and is **OK again** is no longer listed. On 2026-11-21 such Apps filled the list (`apps 24h: Pool 0.1.1, Cooling 0.3.0, +4 more`) and hid the version news.
- **72 hours instead of 24.** The 0.3.19 readback was missed because every chat was paused for about 64 hours; a release can now still be confirmed after a long weekend.
- Unchanged:
  - the top reason still names the App (`top: app health failed: Cooling`);
  - the list still uses only the room the 255-character line leaves;
  - only checked words reach the line;
  - no new route, field, schema or entity.
- Rows written by 0.3.19–0.3.21 carry no version time, so after this update only `Systems 0.3.22` is listed until another App changes version.

Version 0.3.21 (shadow). 0.3.21 makes the family line quieter about short blips in another House Brain App (0.3.20 is reserved for the MacBook House Link and is not this release):

- **A short blip is a warning, not a problem.** When another App says its last cycle failed but it finished a good cycle no more than 3 of its own cycles ago (3 minutes for today's Apps), Systems AI shows it as `app health degraded` (WATCH), not `app health failed` (PROBLEMS). The App's own reasons are still listed.
- **It becomes an error by itself** at the next 5-minute read if the App has not recovered. A loop that stopped (`CYCLE_OVERDUE`), a broken health block, or a stale, unreachable or invalid one is an error at once, as before.
- **Home Assistant restarts are not held against the Apps.** The 3 minutes start when Systems AI could last not read Home Assistant, if that is later than the App's last good cycle.
- **The first reasons are kept.** A case's timeline keeps the reasons from the first read, and says `WORSENED` if a warning later became an error.
- **One-read cases are counted** on the page under Cases: how many of the cases closed in the last 7 days were a single problem seen in one read. A high number means this page is crying wolf.
- The 3 cycles are fixed in the App; there is no new setting. No new route, entity or schema; the health schema, the read policy and every other App are unchanged.

0.3.19 added a **version list** to the one family line (`sensor.house_brain_family_status`, which the AI connectors already read), so a release can be confirmed by machine and a failing App is named:

- **The top reason names the App** when it is an App problem, for example `top: app health failed: Cooling`. This sits in the first part of the line, which is never cut.
- **`apps 24h:`** lists the Apps whose version or status changed in the last 24 hours, and every App that is not OK now. Not-OK Apps come first, then the newest change. Example: `apps 24h: Systems 0.3.19, Cooling 0.3.0 failed, Energy 0.1.9 (running 0.1.8), +2 more`.
  - `(running …)` means the installed version differs from the one the App's own health says it runs: updated but not yet restarted.
- **It only uses the room the line has left** (255 characters in all): whole entries, then `+N more`, or nothing. Every other part of the line stays as before.
- **It remembers across restarts**, in the App's own store, so the first line after this update says `Systems 0.3.19`.
- **Only checked words reach the line.** Versions must look like a version, otherwise `?` is shown. Names come from a fixed list.

The Maintenance App relays the line at most every 30 minutes, so allow about 35 minutes after an update. No new route, field, schema or entity; read policy and health schema unchanged.

Version 0.3.18 (shadow). 0.3.18 only refreshes the embedded read policy to 1.14.0 (the family status sensor becomes readable by the AI connectors: its word, its one line and its time). No code change.

Version 0.3.17 (shadow). 0.3.17 only refreshes the embedded privacy data-class map: it gains one row for the specialist answer contract (`house_brain_specialist_answer.v1.schema.json`, the read-only `/answer` route the specialists already serve). The 0.3.16 code is unchanged.

Version 0.3.16 (shadow). 0.3.16 is three small corrections to the O&M link of 0.3.15. Nothing new is switched on, and the link is still off until you set `onm_key`.

- **A specialist's answer now counts as a lead for O&M.** When one of the specialist AIs answered a question about a problem (0.3.13), the O&M repair record may now list that answer as a possible cause. Before, the record contract did not know this kind of lead and such a case always read "needs an engineer". "Saw nothing unusual" and "not checked" are still never a lead.
- **An old kill-switch reading holds the link.** The two O&M routes now also hold when Systems AI's last kill-switch reading is more than 3 minutes old or lies in the future (a stuck cycle, a clock that jumped). Before, they held only until the first reading after a start.
- **One day for both caps.** The limit of 96 stored repair records a day now counts by the Phoenix day, like the findings limit on the same link. It counted by the UTC day before.

No new option, port, read or write. $0.

Version 0.3.15 (shadow). 0.3.15 adds the **O&M link**: two new signed routes on the House link port (8791) for the O&M investigator that will run on the MacBook (design `ONM_A6_INVESTIGATOR_DESIGN_R1_2026-11-14`, step A6.1). **They stay off** until you set a third option, and nothing on the MacBook uses them yet (that is steps A6.2 and A6.3, each with its own approval).

- `onm_key`: a second long random key (32 to 512 characters). Generate it on the MacBook, like `link_key`, and paste it here; it is never shown in chat or the repository. It must be different from `link_key`. It only works when `link_key` and `link_peer` are also set.

What the two routes do:

- **Read cases** (`GET /link/v1/cases`): the live cases and the cases cleared in the last 8 days, each with its checks, what its investigation concluded, its repair record and a revision number. At most 50 cases and 256 KB. While the kill switch holds Systems AI the list is empty and says "held".
- **Write one repair record** (`POST /link/v1/repair`): the likely cause, what a full repair must achieve and how to prevent a repeat, for one case. Every claim must name its evidence. At most 64 KB each and 96 stored a day. Refused while the kill switch holds Systems AI, and until the first kill-switch reading after a start.

How they are protected:

- Only the MacBook's address (`link_peer`) is answered.
- Every request and every answer is **signed** (HMAC-SHA256) over its exact contents, with a time (2 minutes of tolerance) and a number that must always go up. The key itself is never sent. A copied, changed, late or replayed message gets 403 and changes nothing.
- A record written against an older version of a case is refused, so a stale write never overwrites a newer one.
- A record can never change a case's status, or the two checks Systems AI measures itself (service restored, prevention proven). It fills only the case's O&M repair record.

On the Systems AI page, a case that has a repair record shows it under the case: its state, O&M's two checks (cause corrected, side effects reconciled) with the evidence they rest on, the most likely cause, and any request O&M suggests. Every request stays your tap in Maintenance or the Deployer. The Cases section also shows whether the O&M link is on, how many records were stored today and the last refusal.

Nothing here acts: no command, no Home Assistant write, no internet, no model, $0. Home network only. Daily counts when the MacBook side is running: at most 288 case reads and 96 stored records. To turn the routes off again, clear `onm_key` and restart Systems AI. If the MacBook ever loses its O&M state, set a new `onm_key` on both sides.

Version 0.3.14 (shadow). 0.3.14 adds a **findings channel** to the House link, so the MacBook's weekly reading can reach this page. Once a week (when that research job is built and switched on, a later step) the MacBook asks one fixed public question per area (energy, cooling, water, pool, house health, systems, AI) and follows new small open models and llama.cpp releases. Each result arrives as a "finding" and is shown in a new section, **What the house read this week (untrusted, not checked)**: the area, the question, how many sources on how many sites, the page titles as links, and any source that could not be checked. Model-watch rows show the size and "may fit" or "size unknown" exactly as the MacBook wrote them.

- A finding is reading material, not a checked fact. It never opens a case, never becomes a hypothesis or a suggestion, never reaches Maintenance, O&M or the Deployer, and never downloads or installs anything.
- The route is `POST /link/v1/findings`, on the same port, address and key as the other House link routes, and it is off while the link is off. Each finding is checked strictly: only the known fields, none twice, only `https` links, at most 10 links, every link marked untrusted, read-only permission and no execution authority. An investigation id (`inv-…`) is refused here.
- Limits: 64 KB a request and 20 findings a Phoenix day (separate from the 120 research answers); over the cap the MacBook gets "429" and nothing is kept. While the kill switch holds Systems AI, findings are refused ("409").
- Systems AI keeps the newest 50 findings for 30 days in its own database. A finding for the same area and week replaces the earlier one. Findings that drop off are counted on the page.
- Nothing new is read from the internet by the Pi, and nothing costs money ($0).

Version 0.3.13 (shadow). 0.3.13 lets Systems AI **ask the specialist AIs** about a problem. When an investigation is about something one of the five specialists knows best, Systems AI asks that App one read-only question and shows its answer under the investigation as a "SPECIALIST" line:

- an Energy AI, Cooling AI, Water AI, House Health AI or Pool AI problem (stopped, failing, unhealthy) asks that App about its own health;
- unusual errors from Sense or Enphase ask Energy AI about power data; Ecobee or a cooling integration asks Cooling AI; Rachio or B-hyve asks Water AI; NUT (the UPS), Ping, Netgear or Google Wifi asks House Health AI; njsPC, AquaRite, Ondilo or Jandy asks Pool AI;
- anything else asks nobody. Systems AI never guesses who to ask.

The line reads, for example, "Energy AI says (2 facts): …", "Energy AI saw nothing unusual in power data", or "Energy AI: not checked (reason)". No answer, a refused or unreadable answer, or an answer older than two hours always reads **not checked**, never "nothing wrong". The line sits below a matching playbook and above public research. It is evidence only: it never adds a suggestion, never acts, and a "not checked" or "nothing unusual" line does not count as an explanation.

Limits: at most one question per investigation per hour, at most 24 questions a day to each specialist (the count survives a restart), only to an App that is running, and nothing at all while the kill switch holds Systems AI. Each question is one GET on the internal Supervisor network, on the same address and port as the App's health check; no new port, no internet, no Home Assistant read, $0. A problem that comes only from Home Assistant's WebSocket being unreadable is not sent to House Health AI: Systems AI has no outage signal of its own to tell an outage from a plain read failure.

Version 0.3.12 (shadow). 0.3.12 only refreshes the embedded privacy data-class map: it gains two rows for the AI perimeter's new history-archive tool (`get_history_archive`). The 0.3.11 code is unchanged.

Version 0.3.11 (shadow). 0.3.11 also watches **House Brain Tang**, the small key server that lets the MacBook unlock its disk at home. Tang has no page of its own, so Systems AI reads its health on its internal port. If Tang stops answering, Systems AI shows it on this page and in the family status line and opens a case, so you hear about it before a MacBook reboot would need the passphrase. Starting Tang again stays your tap, and Home Assistant's watchdog also restarts it.

Version 0.3.10 (shadow). 0.3.10 watches the two newest House Brain AIs, **House Health AI** and **Pool AI**, the same way it watches Water AI. It reads each one's health block, shows it on this page and in the family status line Maintenance shows, and opens a case if one fails, goes stale or stops answering. Starting or restarting one stays your tap in Maintenance, as always. This release also puts back the 0.3.8 note below, which an earlier edit had removed.

Version 0.3.9 (shadow). 0.3.9 only refreshes the embedded read policy to 1.13.0 (two AI status sensors gain their heartbeat and outage fields); the House link readings now include them. No code change. Before that, 0.3.8 counts research sources honestly. A research answer from the MacBook can list the same page twice (with tracking tags, "www." or a trailing slash) or several pages from one website. Each page now counts once, a repeat no longer pushes another source out of the top 10, and the investigation line says how many sources on how many sites, for example "3 source(s) on 2 site(s)". Pages from one website alone are marked "one site only, not corroborated". Research stays untrusted evidence, never a suggestion or an action.

Version 0.3.8 (shadow). 0.3.8 counts research sources honestly. A research answer from the MacBook can list the same page twice (with tracking tags, "www." or a trailing slash) or several pages from one website. Each page now counts once, a repeat no longer pushes another source out of the top 10, and the investigation line says how many sources on how many sites, for example "3 source(s) on 2 site(s)". Pages from one website alone are marked "one site only, not corroborated". Research stays untrusted evidence, never a suggestion or an action.

Version 0.3.7 (shadow). 0.3.7 makes the House link stricter about the research answers the MacBook sends back. An answer that names any field twice, or carries a field Systems AI does not know, is refused with a fixed reason (`ANSWER_DUPLICATE_KEY`, `ANSWER_FIELDS_INVALID`, `ANSWER_SOURCES_INVALID`) and is never stored. The MacBook's own research service already sends exactly the known fields, so nothing changes for it. Nothing else changes, and the link still stays off until `link_key` and `link_peer` are set.

Version 0.3.6 (shadow). 0.3.6 adds **house readings** to the House link (your pop-up "Also include house readings"). The MacBook can now read the same privacy-filtered sensor readings that Claude and ChatGPT already get. Systems AI runs the exact same privacy filter (embedded, and checked at start against its fingerprints) on the states it already reads, plus Home Assistant's entity list, which it reads once an hour. People, locks, cameras, trackers and anything on a lock or camera device are never included, and neither are names. Without a filter-approved snapshot the link serves no readings at all, only a reason. It rebuilds at most every 5 minutes and never while held. Like the rest of the link, it stays off until `link_key` and `link_peer` are set.

Version 0.3.5 (shadow). 0.3.5 adds the **House link** (design `MACBOOK_HOUSE_LINK_R1_2026-11-08`): one port (8791) on your home network for the MacBook research service. **It stays off** until you set both new options in the App's Configuration tab:
- `link_key`: a long random key (at least 32 characters). Generate it on the MacBook and paste it here; it is never shown in chat or the repository.
- `link_peer`: the MacBook's fixed address on your network. Requests from any other address are refused.

Through the link the MacBook can read the AI stop switch, the House Brain App states and the open investigation questions, and send back research answers. An answer appears in its investigation as a **"Public research (untrusted)"** line with its sources, below any matching playbook. It is never a suggestion and never an action, and while Systems AI is held answers are refused. House readings over the link come in 0.3.6.

Version 0.3.4 (shadow). 0.3.4 adds a **progress ledger and a stall counter** to each live investigation (research idea W26-C6, after Microsoft's Magentic-One). Once an hour each one answers five fixed questions: is it answered (a playbook, an earlier fixed case or a suggestion already shown), is it going in a loop, is it making progress (new facts or evidence since the last review), who acts next, and what to ask. An hourly review with nothing new adds one to its stall counter; new evidence takes one off. After 3 such reviews (option **stall_reviews**, 2 to 10) it reads **stalled** (reason `CASE_STALLED_NO_PROGRESS`) and asks for a re-plan or your answer, on its page and on the family status line Maintenance already shows. A stalled case never retries faster, makes no extra read and takes no action. Each investigation keeps at most 64 short fact digests and 6 ledger lines; a closed one keeps none.

Version 0.3.3 (shadow). 0.3.3 adds **playbooks** (agentic layer step A2): five real cases the house has already solved, written down so Systems AI recognises them again without a model or a subscription. A new **Playbooks** section on the page shows what matches now, what to ask you, how the fix is checked and when not to use it:
- **AI Access stopped** (2026-10-07 and 2026-11-01): every House Brain AI is held; start AI Access.
- **Start on boot off** (2026-11-01): AI Access, the Inspector or the Observer stopped with Start on boot off now raises a warning and suggests Start (your tap in Maintenance). Before 0.3.3 it raised nothing, so they stayed down unseen after a restart.
- **Deployer held at start** (STARTUP_REFUSED, 2026-11-01): restarting does not clear it; the known cause and the fixed version are named.
- **Deployer daily approval limit** (2026-11-02): nothing to repair; tap "Allow more today" or wait.
- **An integration needs a new login** (Tuya, 2026-11-02): log in again; Maintenance (rule R9) owns it.
A matching playbook ranks first in its investigation. Playbooks only suggest: they add no read, no action and no new suggestion of their own.

Version 0.3.2 (shadow). 0.3.2 fixes two things seen on the first live 0.3.1 page:
- **Patched versions:** an advisory whose fix is already installed (your version is at or past its patched version) reads "not affected", even when GitHub's affected range is open-ended.
- **Investigations:** each changing thing is one line ("changed 3 times shortly before"), with updates and App states ranked above routine status flips.

Version 0.3.1 (shadow). 0.3.1 fixes two things seen on the first live 0.3.0 page:
- **ESPHome:** an ESPHome add-on that is not installed still answers with no version; that now counts as "not installed" and the next ID is tried, so the beta Device Builder is found.
- **Investigations:** they open only while a problem is still active (or has come back), and live ones are listed first. The closed items the 0.3.0 install opened for already-cleared cases are cleared once.

Version 0.3.0 (shadow). 0.3.0 adds **investigations**, the first step of the approved agentic layer (design R1 step A1):
- **When one opens:** automatically, for a serious problem, one that came back, or a warning still open after an hour.
- **What it is:** a bounded work packet: case, question, budget, a 7-day expiry, read-only research permission and no
  execution authority.
- **How it answers:** today only Systems AI's own rule agent (T0) answers, from the evidence:
  - the problems and the changes seen before them;
  - the same problem in earlier cases (case memory) and how it ended;
  - possible causes listed side by side;
  - the fix Systems AI already suggests, if any (your tap);
  - otherwise "needs an engineer".
- **O&M's part:** each case also carries an empty repair record that only O&M will fill in.
- **Unchanged:** no new read, outside request, model or authority.
0.2.3: when the AI kill switch is missing or stale (never on FULL_STOP), this App still makes
one read-only Supervisor read of AI Access every 5 minutes. AI Access publishes the switch, and if it stops, every
House Brain AI holds (seen live on 2026-11-01). If AI Access is stopped, the page and the family line say so, and
after 15 minutes Maintenance shows a "Start AI Access" suggestion, carried out only on your tap. Nothing else runs while
held.
Version 0.2.2 (shadow). Boot is manual (turn on Start on boot in the App's page; it is read-only, so that is safe).
Only `SHADOW` mode exists. 0.2.2 reads the ESPHome Device Builder's version (its Supervisor info, options dropped),
so ESPHome security advisories are matched to the version you run, and the System Monitor CPU and load sensors
when you enable them. No new outside read. 0.2.1 added the **World Change Radar**:
public security news that touches what runs here (see below). It is this App's first and only outside read,
10 requests a day at most, with no key and $0. 0.2.0 added **cases**: each incident is
followed from first sign to a verified fix and kept after the incident has rolled off (see below); no new
read, route or authority. 0.1.4 also watches Water AI's own health
block (Water AI serves it from 0.1.0); nothing else changes. 0.1.3 opened a fresh Core WebSocket for each
cycle and retries a command once on a fresh socket when a kept one has gone stale, so the WebSocket read no
longer fails every other cycle; the four read-only commands are unchanged. 0.1.2 added one read-only, internal-network
route, `GET /archive/<day>.json`, so the coming Cooling AI can read archived days (see Outputs), and
watches Cooling AI's own health block once it is installed. 0.1.1 added the Arbiter's decision,
reason, owner, kind and shadow selection, the A/C's real call (`binary_sensor.ac_compressor_cooling`), four
eligibility flags, the modelled evap supply temperature, the vent floor and the APS price to the cooling
archive (31 entities). The new entities' 40 days are copied first after the update (about 9 hours).

## What it does

- **Reads, never writes.** It makes three kinds of request:
  - `GET` to the Supervisor's `info` routes (`/info`, `/host/info`, `/resolution/info`, `/addons/self/info` and `/addons/<House Brain App>/info`), with role `default`;
  - `GET /core/api/states`;
  - `GET /core/api/history/period/...` for the 31 cooling archive entities only (states, no attributes, one day at a time);
  - four read-only Core WebSocket commands (`system_log/list`, `repairs/list_issues`, `system_health/info`, `recorder/list_statistic_ids`). From 0.1.3 the socket is closed at the end of every cycle, and a command that fails on a socket kept from earlier in the cycle is tried once more on a fresh one. A failure on a fresh socket is a real failure.

  There is no POST, state write or service call anywhere in the App. Sibling App info is cut down to slug, name, version, state, boot, hostname, ingress port, watchdog and update flag before anything is kept; their options are never stored.
- **Finds its siblings without settings.** It reads its own slug, then checks the known House Brain App names under the same store prefix and under `local_`, once an hour.
- **Self-health.** It reads each App that serves `house_brain_app_health.v1` (Energy AI 0.1.2 and later; Cooling AI and Water AI from 0.1.0) at `http://<hostname>:<ingress port>/health`. It checks the block with the shared validator: unknown, stale, invalid or unreachable is never OK.
- **Learns:**
  - database size (hourly, from `system_health/info`), disk free (`/host/info`), memory free and swap (System Monitor sensors);
  - errors per integration per day, against its own 14-day median and spread;
  - message kinds in the system log (masked templates; numbers, addresses, ids and e-mails become placeholders);
  - Repairs issues, Supervisor resolution issues, and App state and version changes.
- **Forecasts with honest bands.** A robust trend (Theil-Sen) with a split-conformal 90 % band. Each forecast (1, 7 and 30 days) is frozen when made and scored later against the "no change" baseline. It is called a forecast only after it has beaten that baseline on at least 5 scored days.
- **One alert per problem:**
  - Problems are fingerprinted.
  - Maintenance's own findings are counted, not raised again.
  - A parent problem (Core or the Supervisor unreachable) hides the problems it explains.
  - Flapping is held back (Nagios-style weighted state change, 21 cycles, 50 %/25 % hysteresis).
  - Problems that open within 15 minutes of each other form one incident with a timeline, the changes seen in the 24 hours before it, and a draft postmortem whose causes are marked as hypotheses.
- **World changes (0.2.1).**
  - **Sources:** the CISA list of vulnerabilities attackers are using now, and GitHub security advisories for Home Assistant Core, OS, the Supervisor and ESPHome.
  - **Reads:** at 03:20 and 15:20, one source a minute, at most 2 attempts per source a day (10 requests a day). A failed read is retried once, 30 minutes later. No read happens while the kill switch holds this App. Every attempt is saved before it is sent, so a restart never repeats one.
  - **Matching:** an advisory touches you when the installed version is in its affected range. A recent CISA entry (30 days) for a product you run touches you until that component is updated. An unknown version is "version unknown", never "not affected". ESPHome's version comes from its add-on (stable, beta or dev, from ESPHome's own repository); if none is found, its advisories show as version unknown.
  - **Output:** a match raises `WORLD_CHANGE_TOUCHES_YOU` (a problem, so it joins incidents and cases) and suggests the matching update in Maintenance. Systems AI changes nothing.
  - **Failures:** a source that fails keeps its last good result with its age. One that was never read says "not checked", never "no threats".
  - **GitHub data:** the advisories show "not yet proven on live data" until the first live read parses.
- **Cases (0.2.0).** Each incident becomes a case, kept in this App's own database:
  - `OPEN` while a problem of it is open; `ACTION_SUGGESTED` when Systems AI also suggests a fix for one of them (still only your tap in Maintenance or the Deployer);
  - `WATCHING` for 24 hours after every problem cleared, then `FIXED_VERIFIED` if none came back;
  - `REOPENED`, linked to the newer case, if one came back during the watch.
  - Four repair checks: *service restored* (measured when the problems clear), *prevention proven* (none back for 7 days; `FAILED` if one came back), and *cause corrected* and *side effects reconciled*, which this App cannot measure and always shows as `NOT_MEASURED`, never as passed.
  - An open case whose incident rolled off the 60 kept is marked "no longer measured", never fixed.
  - While the kill switch holds Systems AI, cases do not move. A problem that cleared during a hold is checked again from the end of the hold. It counts as restored only after a full 24-hour watch with it absent.
  - Kept 90 days after it closes, 200 at most (oldest closed first); the page shows the newest 20 and counts by status.
- **Error budgets.** Core API, Supervisor API, WebSocket, the kill switch and each running House Brain App each get a 99.5 % 30-day budget, with 1 h / 6 h burn rates. Unknown counts as bad.
- **Keeps the cooling season's detail before Home Assistant forgets it.** Home Assistant keeps full detail for 40 days. Once started, this App copies the recorder history of 18 cooling plant entities (outdoor and indoor temperature, humidity and dew point, the evap relays and relay mode, the Arbiter phase, the eligibility code, AC power, the attic thermometers), one entity-day a minute, oldest day first; the 40 days are copied in about 12 hours, then each new day after midnight. It keeps 5-minute time-weighted values for numbers and exact change times for relays, phases and codes, for 120 days (about 10 MB a season). A steady reading counts as idle, not missing; only `unknown`/`unavailable` are gaps. Nothing about people, presence or location is read. Any day downloads as CSV from the page. Your Home Assistant backups include it.
- **Advice.** "Good time to ask" is the Cooling Arbiter `IDLE` and outside 22:00–07:00. There is also a shared list of readings no AI should trust.

## Outputs

- **Ingress page** (sidebar): plain words first, full evidence behind "tap to open".
- **Internal network only** (the /23 holding the Supervisor proxy address, and loopback; never the LAN):
  - `GET /health`: this App's own `house_brain_app_health.v1`.
  - `GET /digest`: one family status line of at most 255 characters, plus one line per App. Maintenance relays it into the one family sensor for Companion; owner decision 2026-10-20, built on the Maintenance side as a separate release.
  - `GET /archive/YYYY-MM-DD.json` (0.1.2): one local day of the cooling archive, rows `[entity, time, value, text, kind]`, plus each entity's read status for that day (`DONE`, `EMPTY`, `FAILED` or `NOT_READ`, and whether the purge had cut into it), so a gap is never read as a value. Schema `house_brain_systems_ai_archive_day.v1`. Read by Cooling AI (owner decision 2026-10-25 "Read Systems AI's archive"). Anything but an exact day is refused.
  - `GET /proposals`: at most 10 suggestions, only `start_app` or `restart_app` for a House Brain App slug, each open at least 15 minutes and not flapping, expiring after 1 hour. Maintenance may show them under "Needs your decision"; nothing happens without the owner's Approve. They are counted in a shadow scorecard (`executed: false`).
- Any other method gets HTTP 405 `READ_ONLY_APP`.

## Kill switch

Every fast cycle reads `sensor.house_brain_ai_kill_switch_effective`. FULL_STOP holds all collection and shows `HELD`; that is not a problem. A missing, stale, future-dated or unknown state also holds, and is reported as a problem. `READ_ONLY` (the owner's steady state) allows everything this App does, because it never writes. During a hold, `/proposals` is empty.

## Bounds

- About 8,000 local requests a day, all inside the house: states each minute, Supervisor and sibling reads each 5 minutes, database size hourly, statistic ids daily. While the archive catches up, one more read a minute (about 700 reads once); after that 18 a day. From 0.2.2, up to 3 more an hour to find the ESPHome Device Builder (stable, beta, dev; 1 once found).
- Outside requests (0.2.1): at most 10 a day, only to `www.cisa.gov` (one exact URL) and `api.github.com` (four exact URLs). They send no key or token, refuse redirects, use the system TLS store and are capped at 6 MB. No paid API.
- Python standard library only.
- 256 MiB address cap. App-owned SQLite (mode 0600): 7 days raw, a year of hourly rollups.
- 2,000 log templates at most, 60 incidents, 200 cases, 200 changes, 400 frozen forecasts per metric.

## Not in 0.1

- The busiest-entity ranking and the owner tap ledger wait for Maintenance to share what it already measures.
- Feeding the offline evaluators is a later release.
- The outside Pi-freeze check cannot be seen from the Pi.
- Disk, CPU and load System Monitor sensors and the per-App Supervisor CPU/memory sensors are disabled in Home Assistant by default. Turning them on is the owner's UI step. From 0.2.2 this App reads `sensor.system_monitor_processor_use` and `sensor.system_monitor_load_15_min` when they exist; until then they read as missing, never OK. Disk space comes from the Supervisor.

## Recovery

Stop the App; it holds nothing that needs undoing. Its database can be deleted to start learning again, but that also deletes the cooling archive: download the days you want first.
