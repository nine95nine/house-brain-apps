# House Brain Engineering Observer

## v0.2.2 scope

The Observer preserves the bounded maintenance-snapshot publisher and provider-neutral Engineering
Read Gateway worker while tightening the worker to Supervisor surfaces actually available to the
accepted `hassio_role: default` permission boundary.

The Observer remains continuously available and intentionally least-privileged. Complete all-App
enumeration is owned by the separate one-shot Maintenance Inventory Scout.

## Required configuration

- `broker_url`: exact HTTPS Maintenance Broker `/v1/snapshot` endpoint.
- `broker_write_key`: independent maintenance snapshot/read-worker capability.
- `publish_interval_seconds`: 60–300 seconds; default 90.

The Broker origin is derived only from a validated HTTPS snapshot URL. Redirect following is
disabled for read-worker requests so the bearer capability cannot be redirected to another origin.

## Active worker operations

The active remote worker catalog is intentionally limited to:

- `ha.app.info` using one explicit `/addons/<slug>/info` GET;
- `ha.network.tailscale.identity` using only default-role `.../info` evidence. Manager-only App
  log reads are intentionally not attempted; the result reports
  `tailscale_latest_startup_logs=false` / `not_collected_default_role` rather than creating
  expected permission failures.

`ha.apps.inventory` and `ha.app.stats` are not active here because their Supervisor API routes
require manager scope. This App must remain `hassio_role: default`.

## Data minimization

The read plane does **not** expose:

- App options/configuration or environment;
- generic raw logs;
- App descriptions, documentation, repository URLs, Web UI URLs or arbitrary free text;
- credentials or Cloudflare secrets;
- Home Assistant entity/service control;
- Supervisor mutation endpoints.

The existing read-only `/addons` filesystem mapping remains solely for accepted AquaRite local
source-custody inspection and does not provide Supervisor API enumeration authority.

## Reliability and failure behavior

Read requests expire after 60 seconds and are rejected locally when stale. Broker result retention
and queue depth remain bounded. Worker polling backs off while idle and resets after work.

Missing, malformed, stale, out-of-range or unexpected evidence fails closed. Exception text is never
returned over the remote read plane.

App 0.2.4 hardens fault containment (#91 fault-injection findings GAP-91-01..10):

- transport faults become bounded codes: `timeout`, `deadline_timeout` (a response still arriving
  after the 30 s total deadline), `unavailable` and `protocol_error` (truncated body, bad status
  line, over-long header); deeply nested JSON is `malformed_json`;
- the snapshot loop and the read worker log only the exception type of an unexpected fault and
  keep running; a stopped read worker is reported as `data_quality` source `read_worker`;
- an unreadable or unwritable `/data/observer-state.json` never stops publishing: a corrupt file
  restarts the session, and an unwritable volume is reported as `observer_state`;
- Supervisor strings with control characters are not forwarded; the affected section is named in
  `data_quality.reason_codes` (`*_CONTROL_CHARACTERS_REJECTED`);
- when `/jobs/info` or `/resolution/info` was not read, `resolution.healthy` is `false` and
  `JOBS_FACTS_UNOBSERVED` / `RESOLUTION_FACTS_UNOBSERVED` name the defaulted facts.

Every new fact uses the existing `data_quality` arrays, so the publisher wire identity stays
`observer_version` `0.2.2` and the Maintenance Broker schema is unchanged.

## Provider boundary

AI providers connect through the same accepted provider-neutral read contract. Provider
authentication is not authorization and may only reduce scopes. No provider receives the
Observer's Supervisor token or Broker write key.
