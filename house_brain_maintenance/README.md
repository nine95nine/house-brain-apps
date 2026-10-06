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
- Core/OS updates (0.6.0, owner decision 2026-10-06): a review on the tracking issue when Core or the OS has
  an update, then an iPhone approval; only after Approve: full backup -> update (Core and OS separately, never
  automatically) -> health check -> automatic restore of Home Assistant from that backup if Core fails. The OS
  falls back to its previous version by itself (A/B) if it cannot boot.
- Never updates the Supervisor or itself, never updates Core/OS without the owner's Approve. No host port, no
  `ha` CLI, no physical control.
