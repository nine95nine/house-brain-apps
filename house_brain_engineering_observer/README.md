# House Brain Engineering Observer v0.2.2

Least-privilege local half of the House Brain Engineering Read Gateway.

## Authority

This App observes engineering state only. It deliberately has:

- Supervisor API access with `hassio_role: default` only;
- `homeassistant_api: false`, so generic entity/state and service APIs are not exposed;
- no Docker API or Auth API;
- no host network or host namespaces;
- no mapped devices, UART, USB, GPIO, udev, kernel modules, audio, or video;
- no arbitrary shell-command interface;
- no App start/stop/restart/update/install/uninstall calls;
- no entity/service-call or physical authority.

`boot: auto` is intentional so the read-only Observer recovers after HAOS reboot. This changes
availability, not control authority.

## Active live-read operations

The default-role worker executes exactly two operation-bound read paths:

1. `ha.app.info` — sanitized runtime/security posture for one explicitly named App using
   `/addons/<slug>/info`.
2. `ha.network.tailscale.identity` — specialized bounded identity evidence using only default-role
   `/host/info`, `/network/info`, and `/addons/<tailscale-slug>/info` GETs. It intentionally
   does not read App startup logs; exact MagicDNS FQDN remains separate accepted evidence when
   available.

The following operations are intentionally **not** active in this default-role process:

- `ha.apps.inventory`;
- `ha.app.stats`.

Home Assistant Supervisor reserves the all-App `/addons` overview and App `/stats` surfaces for
manager scope. Complete App inventory is therefore isolated in the separate one-shot
`house_brain_maintenance_inventory_scout` App rather than widening this permanent Observer.

Generic raw App logs, App options/configuration, documentation, arbitrary files, free-form
descriptions and arbitrary Supervisor proxying remain excluded.

The existing maintenance snapshot/source-custody behavior is preserved. Accepted artifact policy is
kept in `contracts/house_brain_accepted_artifacts.v1.json`.
