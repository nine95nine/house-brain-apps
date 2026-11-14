# House Brain House Health AI

Version 0.1.1 (shadow; 0.1.1 adds `GET /answer`, A4). Boot is manual. Only `SHADOW` mode exists. Design:
`docs/house-brain/HOUSE_HEALTH_AI_DESIGN_R1.md` (owner approval 2026-11-08 with decisions D1–D5).

## In plain words

House Health looks at readings Home Assistant already has and tells you what they mean for the house.
It never changes anything. It does not send alerts. It shows its answers on its own page, and it keeps
score against the YAML packages that do these jobs today. Those packages keep running; replacing any
of them is a separate decision for you, made only after House Health has matched them for a while.

## What it does

- **Reads, never writes.** Every request is a `GET` through the Supervisor's Core proxy:
  - `/core/api/states`, cut down at once to the listed ids. Only id, state and times are kept. Attributes are dropped, except one number: the sun's elevation (the outage check needs it).
  - `/core/api/history/period/...` for the listed series only (the five room temperatures, house humidity and dew point, the Arbiter phase, and the UPS status, runtime, charge and load): states only, at most 26 hours a read.

  There is no POST, state write or service call anywhere in the App, and no Supervisor API (`hassio_api: false`). Nothing about people, presence or location is read.
- **Rooms.** For the living room, master bedroom, Ledger's, Lake's and Mari's rooms it learns the normal temperature for six times of day (midnight–4, 4–8, 8–noon, noon–4, 4–8, 8–midnight) over the last 7 days, the same way Learning 1.4 does. A room is **drifting** when it is 2.5 °F or more from its normal for 30 minutes; it is back to normal once within 1.5 °F for 30 minutes. A missing reading is **unknown**, never normal. Each drift shows what the Cooling Arbiter was doing when it started and now. It needs 3 days before it judges a time of day.
- **Mold risk.** A slow 30-day average of the whole house's humidity and dew point:
  - humidity: **watch** at 60 %, **risk** at 70 %;
  - dew point: **watch** at 60 °F, **risk** at 65 °F;
  - it steps back down only 3 points (humidity) or 2 °F (dew point) below the line.

  These thresholds are provisional. Without enough data (80 % of the 30 days) it says **unknown**, never OK. Real-time humidity stays with Cooling's stale-air advisory and Humid Boost. It is house-level only: there are no per-room humidity sensors yet.
- **Outages.** Every minute it classes the moment as **power**, **internet (ISP)**, **home network (LAN)**, none or unknown. It uses the same rules as the Network Outage Advisory 1.0 package, then your current facts: the **Orbi main router and the ISP modem are not on the UPS**; the Orbi satellite, ZEN16, Pi and network switch are. So in a power cut, Wi-Fi from the main router and the internet are expected to go down. If the modem still answers while the UPS is on battery, utility power is reaching the modem, so House Health will not call it a full power cut; it shows that as a named difference from the package.
- **UPS.** It learns how the battery is ageing **only from real power cuts**, never from a pulled plug. A cut shorter than 2 minutes may be the UPS's own self-test and is not used. A cut that starts at 95 % charge or more, at low load, and drains at least 10 points gives an estimate of the full runtime. That is compared with the new battery: 160 minutes at 4 % load (battery replaced 2026-07-24). At 80 % or more of new it is **healthy**; below, **watch** (provisional). The live runtime estimate is checked against two floors you approved: **45 minutes is the alarm floor**, **60 minutes the planning floor**.
- **Replacement calendar.** Each item is **good**, **soon**, **due** or **blocked**, and a blocked item names what is missing:
  - A/C filter and evap pad, from their life-remaining sensors;
  - UPS battery: replaced 2026-07-24 plus 1,095 days (about 2029-07-23); soon within 60 days; a weak measured runtime can only bring it earlier;
  - smoke/CO batteries: last checked plus 365 days, once you have confirmed the date;
  - smoke and CO alarm end of life: **blocked: date unknown** until you read the manufacture date off each alarm (a photo is enough);
  - Ecobee room-sensor batteries: blocked: no battery date recorded.

  It adds no low-battery alert: Maintenance owns low batteries and offline devices.
- **Idle or stale.** For the room temperatures, humidity and dew point it tells a sensor that is still reporting but has not changed for unusually long (more than twice its longest normal quiet spell over the week, never less than 6 hours) from one that has stopped reporting. Why a sensor stopped is Systems AI's job.
- **Reserved, not built:** heat safety (it needs Cooling AI's time constant first), per-room mold, leak-device lifecycle, Orbi/MoCA quality, and the aquarium, ceiling fans and CO2. Each shows as reserved on the page and in the digest.
- **Not here:** the always-on power creep (Energy AI), solar health (Energy AI), leak detection (Water AI), App health (Systems AI).

## Scorecard (shadow)

Counted, never acted on. A bar met only means a YAML retirement can be put to you as its own question.

| Measure | Against | Bar |
|---|---|---|
| Room normal temperature per time of day | Learning 1.4 | median difference 0.2 °F or less over 14 days |
| Outage class | NOA 1.0 | same class in every real outage (at least 3), never a power cut called internet |
| Due dates | Predictive Maintenance 2.3 | same status, or a named reason |
| UPS runtime | the next real power cut | within ±15 % |

It also checks, every minute, that its copy of the NOA 1.0 rules gives exactly what the live package shows.

## History

Home Assistant keeps full detail for about 40 days. Once started, House Health reads those days for its 12 listed series, one series-day a minute, oldest first, never today: about 8 hours once, then 12 reads a night. It keeps up to 45 days in its own database, at most one numeric reading a minute; gaps are always kept and never filled in. A failed read is retried at most 3 times, 15 and then 30 minutes apart.

## Outputs

- **Ingress page** (sidebar): plain words first, full evidence behind "tap to open".
- **Internal network only** (the /23 holding the Supervisor proxy address, and loopback; never the LAN):
  - `GET /health`: its own `house_brain_app_health.v1`.
  - `GET /digest`: one line of at most 255 characters.
  - `GET /proposals`: always empty in 0.1.
  - `GET /answer?topic=<topic>` (0.1.1, `house_brain_specialist_answer.v1`, design A4 R1, owner pop-up 2026-11-09 "Approve and merge"): Systems AI's questions, answered only from what the App already holds (no Home Assistant read, no network call, no store read). Topics: `self` (its own health detail: reasons, last cycle, inputs, store) and `power_network` (the outage class, an open outage episode, NOA 1.0's live class and the learned UPS view; never a room or a person; as of the last cycle). The query must be exactly `topic=<topic>`; anything else, or another App's topic, is a 200 `NOT_MY_TOPIC` answer. At most 5 facts of at most 160 characters, each with its evidence and age; `ANSWERED`, `NOTHING_SEEN`, `NOT_CHECKED` (with reasons) or `NOT_MY_TOPIC`. A kill-switch hold, no finished cycle, an unreadable Core API or an overdue loop is `NOT_CHECKED`, never "nothing seen"; a missing or unusable input is a fact and is also named as not checked. Nothing beyond what the page shows, and `authority: NO_EXECUTION_AUTHORITY`.
- No Home Assistant entity, no push, no alert.
- Any other method (POST, PUT, PATCH, DELETE, HEAD, OPTIONS) gets HTTP 405 `READ_ONLY_APP`.

## Kill switch

Every cycle reads `sensor.house_brain_ai_kill_switch_effective`. FULL_STOP holds collection, history reads and learning and shows `HELD`; that is not a problem. A missing, stale, future-dated or unknown state also holds, and is reported as a problem. `READ_ONLY`, the owner's steady state, allows everything this App does, because it never writes.

## Bounds

- About 1,452 local requests a day: states each minute (1,440), plus 12 history reads a night after the first catch-up (468 once, over about 8 hours). No external calls and no paid API. $0.
- Python standard library only.
- 256 MiB address cap. App-owned SQLite (mode 0600, at most 64 MB). If the database cannot be written, the cycle fails and is reported as a problem; the App keeps running.

## Recovery

Stop the App; it holds nothing that needs undoing. Its database can be deleted to start learning again. The last 40 days come back from the recorder; anything older is gone.
