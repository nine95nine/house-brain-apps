# House Brain AI Access

**0.1.6-dev — internal HAOS App candidate. Not install-ready or live-qualified.** 0.1.6-dev (with `ha_bridge` on) also publishes `sensor.house_brain_ai_activity`, the weekly AI activity counts from AI perimeter 0.4.5. 0.1.5-dev (with `ha_bridge` on) publishes `sensor.house_brain_ai_connectors`, the AI connector sign-in heads-up from AI perimeter 0.4.4. 0.1.4-dev accepts the live Maintenance Broker 0.6.0 on the snapshot read path (the reads failed `BROKER_VERSION_UNQUALIFIED`). 0.1.3-dev added the opt-in Home Assistant kill-switch tile bridge (`ha_bridge`, off by default; see DOCS.md). 0.1.2-dev named itself to Cloudflare (live 403 fix, 2026-10-06). 0.1.1-dev added the Claude-only heartbeat mode (`claude_only`).

The approved target is the existing Home Assistant Pi (aarch64). This is a separate,
private, outbound-only connection for House Brain status, platform versions and
backup readiness. It does not expose Home Assistant or Supervisor APIs, devices,
arbitrary commands, public ports, host networking or a maintenance executor. With
`ha_bridge` on (0.1.3-dev), the App itself reads one HA helper and writes one HA sensor
for the AI kill switch, and nothing else.

The six runtime files are generated, byte-identical copies of product commit
`22d8efc83df03010e75da642cc3ba5c90bebd14b`, checked against the canonical repository
files and pinned manifest. Do not edit those copies independently. The vendor
release is OpenAI tunnel-client v0.0.14; its checksum-locked installer excludes the
unused cloudflared executable. This is not a new application protocol implementation.

The owner approved configuration/build preparation and offline qualification only.
No install, start, HA restart, real credentials, hosted tunnel or physical control
is authorized. Configuration defaults to disabled, with manual-only boot.

The current candidate adds wrapper-loss admission and bounded whole-App containment;
full container/s6/ARM64-Python acceptance remains open. The locked ARM64 native
parent is qualified in the stated QEMU/host-Python laboratory scope. Both
corrections are included in the current draft source lifecycle; no deployment
is implied.

See [DOCS.md](DOCS.md) for the security, lifecycle and remaining acceptance gates.
