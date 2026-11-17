# House Brain Cooling AI

Version 0.3.0 (shadow; 0.1.1 added `GET /answer`, A4; 0.2.0 added the regret ledger; 0.3.0 adds the natural commissioning ledger and the shadow comfort readout). Boot is manual. Only `SHADOW` mode exists. Design:
`docs/house-brain/COOLING_AI_DESIGN_R1.md` (owner pop-ups 2026-10-25 "learner plus scorecard, plus
full plan list", "Read Systems AI's archive", "Approve design").

## What it does

- **Reads, never writes.** Every request is a `GET`:
  - `GET /core/api/states`, cut down at once to the listed plant and tariff ids. Only id, state and times are kept; attributes are dropped before anything is stored.
  - Supervisor `GET /addons/self/info` and `GET /addons/<prefix>_house_brain_systems_ai/info` (role `default`), to find Systems AI.
  - `GET /archive/YYYY-MM-DD.json` from Systems AI 0.1.2 or later, on the internal network, for days already copied from the recorder.

  There is no POST, state write or service call anywhere in the App. Nothing about people, presence or location is read.
- **Keeps the truths apart.** What the house *did* comes from the relays (`sensor.evap_actual_relay_mode`, measured) and the Ecobee's call (`binary_sensor.ac_compressor_cooling`, inferred). The Arbiter's phase is its intent, not operation: a `VENT_RUN` phase with the relays off counts as idle. Sense watts are energy only and never prove the A/C ran.
- **Learns, in °F per hour:**
  - the cooling rate of each mode against its drive: outdoor minus indoor for idle drift, vent and A/C; wet bulb minus indoor for wet evap. Each is a robust line (Theil-Sen) fitted on 4 local days in 5, with a 90 % band measured on the fifth day;
  - the house's time constant from idle drift;
  - the A/C's runtime per outdoor degree-hour over 65 °F and its cycles per runtime hour (from the Ecobee's call);
  - the *modelled* wet-evap effectiveness. It uses the modelled supply temperature until the #119 probe exists, so it shows the model's assumption, not pad scaling;
  - Humid Boost: the projected RH at a session's start against the measured RH at its end.

  Thermal response only: never COP, EER, SEER, capacity, airflow or equipment health. A mode is learned only after 3 hours of clean points over 2 days plus held-out points; until then it reads "learning".
- **Plans each minute.** It picks the cheapest mode (vent, then wet evap, then A/C) whose learned band is wholly cooling, from the Arbiter's own published comfort target, floor, charge target and permission flags. It never plans a mode those flags block. A mode it is already running continues until the target is met; any other start needs the indoor temperature 0.5 °F above the target. When a cheaper permitted mode is still unlearned, it says "learning" rather than guessing past it to a dearer one.
- **Scores.** Each minute is either agreement, one fixed "Cooling AI prefers X, the house did Y" code, or set aside as not comparable: an owner override, Pause, life safety, panic, a human thermostat change, a pending or transition phase, an unclear plant moment, or a missing input. Set-aside minutes are never counted as Cooling AI being right. It also counts the plant switches each side would make and how often it matches the Arbiter's own shadow selection.
- **Plan list, all SIMULATED:**
  - degree-hours above target under its plan against the house, with a band;
  - energy and cost of each difference at the APS price, valued $0 while the EPR-6 bank covers on-peak; an unknown bank reading is counted as unknown, never as $0 or as full price;
  - pre-cool value per weekday (a 2 °F pre-cool before 12:00), $0 while the bank covers on-peak, "bank unknown" when its reading is missing;
  - Humid Boost calibration;
  - forecast calibration.
- **Forecasts honestly.** Each hour it freezes the indoor temperature 1 hour ahead under the current mode. At 22:00 it freezes the 06:00 temperature if the plant stays idle, using last night's outdoor temperatures. Each is scored later beside "no change", and is called a forecast only after it beat that on at least 5 scored days. A 1-hour forecast whose mode changed is not scored (one unknown minute is not a change). A night when the plant ran is not scored, and a night with less than 90 % of its readings is MISSING, never "idle". The overnight band is the hourly band times 8, because a steady rate error adds up.
- **Episodes:** each clean run of one mode becomes a `house_brain_cooling_thermal_episode.v1` record (MEASURED_PHYSICAL, TRAIN or HOLDOUT by day).

## History

Home Assistant keeps full detail for 40 days. Cooling AI reads older and earlier days from Systems AI's archive, one day a minute, oldest first, until it holds up to 120 days; after that it reads one new day each night.
- A day is complete when every entity Cooling AI reads is copied. A day Systems AI has only partly copied waits. A day older than 41 days is taken as it is, because the recorder has already purged it. A day Systems AI never kept is marked unavailable.
- A failed read is tried again within the hour, at most 6 times, and never filled in. A 404 never means "the day is gone".
- Without Systems AI 0.1.2 or later, it asks again once an hour, learns from its own minutes only, and says so: the health block reads `SYSTEMS_AI_ARCHIVE_UNREADABLE`.

## Outputs

- **Ingress page** (sidebar): plain words first, full evidence behind "tap to open", and `/episodes.json` (the latest episodes).
- **Regret ledger** (0.2.0, owner pop-up 2026-11-16 "Approve, build 0.2.0"; proposal `docs/architecture/COOLING_AI_0_2_0_REGRET_LEDGER_PROPOSAL_2026-11-16.md`).
  - Shown on the page section "Which rules cost the most" and in `/regret.json` (ingress only).
  - Each comparable minute is charged to the Arbiter's own `REASON`, with the scorecard code and the weather band (outdoor °F and dew point, 5 °F steps).
  - A disagreement minute adds the scorecard's SIMULATED increments, house minus Cooling AI. Minutes that cannot be priced (no learned model, power or price, unknown EPR-6 bank) are counted and add no dollars.
  - A minute whose drive lies outside the learned model's training range (± 3 °F) is **not priced** (`out_of_support_minutes`): the model would be extrapolating.
  - Rolling 7- and 28-day top 5, ranked by a one-sided 90 % **lower bound** on the window cost, with each evidence day treated as one draw:
    - `REGRET` only when the lower bound is above $0 and the house was not cooler;
    - `COMFORT_TRADE` when the lower bound is above $0 but the house was kept cooler;
    - `NOT_PROVEN` when the point estimate is positive but the lower bound is not, which includes any rule with fewer than 3 evidence days (`evidence: THIN`);
    - `BELOW_FLOOR` under $0.25 in the window, not ranked;
    - `MODEL_NOT_QUALIFIED` when nothing could be priced.
  - `rules` lists up to 20 rules seen, most costly first, for the evidence view.
  - Kept 120 local days, at most 400 rows a day (the excess folds into one `OTHER` row, still counted).
  - The digest adds the 7-day top rule.
  - `/proposals` stays empty.
- **Natural commissioning ledger** (0.3.0, owner pop-ups 2026-11-16 "Approve ...", "Yes, read both", "Build now, hints as 0.3.1"; proposal `docs/architecture/COOLING_AI_0_3_0_COMMISSIONING_LEDGER_PROPOSAL_2026-11-16.md`).
  - Shown on the page section "How each start went (commissioning)" and in `/commissioning.json` (ingress only).
  - **A start** is 30 clean idle minutes, then vent, wet evap or A/C by the house itself. A wet start may pass through a pump-only pre-wet of up to 10 minutes. The App never starts anything.
  - **Its response** is read to the earlier of 30 minutes or the mode's own stop (at least 15 minutes), less the drift already under way before the start: the change in the indoor temperature slope and in the indoor dew-point slope.
  - **Not judged, counted by reason:** owner override or safety, Pause, a transfer from another cooling mode, a short run, a gap, a missing input, more than 8 starts in a day.
  - **Starts from before the first 0.3.0 run only build the baseline** (`BASELINE`) and are never judged. For those days only the Arbiter owner and decision can be checked, because Pause, panic and the evap mode are not in the archive.
  - **Band:** a robust line of the response against the drive on 4 days in 5, with a one-sided 90 % lower band from the fifth. It is used only with 20 held-out starts on 5 held-out days, and only inside the learned drive range ± 3 °F.
  - **Verdict, made once and kept:** `NORMAL`, `LOW_RESPONSE`, `NO_BASELINE`, `BASELINE`, `BAND_MISCALIBRATED`. A later relearn never changes a stored verdict. Low starts never re-enter a fit.
  - **Pattern** (after 2 low starts on 2 days and 2 of the last 3 judged starts; 2 normal starts clear it). A pattern says what was measured and names no part:
    - `WET_LIKE_VENT`: a wet start cooled like a fan-only start and added almost no moisture;
    - `BOTH_REDUCED`: a wet start cooled less and added less moisture than usual;
    - `TEMP_ONLY_LOW`: a wet start cooled less while moisture rose as usual;
    - `VENT_LOW`, `AC_LOW`: a fan-only or A/C start cooled less than usual;
    - `ROOM_FLAT`: one room reading did not change at all while the other room moved.
    - With a wet-bulb depression under 10 °F the moisture signal is not used. A/C never uses it.
  - **Drift:** once a mode has 30 starts over 10 days, that fit is frozen as its season anchor. The last 28 days are compared with it: `STEADY`, `WEAKER` (even the upper bound is below zero) or `NOT_PROVEN`.
  - **Self-check:** when more than 30 % of a mode's last 20 judged starts were low and lows alternate with normals, the band is not trusted: the mode reads `BAND_MISCALIBRATED` and shows no pattern until that clears.
  - **Kept:** one store row per local day, at most 8 starts a day, not expired by the 120-day rule. At 1.5 MB the ledger stops adding and says `COMMISSIONING_STORE_FULL`; nothing is deleted.
  - **Limit:** it compares the house with its own past. A fault that was already there while the baseline was learned looks normal. Held history starts about 2026-09-16, so most modes read "no baseline yet" until summer 2027.
  - The digest adds one token of at most 40 characters (`starts: ok`, `starts: no baseline`, `starts: WET low 2/3`).
- **Shadow comfort readout** (0.3.0). Page section "Comfort (shadow model)". MODELLED.
  - The ASHRAE 55 adaptive band from the mean of the previous 7 days' outdoor temperature, beside the Arbiter's fixed target: how many minutes above the target were still inside the 90 % and 80 % bands, for idle, vent and wet evap.
  - Valid only for a 7-day outdoor mean of 50–92.3 °F; otherwise `OUT_OF_RANGE` and no claim.
  - A minute with indoor dew point above 62 °F, or unknown, is never counted acceptable.
  - Room air speed is not measured. The air-movement credit (+2.2 °F on HIGH fan) is a separate "if air speed is at least 0.6 m/s" column, never a claim.
  - The applicability range and the credit are as recalled from the standard; the primary text was not retrieved, and the output says so.
  - **It feeds nothing:** not the plan, the scorecard, the regret ledger or any proposal.
- **Two more readings** (0.3.0, owner pop-up "Yes, read both"): `sensor.living_room_temperature` and `sensor.master_bedroom_temperature`, the number only, kept in the App's own 5-minute rows for 120 days. They are not in Systems AI's archive, so they have no history before 0.3.0.
- **Internal network only** (the /23 holding the Supervisor proxy address, and loopback; never the LAN):
  - `GET /health`: its own `house_brain_app_health.v1`, which Systems AI 0.1.2 reads.
  - `GET /digest`: one line of at most 255 characters.
  - `GET /proposals`: always empty in 0.1. Cooling AI never proposes an action; a later "tune the Arbiter" idea would be its own owner decision and a YAML change through the Cooling chat.
  - `GET /answer?topic=<topic>` (0.1.1, `house_brain_specialist_answer.v1`, design A4 R1, owner pop-up 2026-11-09 "Approve and merge"): Systems AI's questions, answered only from what the App already holds (no Home Assistant read, no network call, no store read). Topics: `self` (its own health detail: reasons, last cycle, inputs, store) and `cooling_data` (missing plant readings, the Arbiter phase, actual mode and shadow plan, the archive day, modes learned, as of the last cycle). The query must be exactly `topic=<topic>`; anything else, or another App's topic, is a 200 `NOT_MY_TOPIC` answer. At most 5 facts of at most 160 characters, each with its evidence and age; `ANSWERED`, `NOTHING_SEEN`, `NOT_CHECKED` (with reasons) or `NOT_MY_TOPIC`. A kill-switch hold, no finished cycle, an unreadable Core API or an overdue loop is `NOT_CHECKED`, never "nothing seen"; missing readings are facts and are also named as not checked. Plant data only, nothing beyond what the page shows, and `authority: NO_EXECUTION_AUTHORITY`.
- It sends nothing to the phone; A/C Insight's watchdogs and pushes stay in YAML.
- Any other method (POST, PUT, PATCH, DELETE, HEAD, OPTIONS) gets HTTP 405 `READ_ONLY_APP`.

## Kill switch

Every cycle reads `sensor.house_brain_ai_kill_switch_effective`. FULL_STOP holds collection, learning and scoring and shows `HELD`; that is not a problem. A missing, stale, future-dated or unknown state also holds, and is reported as a problem. `READ_ONLY`, the owner's steady state, allows everything this App does, because it never writes.

## Bounds

- About 1,450 local requests a day: states each minute (1,440), plus one Supervisor lookup of Systems AI an hour at most (24), plus archive days. While catching up it reads one day a minute (up to 120 reads once); after that, 1 a day. Without Systems AI it adds only the hourly lookup. No external calls and no paid API.
- Python standard library only.
- 256 MiB address cap. App-owned SQLite (mode 0600, at most 96 MB): 5-minute rows for 120 days (about 27 MB), models, a 14-day scorecard, 600 frozen forecasts, and the start ledger (at most 1.5 MB).
- The hourly relearn reads back only the keys learning needs. With a full 120 days it peaked at 94 MB resident and about 1.4 s on the build machine, and it also completes under a 160 MiB address cap. The Pi figure is measured after install.

## Not in 0.1 to 0.3

- Any action, proposal, YAML suggestion, push, notice or Home Assistant sensor.
- Naming a part, airflow or equipment health from a start pattern. "What to check" hints are a separate owner-approved step (0.3.1) with their own governance line.
- Pad-scaling claims before the #119 supply probe.
- Weather-forecast inputs (Open-Meteo would be a new external call and its own owner decision).
- Natural acceptance: about two weeks of scorecard, read back, before any claim beyond SHADOW.

## Recovery

Stop the App; it holds nothing that needs undoing. Its database can be deleted to start learning again; the history comes back from Systems AI's archive.
