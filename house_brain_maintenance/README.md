# House Brain Maintenance

Owner-approved App updates and maintenance jobs for the House Brain Home Assistant estate
(#223 / #454). Design, decisions and threat model: `docs/architecture/HOUSE_BRAIN_MAINTENANCE_APP_R1.md`.

- App updates: review -> iPhone approval -> single-App backup -> update -> health watch ->
  automatic restore on failure. Optional night-time auto-install of low-risk bug-fix updates.
- Scout jobs: `ROTATE_SCOUT_KEY` (prepare / activate), `RUN_SCOUT_ONCE`, `CHECK_BROKER` (0.5.3: read-only, no
  approval; the live Broker version from its public `/healthz`), `APP_LOG_WINDOW` (0.5.4: read-only, no approval;
  one installed House Brain App's log for one boot, filtered to a UTC window and plain keywords, scrubbed, capped).
- Supervisor manager role, used only through a pinned route allowlist (`hbm/ha.py`); mutating
  update routes open for exactly one App at a time.
- Never updates Home Assistant Core/OS/Supervisor or itself. No host port, no `ha` CLI, no
  physical control.
