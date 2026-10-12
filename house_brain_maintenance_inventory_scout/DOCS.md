# House Brain Maintenance Inventory Scout

## Purpose

Collect one sanitized installed-App overview from Home Assistant Supervisor and publish it to the
House Brain Maintenance Broker for offline maintenance-inventory composition.

## Operation

This App is intentionally:

- `startup: once`;
- `boot: manual_only`;
- manager-role only for the Supervisor API;
- non-actuating;
- non-interactive;
- one-shot.

Configure:

- `broker_url`: the exact House Brain Maintenance Broker `/v1/inventory` HTTPS URL;
- `broker_inventory_write_key`: the dedicated inventory-only Broker capability.

Starting the App performs exactly one collection/publish attempt and exits.

The collection is evidence-honest: Supervisor `GET /addons` lists installed Apps only and carries no
`installed` field, so every row is treated as installed; a row explicitly marked `installed: false` is
excluded, while malformed or ambiguous rows (including a non-boolean `installed` value) fail the entire
attempt closed instead of disappearing from a supposedly complete inventory. Supervisor redirects are not followed while the manager-role token is
present.

## Expected successful log

`maintenance_inventory_scout=pass ... control_authority=none`

Failure logs contain bounded reason codes only. Secrets and raw Supervisor payloads are never
printed.

## Authority

This App does not install/update/restart Apps, call Home Assistant services, mutate YAML, access
devices, or perform physical actions.
