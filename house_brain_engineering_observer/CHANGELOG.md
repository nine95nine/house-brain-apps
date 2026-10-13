# Changelog

## 0.2.5 — Store-ready: unused run.sh removed (2026-10-13)

- remove the unused `run.sh` compatibility launcher and its two Dockerfile references (the
  `COPY run.sh /run.sh` line and `/run.sh` in the `chmod 0555` line); the image `ENTRYPOINT`
  already starts `/usr/local/bin/python3 /opt/house-brain-engineering-observer/observer_entrypoint.py`
  directly and never ran `run.sh`;
- reason: the House Brain Apps store privacy gate refuses shell scripts, and the Observer is
  moving into the store (owner pop-up 2026-10-13 "Observer 0.2.5 without it");
- unchanged: every Python module, `DOCS.md`, `README.md`, `apparmor.txt` (its `/run.sh rix`
  rule now names a path absent from the image and grants nothing; dropping it would be a
  separate AppArmor change), the publisher wire identity `observer_version` `0.2.2`, snapshot
  schema, Broker destination rules, `hassio_role: default`, base image and every other
  `config.yaml` field; no new permission, route, dependency or control authority.

## 0.2.4 — Fault-containment fixes for #91 GAP-91-01..10 (2026-09-29, repository candidate)

- contain `http.client` protocol errors (IncompleteRead, BadStatusLine, LineTooLong) at the
  Supervisor, Tailscale, Broker publish and read-worker call sites as `protocol_error`
  (GAP-91-01); add a last-resort per-iteration guard to the snapshot loop (type name only);
- contain deeply nested JSON (`RecursionError`) at every decoder as `malformed_json` /
  `broker_invalid_json` (GAP-91-02);
- keep the read worker alive on any exception and report a stopped worker as `data_quality`
  source `read_worker` (`READ_WORKER_STOPPED`) (GAP-91-03);
- read response bodies against a 30 s total deadline (`deadline_timeout`) so a trickling peer
  cannot hold a read open (GAP-91-04);
- a non-UTF-8/corrupt state file restarts the session; an unwritable `/data` no longer stops
  publishing and is reported as `observer_state` (`OBSERVER_STATE_NOT_PERSISTED`) (GAP-91-05);
- split `timeout` from `unavailable` (GAP-91-06, Observer half; the Inspector half is separate);
- null Supervisor strings carrying C0/DEL control characters and name the section in
  `data_quality.reason_codes` instead of forwarding them for the Broker to reject (GAP-91-07);
- `resolution.healthy` is `false` unless `/resolution/info` was observed, and unobserved jobs /
  resolution are named `JOBS_FACTS_UNOBSERVED` / `RESOLUTION_FACTS_UNOBSERVED` (GAP-91-09);
- unchanged: publisher wire identity `observer_version` `0.2.2`, snapshot schema and key set,
  Broker destination rules, `hassio_role: default`, AppArmor profile, base image and every
  other `config.yaml` field; no new permission, route, dependency or control authority.

## 0.2.3 — Base image security refresh (2026-09-29)

- rebuild on Home Assistant base `3.14-alpine3.24-2026.08.0` (digest-pinned), replacing `2026.06.1`
  (Trivy, linux/arm64: 1 CRITICAL CVE-2025-68121 in `/usr/bin/tempio` + 95 HIGH → 0 CRITICAL + 50 HIGH);
- no code change: the running software, its catalog and its privilege boundary are exactly 0.2.2, and it
  still reports `observer_version` `0.2.2` so the live Maintenance Broker 0.3.0 keeps accepting its snapshots.

## 0.2.2 — Supervisor role-boundary correction

- preserve the permanent Observer at `hassio_role: default`;
- remove manager-only `ha.apps.inventory` and `ha.app.stats` from the active read-worker catalog;
- tighten the generic Supervisor client allowlist to default-role `.../info` paths;
- make `ha.app.info` use direct `/addons/<slug>/info` lookup without all-App enumeration;
- move complete App inventory to the separate one-shot Maintenance Inventory Scout;
- preserve maintenance snapshot publication, source custody, Tailscale identity, Broker origin
  restrictions and no-control authority.

## 0.2.1 — HAOS startup hardening

- override the Home Assistant base image inherited `/init` entrypoint with the exact Observer Python coordinator;
- add the Python 3.14 standard-library directory read permission required by the enforced AppArmor profile;
- preserve the v0.2 read-only Supervisor GET boundary, Broker destination restrictions, publication schema, and no-control authority.

## 0.2.0 — repository candidate

- preserve the accepted maintenance snapshot publisher and Tailscale identity collector;
- add sanitized all-installed-App inventory, per-App runtime/security posture and per-App stats;
- activate four operation-bound Supervisor GET reads with local expiry enforcement;
- add adaptive idle polling and redirect-safe Broker requests;
- harden Broker queue backpressure, terminal-state retention and operation-bound result schemas;
- fix Supervisor `usb` handling to derive presence from the documented list type;
- switch App boot policy from `manual_only` to `auto` for unattended read-only recovery;
- retain `homeassistant_api: false`, Docker/Auth API off, host namespaces off and no physical authority.

## 0.1.0

Initial repository candidate.
