# House Brain Water AI

Version 0.1.0 (shadow). Boot is manual. Only `SHADOW` mode exists. Design:
`docs/house-brain/WATER_AI_DESIGN_R1.md` (owner pop-ups 2026-10-26 "RO + yard ledger, slots
reserved", "Reserve a slot now", "Separate Apps", "Approve design").

## What it does

- **Reads, never writes.** Every request is a `GET` through the Supervisor's Core proxy:
  - `/core/api/states`, cut down at once to the listed water and garden ids. Only id, state and times are kept; attributes are dropped before anything is stored.
  - `/core/api/history/period/...` for the listed series only (RO pressure, the three active timers' valve, battery, next watering and rain delay, v2.2's Orbit totals, the garden temperature): states only, at most 26 hours a read.

  There is no POST, state write or service call anywhere in the App, and no Supervisor API (`hassio_api: false`). Nothing about people, presence or location is read. B-hyve keeps watering control.
- **RO tank, in psi only.** From the one-a-minute pressure it finds:
  - refills (rising), with their minutes, psi per hour and the pressure they settle at;
  - draws (falling fast), with their psi drop, sorted into small and large where this tank's own draw sizes fall into two groups (sizes only: which device or tap made a draw is never claimed);
  - the overnight idle bleed (psi per hour, quiet nights only).

  The slope that counts as rising or falling is learned from the device's own noise, never below 0.5 psi/h. An episode must move at least 1 psi. These limits only decide what is an episode; they are not alerts. **Gallons are not available.** They need the RO bench values (membrane rating, precharge, shut-off fraction) and a second calibration point, and they are never worked out from runtime or a ratio. The fish-room probes on the RO monitor are shown but treated as not commissioned: a disconnected leak lead reads dry, so its `off` is not proof of dry.
- **Yard ledger** for the three active hose timers (front yard, back yard by the pool, pool equipment):
  - **Runs:** each run is the valve open to closed.
  - **Gallons:** the rise in v2.2's Orbit-reported total between a run's start and the next run (or three hours after it). Until that window closes, a run with no rise yet is **waiting for Orbit**; after it, a run with no rise is **not reported**: a null Orbit volume is a gap, never runtime × GPM, and never zero. A valve that goes unavailable mid-run makes that run a gap; `opening` and `closing` neither start nor end a run.
  - **Renamed entities:** the pool-equipment timer's renamed `_2` entities are read as the same series. When both spellings hold a reading, the one that changed last wins, so a stale old entity never hides its replacement.
  - **Schedule kept:** each scheduled start is on time, missed, skipped for a rain delay, or the run was unscheduled. When the valve was a gap around the scheduled time it is **no data**, never missed. One run keeps one scheduled start.
  - **Battery:** a trend, with the date it reaches *your own* v2.2 low-battery threshold. No date without that threshold; a date more than a year out reads "beyond a year".
  - **Orbit-reported flow** per zone, with a note when the last runs reported a different flow. That note is advisory; v2.2's flow-anomaly sensor stays the alert source.
  - **Garden temperature against gallons:** context only. With no evaporation (ET0) or rain source, it never claims a water need.
- **Forecasts, scored honestly:**
  - RO time to full after a draw, against the last refill;
  - each zone's next-run gallons, against the last run (reported runs only);
  - battery in 14 days, against no change.

  Each is called a forecast only after it beat its baseline on at least 5 scored days. A forecast is frozen before what it forecasts has happened. It is scored only once the outcome is complete: a refill that has settled, and a run whose Orbit window has closed.
- **Waiting for hardware:** leak sensors (#124), a rain or ET0 source, indoor plant watering, and the 31 offline timers. Each reads not connected; none ever reads dry, OK or zero. When indoor plant watering exists, it joins as a Water AI update, and its own controller keeps control.

## History

Home Assistant keeps full detail for 40 days. Once started, Water AI reads those days for its 17 listed series, one series-day a minute, oldest first, never today: about 11 hours once, then one day a night. A renamed timer's two spellings are read together and kept as one series. The RO pressure day is read in four 6-hour pieces, so no reply comes near the 4 MB cap. It keeps everything in its own database for 120 days, at most one numeric reading a minute; valve edges and gaps are always kept. A failed read is retried at most 3 times, 15 and then 30 minutes apart, and never filled in. One missed minute in the RO pressure holds the previous reading; a longer gap stays a gap.

## Outputs

- **Ingress page** (sidebar): plain words first, full evidence behind "tap to open".
- **Internal network only** (the /23 holding the Supervisor proxy address, and loopback; never the LAN):
  - `GET /health`: its own `house_brain_app_health.v1`.
  - `GET /digest`: one line of at most 255 characters.
  - `GET /proposals`: always empty in 0.1.
- It raises no alerts and sends nothing to the phone. v2.2's problem sensors and the planned fish-room notices stay the alert sources.
- Any other method (POST, PUT, PATCH, DELETE, HEAD, OPTIONS) gets HTTP 405 `READ_ONLY_APP`.

## Kill switch

Every cycle reads `sensor.house_brain_ai_kill_switch_effective`. FULL_STOP holds collection, history reads and learning and shows `HELD`; that is not a problem. A missing, stale, future-dated or unknown state also holds, and is reported as a problem. `READ_ONLY`, the owner's steady state, allows everything this App does, because it never writes.

## Bounds

- About 1,472 local requests a day: states each minute (1,440), plus 32 history reads a night after the first catch-up (1,248 once, over about 11 hours). No external calls and no paid API.
- Python standard library only.
- 256 MiB address cap. App-owned SQLite (mode 0600, at most 64 MB): the RO minute series and the change points for 120 days (about 20 MB). If the database cannot be written, the cycle fails and is reported as a problem; the App keeps running.

## Not in 0.1

- Any action, proposal, YAML suggestion, push or Home Assistant sensor.
- RO gallons before the bench values and the second calibration point.
- A water-need claim before a rain or ET0 source.
- Leak handling before the #124 hardware.
- Indoor plants before their hardware.

## Recovery

Stop the App; it holds nothing that needs undoing. Its database can be deleted to start learning again. The last 40 days come back from the recorder; anything older is gone.
