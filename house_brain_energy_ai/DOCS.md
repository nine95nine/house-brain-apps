# Setup and recovery

Unreleased source candidate; native qualification and installation approval are
required. Source publication, app installation, restart and control commissioning
are separate gates. Boot is manual; only SHADOW and BUILD/REPLAY are supported.

The package uses config.yaml, an immutable base-image digest and
SOURCE_MANIFEST.sha256. Python has no third-party runtime dependencies. The
proposed architecture is aarch64 and the proposed Home Assistant minimum is
2026.10.0; those are target requirements, not installed-host evidence.

A trusted release engineer prepares a version-2 household.json bundle containing
options, tariff, banking policy, accepted identity register, private safeguards
and native adapter bindings. Retained statement/credit evidence is optional.
Preparation sources and generated bundles belong outside this public package.
The runtime requires the bundle in its app-owned /data directory. A setup-only
receiver now implements the documented Supervisor app standard-input transport;
the existing release adapter and installed-host delivery remain unqualified.

Normal options retain setup_only=false. A qualified release job can explicitly
start with setup_only=true and send one bounded JSON frame through Supervisor's
stdin route. The app receives at most one frame within 60 seconds, verifies the
packaged source manifest, request expiry (at most five minutes), bundle/source
identity and current digest, then emits a redacted receipt and exits. It creates
no SQLite database, HA client, listener, notification or equipment action. It
does not start the analytics runtime after setup. A normal start requires the
release process to restore setup_only=false separately.

READBACK reports the active digest without bundle contents. ACTIVATE uses the
existing stopped-app installer and rollback rules below. An already-active
retry never overwrites the retained previous bytes; stale installer locks need
inspection. A transport success alone is not activation proof: the release
adapter must match the fresh receipt's request, artifact, source and active
digest. Standard input adds no filesystem mapping, Docker API, Supervisor API
access or physical authority to this app. Never send private payloads through
public artifacts or log their bodies.

The bundle has fixed members, exact text hashes and a source commit. It is
bounded to 2 MB and refuses duplicate/nonfinite JSON, extra/path members,
symlink/FIFO files, unsupported tariffs and control modes. Hashes establish
integrity/custody; trusted review and installation authorization are separate.
Version-1 bundles require offline regeneration from trusted private source.
No installed version is migrated automatically.

Activation binds the whole artifact digest. Replacement additionally binds the
current digest, retains household.previous.json, writes mode 0600, fsyncs and
atomically renames under an exclusive lock. A process-death lock requires
inspection. Rollback explicitly activates the retained prior bytes against the
current digest while stopped. The evidence database is not erased or downgraded.
No activation calls Home Assistant or starts/restarts an app.

The app shares one HA states poll every minute. It reads installed credit-bank,
price, advice and monitoring surfaces through private bindings. A fresh snapshot
alone does not qualify bank freshness: source meters, seed coverage, period,
units and arithmetic are checked. It neither accumulates a competing bank nor
sends duplicate monitoring notices. Retained statements are historical billing
evidence, not hourly device-model training history or the current account state.

Every cycle first reads the House Brain AI kill switch from the same poll
(sensor.house_brain_ai_kill_switch_effective, written by the AI Access App).
FULL_STOP holds the cycle (no collection, model update, forecast or plan; the
status shows KILL_SWITCH_HOLD). READ_ONLY, the owner's steady state, allows
collection and shadow analytics but blocks status publication. ARMED_NORMAL
allows publication when enabled. A missing, stale (over 5 minutes), future or
unknown kill-switch state holds everything. The app can only hold work; it never
changes the switch. Offline --replay runs have no network and are not gated.
A start without the Supervisor-provided API token exits with one
HA_API_TOKEN_REQUIRED line before opening the database.

An optional private solar_reference adapter names existing forecast entities
(per-plane next-hour and day-ahead kWh). The next-hour estimate is frozen before
its target hour and scored against later observed solar alongside the app's own
forecast. If the existing forecast is at least 5% better over 24 or more scored
hours, the solar model is not promoted. This adds no provider, key, request or
cost; a missing, stale or non-energy plane makes the reference unavailable.

A shadow scorecard folds every cycle into a local-day row (35 days kept): distinct shadow
proposals (each counted once) with the planner's modeled benefit, blocked cycles by reason and the
latest scored forecast skill. Status shows the last 7 days. Modeled benefit is the planner's own
estimate at decision time; nothing is executed, so it is never reported as measured savings.

The plan is grandfathered Legacy ET-2 with EPR-6 banking, and leaving it is permanent. A stay-put
view values, per retained statement, the imported kWh that banked credits offset at the plan's own
base energy rates (adjustors, taxes and fees excluded: a lower bound). A what-if view stays WAITING
until 90 days of hourly grid import/export are retained and the private rate_plan_alternatives
adapter lists at least one owner-verified tariff (at most four); it then prices both plans on the
same hours, base charges only. Both carry the permanence warning and never a recommendation.

Hourly read-only insights reuse the data already kept: the overnight always-on baseline per day with
its month-over-month change and yearly cost; this calendar month's base cost so far with a projected
band (bucket netting against the bank); the cost of each recent cycle on a detection binding at its
start-time rate; solar output against the frozen existing forecast as a soiling hint (Solar Health
Watch stays the authority); overnight load as hours per 10 kWh for outage planning; a plain
paragraph per statement against the same period a year earlier, with weather, EV and always-on
shares kept unknown; and an 80% forecast interval learned on older hours and checked on newer ones.
Each one waits until it has enough data, makes no recommendation and adds no alert or request.

The page starts with an "In plain words" section and a short weekly summary built from fixed
templates (no language model). It also adds a bank runway: the next 12 billing months per bucket,
each repeating that month's most recent statement, with the December true-up payout. Energy
counters (kWh, total increasing) that stay unchanged for 3 hours in the daytime while the house uses
power are listed as possibly stuck, on the page only. A monthly solar self-use share comes from the
hourly rollups already kept. The "Why / why not?" list answers the month's projected cost, the bank
runway, the stay-put value and recent appliance-run costs.

Banked on-peak credit only offsets on-peak use, and the leftover is paid out at the December true-up
rate (far below its retail value). An on-peak credit headroom view shows the smallest projected
on-peak bank between now and the true-up: that much off-peak use moved into on-peak hours before
then would use credit that would otherwise be paid out. It is information for the pool, EV and cooling
owners, not a recommendation. An optional private weather_reference adapter names the house's existing
outdoor-temperature entity. Hourly temperatures are kept with the other rollups, a temperature-aware
daily load estimate is fitted on older days and scored on later ones, and monthly cooling degree-hours
are shown. The forecast in use and its promotion rules are unchanged.

Optional history backfill is at most 60 initial bounded entity/day requests.
Optional publication writes only the app-owned status sensor, defaults off and
adds at most 1,440 state writes/day. No equipment service route is exposed.
New external forecast, paid API and direct device-provider requests are zero.
Existing HA integrations keep their own provider budgets.

Ingress has no mapped host port. It checks ingress/loopback admission, CSRF,
request size and command-queue bounds. Owner reviews require a stable reviewed
Sense binding; display labels do not rename native Sense or grant control.
Expired planning context, overrides, missing evidence and pause block planning.
Accepted rejections and shared EV restrictions are private safeguards, not
learned identities. Model rollback persists a 24-hour promotion hold.

App-owned SQLite uses schema 1, mode 0600, seven days raw, up to 365 days hourly,
10,000 transient events and 35 days forecast audit. Corrections/checkpoints are
retained independently. Corruption or a newer schema refuses startup. Native
cold backup/restore acceptance remains required; stop the app before restoring
and retain the original privately. Confirm labels, pause, models and backfill
state after restoration. Expired action requests never become permission.

Development containment is 256 MiB address space, optional-work hold at 128 MiB
peak RSS and a 128 MiB database cap. Journal/backups need additional space. These
are not measured host budgets. Planning is at most four jobs, 96 slots and
10,000 combinations or two seconds. The private settings are delivered by House Brain
Maintenance (0.9.24 or later, request `ENERGY_AI_SETTINGS`) after the owner's Approve;
nothing else writes to this app's input.

Data health shows `IDLE` for a Sense device that reads 0 W and has not changed since it
switched off, while the Sense total is fresh (0.1.1). Idle readings are not faults and
are never stored as measurements.

Before installation, qualify the exact image on the supported architecture:
configuration schema, ingress peer/API behavior, AppArmor, start/stop/restart,
persistence, cold backup/restore and measured host CPU/RAM/storage/latency.
Local process/file mimics do not qualify Home Assistant or physical equipment.
At first-install rollback leave this app stopped and native schedules running.

Self-health (0.1.2, `house_brain_app_health.v1`): `GET /health` on the App's port answers only
peers on the internal Supervisor network (the /23 that holds the Supervisor proxy
address) and loopback, so Systems AI can read
it; the LAN is refused and nothing is written to Home Assistant. The block holds the App's
health (`OK`, `DEGRADED`, `FAILED`), its activity (`RUNNING`, `PAUSED`, `HELD`, `STARTING`),
reason codes, cycle times, input counts (good, idle, bad), peak memory and database size; no
entity ids, readings or household data. An owner pause or a kill-switch FULL_STOP is `OK`
with activity `PAUSED`/`HELD`; an unreadable kill switch, bad inputs, a resource hold or a
failed cycle is not `OK`; before the first cycle it is never `OK`. The answer is HTTP 503 only
when this App's own loop has not finished a cycle for 3 minutes; there is no Supervisor
watchdog line (owner decision 2026-10-20: report only).
