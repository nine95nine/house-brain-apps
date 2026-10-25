# House Brain Cooling AI

Version 0.1.0 (shadow). Boot is manual. Only `SHADOW` mode exists. Design:
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
- **Internal network only** (the /23 holding the Supervisor proxy address, and loopback; never the LAN):
  - `GET /health`: its own `house_brain_app_health.v1`, which Systems AI 0.1.2 reads.
  - `GET /digest`: one line of at most 255 characters.
  - `GET /proposals`: always empty in 0.1. Cooling AI never proposes an action; a later "tune the Arbiter" idea would be its own owner decision and a YAML change through the Cooling chat.
- It sends nothing to the phone; A/C Insight's watchdogs and pushes stay in YAML.
- Any other method (POST, PUT, PATCH, DELETE, HEAD, OPTIONS) gets HTTP 405 `READ_ONLY_APP`.

## Kill switch

Every cycle reads `sensor.house_brain_ai_kill_switch_effective`. FULL_STOP holds collection, learning and scoring and shows `HELD`; that is not a problem. A missing, stale, future-dated or unknown state also holds, and is reported as a problem. `READ_ONLY`, the owner's steady state, allows everything this App does, because it never writes.

## Bounds

- About 1,450 local requests a day: states each minute (1,440), plus one Supervisor lookup of Systems AI an hour at most (24), plus archive days. While catching up it reads one day a minute (up to 120 reads once); after that, 1 a day. Without Systems AI it adds only the hourly lookup. No external calls and no paid API.
- Python standard library only.
- 256 MiB address cap. App-owned SQLite (mode 0600, at most 96 MB): 5-minute rows for 120 days (about 27 MB), models, a 14-day scorecard, and 600 frozen forecasts.
- The hourly relearn reads back only the keys learning needs. With a full 120 days it peaked at 94 MB resident and about 1.4 s on the build machine, and it also completes under a 160 MiB address cap. The Pi figure is measured after install.

## Not in 0.1

- Any action, proposal, YAML suggestion, push or Home Assistant sensor.
- Pad-scaling claims before the #119 supply probe.
- Weather-forecast inputs (Open-Meteo would be a new external call and its own owner decision).
- Natural acceptance: about two weeks of scorecard, read back, before any claim beyond SHADOW.

## Recovery

Stop the App; it holds nothing that needs undoing. Its database can be deleted to start learning again; the history comes back from Systems AI's archive.
