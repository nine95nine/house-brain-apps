# Changelog

## 0.1.7-dev

Explain an `UNUSUAL` week (owner pop-up 2026-10-18 "Both of the above", Work Board #942 row 74). The privacy-filtered
snapshot that AI assistants read exports only single values (text or numbers), so they could see `UNUSUAL` but not
which AI or why. `sensor.house_brain_ai_activity` now also carries the same facts as flat attributes:
- `flag_summary`: for example `claude:MANY_REFUSED`, several joined with `, `; `none` when nothing is unusual;
  empty (null) while `UNKNOWN`.
- Per AI (`claude`, `chatgpt`, `grok`): `<ai>_requests`, `<ai>_refused`, `<ai>_rate_limited`, 0 when that AI did
  not read.
- **Unchanged:** the state, the thresholds, the nested `providers` and `flags` (the Maintenance App reads those),
  the bridge allowlist (still four pairs), the heartbeat and every other sensor.
- Showing the new attributes to AI assistants also needs Inspector 0.2.12 (read policy allowlist).
- **Version:** the User-Agent is `house-brain-ai-access/0.1.7-dev`.

## 0.1.6-dev

Weekly AI activity (owner pop-up 2026-10-18 "Build all 4 parts (Recommended)", Work Board #942 row 72). With
`ha_bridge` on, this App now also publishes `sensor.house_brain_ai_activity`:
- **Source.** AI perimeter 0.4.5 answers each accepted heartbeat with the header `X-HB-Activity`: per AI
  (Claude, ChatGPT, Grok) the last 7 days' requests, answered, refused, rate-limited, tool calls and items.
  Counts only: no sensor id, value, prompt or answer.
- **States:**
  - `UNUSUAL`: at least one flag below.
  - `NORMAL`: AI reads this week, no flag.
  - `QUIET`: no AI request in 7 days.
  - `UNKNOWN`: no fresh perimeter answer for three hours.
- **Flags** (attribute `flags`, one per AI):
  - `MANY_REFUSED`: at least 10 and at least a fifth of that AI's requests refused (for example the kill
    switch or a limit).
  - `RATE_LIMITED`: the per-minute cap was hit.
  - `NEW_AI`: that AI read for the first time, for 7 days after. The first summary after the upgrade records
    the AIs already reading without flagging them (`/data/ai_activity_seen.json`).
- **Attributes:** the window (`from`, `to`), `total_requests` and per active AI its counts. They change at most
  hourly.
- **Phone:** the Maintenance App sends the Sunday line and a push when the state is `UNUSUAL`.
- **Bridge allowlist:** one more (method, path) pair, `POST /core/api/states/sensor.house_brain_ai_activity`,
  for four in all. Still no service call and no other entity; nothing on the AI side changes.
- **Unchanged:** the heartbeat body, signature, key, cadence and the 204 check. An older perimeter sends no
  header, and then the sensor is simply not written.
- **Version:** the User-Agent is `house-brain-ai-access/0.1.6-dev`.

## 0.1.5-dev

AI connector heads-up (owner pop-up 2026-10-13 "Approve, alerts via Maintenance"). The claude.ai connector
dropped without warning on 2026-10-13. With `ha_bridge` on, this App now publishes
`sensor.house_brain_ai_connectors`:
- **Source.** AI perimeter 0.4.4 answers each accepted heartbeat with the header `X-HB-Connectors`. Per
  connector (Claude, ChatGPT, Grok) it gives the last good sign-in or refresh, the expiry (90 days idle)
  and the last refused refresh.
- **States, worst first:**
  - `SIGN_IN_FAILED`: a refresh was refused after the last good one.
  - `EXPIRING_SOON`: 14 days or less left.
  - `OK`.
  - `NO_CONNECTOR`: none signed in yet.
  - `UNKNOWN`: no fresh perimeter answer for an hour.
- **Attributes:** per connector the state, days left, expiry day, last-used day and reconnect link
  (`https://claude.ai/customize/connectors` for Claude), plus `needs_owner`. The attributes change at most
  once a day.
- **Phone heads-up:** the Maintenance App turns this sensor into a notification.
- **Bridge allowlist:** one more (method, path) pair, `POST /core/api/states/sensor.house_brain_ai_connectors`,
  for three in all. Still no service call and no other entity; nothing on the AI side changes.
- **Unchanged:** the heartbeat body, signature, key, cadence and the 204 check. An older perimeter sends no
  header, and then the sensor is simply not written.
- **Version:** the User-Agent is `house-brain-ai-access/0.1.5-dev`.

## 0.1.4-dev

Live fix (2026-10-11): the snapshot read path accepts the live Maintenance Broker 0.6.0. The handler
allowed only `relay.version` `0.3.0` and `0.4.0`, so every snapshot read through the tunnel failed with
`BROKER_VERSION_UNQUALIFIED` once the Broker moved past 0.4.0. From 0.4.0 to 0.6.0 the Broker's
`core.mjs` changed only its version string, and the `GET /v1/snapshot` body is unchanged (0.6.0 changed
only how the route signs callers in). The allowlist stays exact: `0.3.0`, `0.4.0`, `0.5.0`, `0.5.1`,
`0.5.2`, `0.6.0`. Any other or future version, and any other schema, is still refused. The Observer
allowlist is unchanged: Observer 0.2.3 and 0.2.4 still publish `observer_version` `0.2.2`. One runtime
file changes (`runtime/tools/house_brain_ai_snapshot_handlers.py`, byte-identical to
`tools/house_brain_ai_snapshot_handlers.py` at product commit `a123f0eb`), so the runtime manifest,
`MANIFEST_SHA` and the App tree change, and the heartbeat User-Agent is now
`house-brain-ai-access/0.1.4-dev`. No option, permission, route or authority changes. The ARM64
qualification is re-run on the new tree.

## 0.1.3-dev

HA kill-switch tile bridge (owner approval 2026-10-10 "Approve design, build it";
`docs/architecture/AI_KILL_SWITCH_HA_BRIDGE_R1.md`). New option `ha_bridge` (default off, optional in the
options file, so older option files stay valid). When on, every 30 s the wrapper reads
`input_select.house_brain_ai_kill_switch`, applies it to the local #56 store through the store's own
transition rules as `OWNER_LOCAL_UI` (`ha-kill-switch-tile`), and publishes
`sensor.house_brain_ai_kill_switch_effective`. `FULL_STOP` applies at once; `READ_ONLY` only when the App
option `kill_switch` is `READ_ONLY` (the ceiling); `ARMED_NORMAL` is refused
(`HA_BRIDGE_ARMED_NORMAL_REFUSED`) and never loosens. HA errors never loosen. The heartbeat already reads
the store, so a tile `FULL_STOP` reaches the edge. Permission change: `homeassistant_api: true`, the only
one. The Supervisor token is used for exactly two (method, path) pairs, checked before any I/O; a static
test refuses any other `/api/` path in the App source. The heartbeat User-Agent is now
`house-brain-ai-access/0.1.3-dev`. Runtime files and the native binary are unchanged; the App tree
changes, so the ARM64 qualification is re-run on the new tree.

## 0.1.2-dev

Live fix (2026-10-06): the kill-switch heartbeat now sends `User-Agent: house-brain-ai-access/0.1.2-dev`.
Cloudflare's edge answers 403 to Python's default "Python-urllib" user agent before the perimeter Worker
runs (the same edge refusal the Maintenance App fixed in 0.5.1 and the Inspector hit on its first export),
so every heartbeat would have been refused. Nothing else changes: same options, same signature, same
single URL, no redirects, no proxy.

## 0.1.1-dev

Claude-only heartbeat mode (owner approval 2026-10-04 "Build it"). New option `claude_only` (default off).
With `enabled` and `claude_only` on, the wrapper verifies the source, applies the kill switch and publishes
only the #56 edge heartbeat to the Claude perimeter until the App stops: no tunnel process, no OpenAI
egress, and no tunnel or Broker capability accepted (`CLAUDE_ONLY_TUNNEL_FIELDS_SET`). Both heartbeat
fields are required (`CLAUDE_ONLY_HEARTBEAT_REQUIRED`). Before this, the heartbeat ran only beside the
OpenAI tunnel, so the Claude connector could not be used without ChatGPT credentials. The option is
optional in the options file, so older option files stay valid. The runtime files and the image's
native binary are unchanged; the App tree changes, so the ARM64 qualification is re-run on the new tree.

## 0.1.0-dev

Initial owner-approved HAOS App packaging candidate. Preserves the six qualified
runtime files; adds disabled/manual-only configuration, pinned vendor installation,
source admission, non-root process launch, private health socket and bounded s6
restart admission. Initial source/local process qualification only. The later
ARM64-parent scope below does not establish complete-image or HAOS acceptance.

### Cloud-edge kill-switch heartbeat (same internal candidate, 2026-10-01)

Owner decision 2026-10-01 "Heartbeat-only key (Recommended)": the new options `perimeter_heartbeat_url`
and `perimeter_heartbeat_key` are empty by default (no heartbeat, no egress). When both are set, the
root wrapper posts the #56 edge heartbeat, signed with HMAC-SHA256, to the Claude perimeter Worker
every 60 s. The heartbeat follows `docs/architecture/AI_KILL_SWITCH_CLOUD_EDGE_PROPAGATION_R1.md`.
Requests use verified TLS, no proxy and no redirects. Both options are optional in the options file,
so older option files stay valid. The App tree changes; the ARM64 qualification is re-run on the new
tree.

### Kill-switch view hardening and one-time repair (same internal candidate, 2026-09-30)

The wrapper refuses to publish the kill-switch view unless its parent directory is its own and not
group/world-writable. New option `kill_switch_repair` (default off; owner decision "One-time repair
setting"): at App start it runs the accepted `recover()`, then an owner tighten to `FULL_STOP`, or
`reinitialize_after_tamper` for a chain that does not verify. It always ends at `FULL_STOP` with no
view and never loosens.

### #56 kill-switch READ gate (same internal candidate, 2026-09-30)

Owner decisions 2026-09-30: build the kill-switch READ check before the ARM64 run,
and control it with an App setting until the HA helper tile exists. Adds the
`kill_switch` option (`FULL_STOP` default, `READ_ONLY`). The wrapper keeps the
accepted #56 store in `/data/kill_switch`, applies the option as an audited owner
transition at start, and publishes a root-owned read-only view for the MCP child.
The MCP core refuses `tools/list` and `tools/call` with `AI_KILL_SWITCH_BLOCKED`
unless `guard(action_class=READ)` returns `ALLOW`. The runtime grows from six to ten
byte-identical sources; the four added #56/interlock modules are unchanged. This
changes the App tree, so the earlier soak and lifetime evidence describe the
previous tree only. No install, credential or physical authority.

### Consolidated containment and ARM64 qualification (same internal candidate)

Retain a root-owned lifetime claim across wrapper loss or unproved cleanup; prevent
overlapping replacement processes. Reserve the direct-child PID until group
signalling completes. Request App-only s6 shutdown for signalled/unexpected wrapper
exits and keep permanent-down on halt failure. Eighteen new tests and the consolidated
20-fault/43-test App challenge qualify only local boundaries. Included in the
draft source lifecycle, not deployed.

The lab ARM64 adapter verifies QEMU/native identities and original argv0 handling.
Its sixteen-scenario result covers the native ARM64 parent with an x86-64 Python
child, not the complete App image. No core source, YAML or vendor pin changed.

### HA-visible halt signal (same internal candidate, 2026-09-29 R2)

A whole-App halt for a fault now exits the container non-zero: 78 for an
admission, configuration or restart-budget refusal and 70 for a lost wrapper or
an unexpected status. `enabled: false` still exits 0. The code is written to the
existing s6-overlay container-results file before the unchanged halt request, and
a failed write never skips the halt. A refusal at container start waits (at most
5 s) for s6 stage 2 to finish, so s6 cannot replace the code with its own
stage-2 failure exit 1. Not deployed.
