# House Brain Pool AI

Version 0.1.0 (shadow). Boot is manual. Only `SHADOW` mode exists. Design:
`docs/house-brain/POOL_AI_DESIGN_R1.md` (owner approval 2026-11-08 with decisions D1–D3).

## In plain words

Pool AI learns your pool from the house's own history. It works out how fast this pool uses chlorine,
how well the salt cell is doing, when salt will run low, and what each pump run costs. It checks itself
against what really happens and keeps score.

It never changes anything. It does not touch the AquaRite, the pump or Jandy, and it sends no alerts. The
Advisor's advice, your test strip and the morning digest stay in charge. Pool AI shows its answers on its
own page only.

## What it needs from you

Nothing new. Keep logging water tests the way you do now: type FC, CYA and pH, then tap **Log Pool Water
Test**. About **two tests a week** for four weeks lets it learn. The page shows the day the next test would
help most (D1). It never reminds you or counts missed days.

## What it does

- **Reads, never writes.** Every request is a `GET` through the Supervisor's Core proxy:
  - `/core/api/states`, cut down at once to 20 listed ids. Only id, state and times are kept; every attribute is dropped.
  - `/core/api/history/period/...` for 13 listed series only: states only, at most 26 hours a read.

  There is no POST, state write or service call anywhere in the App, and no Supervisor API (`hassio_api: false`). Nothing about people, presence or location is read.
- **Chlorine demand.** Between two logged tests, the chlorine changes by what the cell made minus what the pool used. Pool AI measures what the cell made from the AquaRite's generation rate (output while the pump runs), leaving out spa time. From the pairs of tests it learns two numbers:
  - the **cell rate** (ppm of chlorine per generation hour);
  - the **demand** (ppm the pool uses per day).

  It starts from nothing: never from the old CELL-5 production figure, which stays quarantined. Until it has **8 usable pairs** it says **"learning (n of 8 tests)"**. If your pump schedule is the same every day, the two numbers can't be told apart yet; it then shows only the net change per day, and says why.
- **Forecast and score.** It forecasts the FC of your next test, using only the tests before it. When you log the test, the forecast is scored. The forecast is never advice.
- **Salt cell.**
  - **Efficiency:** the cell rate learned now against the rate it first learned. Below 80 % reads **falling**, a cleaning or wear signal shown beside Insight's hours.
  - **Dates:** Insight's own hours, turned into dates at the rate the cell really runs: "10,000 h around <month year>" and "next cleaning around <date>" (500 h or 90 days, whichever comes first). Insight keeps the reminder; Pool AI adds no second one.
- **Salt runway.** The days until salt falls below 3,200 ppm, from the pool-mode salt trend. Salt you logged with "Log salt added" is taken out of the trend. A drop of 150 ppm or more within three days is a **step**: with rain in the two days before, it is marked **rain dilution** (like the 1 November storm, 2,800 to 2,600). A step restarts the trend. While salt is already below 3,200 it says so (Insight's "salt to add" applies). Hidden water loss stays the salt balance package's job.
- **Pump runs.** Runs come from the pool pump switch. The #111 limits you approved on 31 October apply: a run under 30 minutes is **short**, a start within 10 minutes of the last stop is a **rapid restart**. A switch blip under 10 minutes is ignored.
  - **Energy:** a run gets a kWh figure only when Sense shows the pump's step at both the start and the stop (800–1,100 W, at least 4 readings each side). Otherwise it reads **not corroborated**. Whole-house Sense often can't see the pump, so expect many of those.
  - **Cost:** the APS base energy price for each minute on or off peak (no adjustors or taxes). If the on-peak flag was unknown during the run, no cost is shown.
- **Water temperature: missing.** The Ondilo probe isn't decision-grade until its calibration is complete, and the Jandy pool temperature reads unknown while the pump runs. That Jandy check stays your open equipment check (D3). Anything that needs water temperature says "blocked: no trusted water temperature". The old Ondilo probe (ids without `_2`) belongs to the spa and is never read as pool water. ORP is never turned into chlorine.
- **Reserved, not built:** temperature-dependent demand; LSI/CSI scale risk (needs TA, calcium hardness and TDS inputs, a separate YAML decision, D2); Ondilo local data (#1455); the #111 Phase 2/3 pump protections (they stay in their own lane). Each shows as reserved on the page and in the digest.
- **Not here:** chlorine advice (the Advisor), the cleaning reminder (Insight), hidden water loss (salt balance), AquaRite link health (health alerts), App health (Systems AI).

## Scorecard (shadow)

Counted, never acted on. A bar met only means a next step can be put to you as its own question.

| Measure | Bar |
|---|---|
| FC forecast vs your next logged test | median error 0.5 ppm or less, 90 % within 1.0 ppm, and **never** "OK" when the test shows low (low = below the Advisor's minimum: 7.5 % of CYA, at least 1 ppm) |
| Salt trend | the trend predicts the salt 28 days later within 50 ppm (no step in between), 90 % of the time |
| Pump kWh per run | the typical pump step predicts each corroborated run within 15 %; never a figure for an uncorroborated run |

## History

Home Assistant keeps full detail for about 40 days. Pool AI reads those days for its 13 series, one
series-day a minute, oldest first, never today: about 8.5 hours once, then 13 reads a night. It keeps up
to 45 days of readings in its own database (at most one number a minute; gaps kept, never filled in).
Your tests, the test pairs, the daily salt and the pump runs are kept longer (up to 400–500 of each), so
learning carries past 45 days. A failed read is retried at most 3 times, 15 and then 30 minutes apart.

## Outputs

- **Ingress page** (sidebar): plain words first, full evidence behind "tap to open".
- **Internal network only** (the /23 holding the Supervisor proxy address, and loopback; never the LAN):
  - `GET /health`: its own `house_brain_app_health.v1`.
  - `GET /digest`: one line of at most 255 characters.
  - `GET /proposals`: always empty in 0.1.
- No Home Assistant entity, no push, no alert.
- Any other method (POST, PUT, PATCH, DELETE, HEAD, OPTIONS) gets HTTP 405 `READ_ONLY_APP`.

## Kill switch

Every cycle reads `sensor.house_brain_ai_kill_switch_effective`. FULL_STOP holds collection, history reads and learning and shows `HELD`; that is not a problem. A missing, stale, future-dated or unknown state also holds, and is reported as a problem. `READ_ONLY`, the owner's steady state, allows everything this App does, because it never writes.

## Bounds

- About 1,453 local requests a day: states each minute (1,440), plus 13 history reads a night after the first catch-up (507 once). No external calls and no paid API. $0.
- Python standard library only.
- 256 MiB address cap. App-owned SQLite (mode 0600, at most 64 MB). If the database cannot be written, the cycle fails and is reported as a problem; the App keeps running.

## Recovery

Stop the App; it holds nothing that needs undoing. Its database can be deleted to start learning again. The last 40 days come back from the recorder; older tests and pairs are gone.
