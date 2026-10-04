# Changelog

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
