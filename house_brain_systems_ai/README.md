# House Brain Systems AI

The read-only overseer of the House Brain AI family (layer 2 of three). It reads each House Brain
App's self-health block, the Supervisor's `info` routes and Home Assistant's own health surfaces;
learns baselines; forecasts database, disk and memory headroom with honest bands; groups related
problems into incidents; explains what changed; and suggests fixes that only Maintenance can carry
out after the owner's Approve. It also keeps the cooling season's 5-minute history for 120 days,
copied read-only from the recorder before its 40-day purge.

It has no hands: no POST anywhere, no Home Assistant state write, no service call, Supervisor role
`default`. Python standard library only; no private settings.

See [DOCS.md](DOCS.md).
