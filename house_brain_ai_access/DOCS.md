# App operation and acceptance contract

## Current scope

The target is the owner's existing aarch64 HAOS Pi. The App is an internal candidate,
not an installation instruction. It has not been built or executed as a container,
and it has not been tested as a complete ARM64 App, under Supervisor or on the Pi.
The locked ARM64 native tunnel-client parent has now passed 16 scenarios under
recovered QEMU with a host x86-64/Python 3.13.5 child. This closes native-parent
execution only; it does not establish target Python/base/native-s6 parity.

## Configuration and secrets

Only nine options exist: `enabled`, `tunnel_id`, `tunnel_api_key`, `broker_read_key`,
`kill_switch`, `kill_switch_repair`, `perimeter_heartbeat_url`, `perimeter_heartbeat_key` and
`claude_only`.
The default is disabled with empty credentials. Enabling requires an exact tunnel
identifier and bounded ASCII keys. Unknown options, duplicate JSON keys, invalid
identities and missing credentials fail before any native process or network call.
The private connection retains exactly three fixed local scopes; none is supplied
by an RPC caller or editable as an App option.

The bootstrap reads only this App's `/data/options.json`, rejects links and unsafe
write permissions, and tightens that file to mode 0600. It does not copy credentials
to another persistent file or to command-line arguments. Supervisor still manages
App configuration. Password UI masking is not a claim of encryption. HA backups may
include configuration metadata independently of the App data directory, so no
backup exclusion is claimed to remove secrets. Encrypted-backup/restore handling
must be verified before real credentials are commissioned. Never put keys in chat,
Git, screenshots or an engineering archive.

A root bootstrap validates options, source and the native binary. The native
process and MCP child run as UID/GID 10001 with no supplementary groups. Their
environment is rebuilt from an allowlist, excluding ambient proxies, HA tokens,
custom CA settings, arbitrary commands, profiles and raw HTTP logging. The MCP
child removes the tunnel API key before loading its runtime. Parent and child are
still one App trust domain; same-UID environment minimization is not OS isolation.

## AI kill switch (#56)

`kill_switch` is the owner's control while the Home Assistant helper tile (#56 queue
item) is not yet approved. It offers only `FULL_STOP` (default: AI blocked) and
`READ_ONLY` (AI may use the three reads). `ARMED_NORMAL` is not offered: this App has
no action class that needs it.

- The accepted #56 hash-chained store lives in `/data/kill_switch` (root only, 0700).
  A new store starts at `FULL_STOP` (genesis `GENESIS_FAIL_CLOSED`).
- At every App start the root wrapper applies the chosen option as an audited
  `OWNER_LOCAL_UI` transition (`actor_ref` `ha-app-options`, reason `OWNER_ROUTINE`).
  An unchanged option writes no record. A change takes effect at the next App start.
- The wrapper then publishes a root-owned read-only copy (0755 directory, 0644 files)
  at `/run/house-brain-ai-kill-switch`. The MCP child (UID 10001) can read it but not
  change it, and it never receives write access to the store.
- The MCP core calls the accepted `guard()` with action class `READ` before every
  `tools/list` and `tools/call`. Anything except an exact `ALLOW` returns JSON-RPC
  error -32001 `AI_KILL_SWITCH_BLOCKED`. `server/discover` returns no household data
  and stays open.
- Fail closed: the view is withdrawn before the store is touched. A missing,
  unreadable, torn or tampered store, a clock that runs behind the newest record, or
  any wrapper error leaves no view, so every read is blocked
  (`KILL_SWITCH_FAILED_CLOSED`). The wrapper never reinitializes a store that does
  not verify; the owner repairs it with `kill_switch_repair` (below).
- The interlock inputs are fixed to an enabled, healthy surface with Request Guard
  `ALLOW`, because this App has no Request Guard runtime yet (#20/#58). The kill
  switch can only remove permission from them.

**One-time repair (`kill_switch_repair`, default off; owner decision 2026-09-30).** When it is on at
App start, the wrapper ignores `kill_switch`, publishes no view, and ends at `FULL_STOP`:

1. It first tries the accepted non-destructive `recover()`, which drops a torn final log line and rolls
   the state file forward from the log.
2. If the chain then verifies, the repair is recorded as an owner tighten to `FULL_STOP`
   (`OWNER_RECOVERY`).
3. Only if the chain does not verify does the accepted `reinitialize_after_tamper` move the damaged
   files aside, keeping them byte-exact as evidence with their hash bound into the new genesis, and
   start a new chain at `FULL_STOP`.

The Log shows `KILL_SWITCH_REPAIRED_FULL_STOP` with `action` `INITIALIZED`, `RECOVERED` or
`REINITIALIZED`. The owner then turns `kill_switch_repair` off and chooses `kill_switch` again. A
repair can never loosen anything by itself.

The kill switch grants nothing. `READ_ONLY` permits only the same three fixed
engineering reads; it adds no tool, scope, Home Assistant access or physical authority.

**Cloud-edge heartbeat (`perimeter_heartbeat_url` + `perimeter_heartbeat_key`, both empty by default;
owner decision 2026-10-01 "Heartbeat-only key").** When both are set, the root wrapper posts the #56
heartbeat to the Claude perimeter Worker every 60 s while the App runs. The heartbeat carries the
store's effective state, generation, time and chain head, and is signed with HMAC-SHA256. The URL must
be `https://<name>.workers.dev/v1/kill-switch/heartbeat`. The key is a heartbeat-only key: at least
32 characters, the same value as the Worker secret `HEARTBEAT_HMAC_KEY`. It can only send heartbeats.
Requests use no proxy and no redirects, and time out after 10 s. A store that does not resolve sends
nothing. When the App stops, the heartbeats stop, and the Worker blocks Claude within 5 minutes.
The Log shows only changes: `HEARTBEAT_ACCEPTED`, `HEARTBEAT_REFUSED_<status>` or
`HEARTBEAT_NOT_SENT_STORE_UNREADABLE`. It never shows the key or the body.

**Claude-only mode (`claude_only`, off by default; 0.1.1-dev, owner approval 2026-10-04).** The Claude
perimeter needs the heartbeat, and without this mode the heartbeat ran only beside the OpenAI tunnel,
which needs ChatGPT credentials. With `enabled` and `claude_only` on, the wrapper verifies the source,
applies the kill switch and then publishes only the heartbeat until the App stops. It starts no tunnel
process and makes no OpenAI connection. The three tunnel fields must stay empty
(`CLAUDE_ONLY_TUNNEL_FIELDS_SET`), and both heartbeat fields are required
(`CLAUDE_ONLY_HEARTBEAT_REQUIRED`). The Log shows `CLAUDE_ONLY_HEARTBEAT_STARTING`, then the heartbeat
changes. The mode adds no tool, scope, Home Assistant access or authority. `kill_switch` still decides:
`FULL_STOP` makes the perimeter refuse every read. When the App stops, the Worker blocks Claude within
5 minutes (no synthetic FULL_STOP is sent, so the next start is not locked out).

## Network and privileges

Outbound destinations are fixed in trusted code: OpenAI and the accepted Broker.
No Home Assistant, Supervisor, Auth, Docker, USB/serial/hardware or host-namespace
permissions, mapped HA directories, ingress or published ports are requested.
AppArmor stays enabled. Vendor health uses a private Unix socket, not a TCP
listener. The configuration is not a network firewall; final container egress and
AppArmor behavior remain platform acceptance work. No bundled cloudflared process,
Harpoon target, public noauth endpoint or shell tool is enabled.

## Lifecycle and wrapper-loss containment

The pinned base's native s6 owns retry scheduling. The root bootstrap owns one
process lifetime, guarded by a root-owned `/run` claim. The claim is created before
launch and removed only after cleanup is proved. Losing the wrapper cannot silently
permit a replacement: any retained claim blocks admission until explicit container
restart. No PID from a file is trusted for signalling.

Clean, normalized exits 0 and 1 retain the bounded policy: a five-second finish
delay, no more than three starts per ten minutes. The restart ledger and lifetime
claim are separate; neither is reset after a native-child restart. Disabled or
invalid configuration, source drift, exhausted budget, unproved cleanup and
signalled/unexpected wrapper exits request this App's s6 halt and return 125.
Failure to request halt still suppresses service restart. No HA restart API,
generic shell fallback, host shutdown or new watchdog is used.

How a halt shows in Home Assistant: just before requesting the halt, the finish
hook writes the container exit code into s6-overlay's existing
`/run/s6-linux-init-container-results/exitcode` (it never creates the file).
`enabled: false` exits 0, a plain stop. Invalid options, source drift or an
exhausted restart budget exit 78. A lost wrapper or any unexpected status exits
70. Supervisor then shows the App as failed ("exited with non-zero exit code"),
and the App log names the reason. If writing the code fails, the halt still
happens and `APP_EXIT_CODE_RECORD_FAILED` is logged. A refusal right at container
start first waits (at most five seconds, and no native process is running) for
s6-overlay's startup stage to finish. Without that wait, s6 can report its own
startup failure as exit code 1 in place of the App's code.

Keep this App's Watchdog toggle OFF. Supervisor's watchdog restarts an App that
stopped or failed unless it was stopped by hand, and a new container starts with
a fresh restart budget. This was already true when every halt exited 0.

An explicit stop is forwarded to the owned process group. TERM has a three-second
grace interval, followed by KILL and a bounded no-running-members proof. The direct
child is not reaped until signalling is complete, so its PID cannot be reused
while group signals are being sent. The cleanup budget is six seconds; s6's kill
timeout remains ten seconds, finish timeout eight, and HA App stop timeout thirty.
The separate halt request is bounded to two seconds. These source/local-process
contracts still require native s6/container/HAOS execution before release.

A running container is not proof of a connected tunnel. The startup message remains
STARTING_NOT_READY; hosted authentication and fresh Broker data need separate proof.
If the bootstrap is forcibly killed, a remaining old process is contained by the
requested whole-App shutdown, not an unverified PID-file cleanup. The guard prevents
overlap even before that platform shutdown completes; actual shutdown remains an
unperformed acceptance gate.

| Input | Required outcome |
|---|---|
| Clean native exit, including zero | Cleanup proof, claim release, bounded s6 retry |
| Wrapper killed or cleanup unproved | Claim retained, no replacement; App halt requested |
| Invalid options/source or exhausted policy | No native launch; permanent-down and App halt request |
| Explicit stop | Signal/clean owned group; supervisor desired-down remains respected |
| Halt request fails | No retry; retained claim is not silently cleared |
| Halt for a fault (not `enabled: false`) | Exit code 78 or 70 recorded before the halt; Supervisor shows failed |

## Build and custody

Use the explicit digest-pinned base in Dockerfile; no obsolete build.yaml or assumed
BUILD_FROM fallback. Runtime copies must equal the canonical ten sources (the six qualified MCP/Broker
files plus the four unchanged #56 kill-switch and interlock modules). During
image construction the installer verifies the locked release ZIP, five selected
payload files and ELF machine. It never downloads or updates itself at runtime.
The ARM64 ZIP and executable have been acquired and identity-checked. The native
ARM64 parent has also executed under pinned QEMU, paired with host x86-64 Python.
That is not target Python/base/container or signed-provenance verification. The
amd64 lock exists for lab qualification, not another commissioned household host.

The initial installer incorrectly bounded the unused cloudflared member by the
compressed-archive limit. The real ARM64 archive exposed this before an owner test.
The corrected installer allows its bounded declared size without decompressing or
installing it; a synthetic replay of the real 37,260,752-byte size is permanent.

## Remaining gates and rollback

Still required: actual image build; native s6 startup/retry/stop and owned-child
cleanup; Python 3.14/Alpine/ARM64 execution; Supervisor schema/platform validation;
base/vendor signed-provenance and current dependency scan; actual Pi capacity;
backup/secret handling; representative endurance; release/authority synchronization;
an owner runbook for repairing a kill-switch store that no longer verifies.
Real account setup and the one bounded hosted acceptance require separate approval.

Rollback now is reverting this branch candidate. Nothing has been installed in the
household. A future approved rollback would stop/remove only this new App; the
accepted Broker, Observer, Maintenance Phase A and physical systems remain untouched.
