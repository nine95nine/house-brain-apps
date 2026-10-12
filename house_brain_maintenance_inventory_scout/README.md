# House Brain Maintenance Inventory Scout

A one-shot Home Assistant App that supplies the complete installed-App inventory needed by the
House Brain Maintenance & Reliability Orchestrator.

The Scout exists because Home Assistant Supervisor intentionally places `GET /addons` behind the
`manager` App role. The permanent Engineering Observer remains at `hassio_role: default`; the
broader Supervisor capability is isolated in this minimal, manual-only, one-shot process.

## Security boundary

The Scout:

- uses Supervisor `GET /addons` only;
- has `hassio_role: manager` only because that route requires it;
- has no Home Assistant API, Docker API, auth API, host network, host namespace, device, USB,
  UART, GPIO, udev, audio, video or filesystem-map access;
- validates the Maintenance Broker destination exactly;
- uses a dedicated inventory-only Broker capability, not the Observer's write key;
- publishes only the bounded `house_brain_app_inventory_snapshot.v1` schema;
- treats every Supervisor `GET /addons` row as installed (that route lists installed Apps only and has
  no `installed` field); excludes a row explicitly marked `installed: false`;
- fails closed on malformed/ambiguous rows (including a non-boolean `installed`) instead of silently
  dropping them;
- refuses redirects while carrying the manager-role Supervisor token;
- exposes no App options, logs, descriptions, repository URLs, Web UI URLs or arbitrary free text;
- exits after one bounded collection/publish attempt.

The App does not grant maintenance execution or physical authority.
