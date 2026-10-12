# Changelog

## 0.1.2 — Real Supervisor App-list shape (2026-09-29)

- fix: Supervisor `GET /addons` rows have no `installed` field (the route lists installed Apps only;
  verified Supervisor 2024.06.0 to 2026.09.3). 0.1.1 required that field and failed its first live run
  with `reason=addons_inventory_installed_invalid` before publishing anything. A missing field now means
  installed; an explicit `false` is still excluded; any non-boolean value still fails closed;
- tests now use the exact Supervisor 2026.09.3 row key set;
- still reports `producer_version` `0.1.0`, the exact value the live Broker 0.3.0 accepts; base image,
  permissions, AppArmor, options and Broker contract unchanged.

## 0.1.1 — Base image security refresh (2026-09-29)

- rebuild on Home Assistant base `3.14-alpine3.24-2026.08.0` (digest-pinned), replacing `2026.06.1`
  (Trivy, linux/arm64: 1 CRITICAL + 95 HIGH → 0 CRITICAL + 50 HIGH);
- no code change: still reports `producer_version` `0.1.0`, the exact value the live Broker 0.3.0 accepts.

## 0.1.0 — 2026-09-18

- Initial one-shot, privilege-isolated all-App inventory producer.
- Exact `GET /addons` Supervisor read only.
- Dedicated inventory-only Broker credential and endpoint.
- Strict destination, schema, size, privacy and authority fences.
