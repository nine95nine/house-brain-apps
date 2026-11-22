# House Brain Dashboard Steward (service source)

The always-on dashboard specialist of the House Brain AI family.

**What it does:**
- observes Home Assistant registries and dashboards, read-only;
- finds dashboard debt and new capabilities;
- asks a local model for Lovelace YAML;
- checks every candidate with deterministic validators and the No-Loss contract before the owner sees it.

**What it never does:** write to Home Assistant, approve its own proposals, or call a non-local model.

- Architecture: `docs/architecture/DASHBOARD_STEWARD_R1_ARCHITECTURE_2026-11-08.md`
- Build record: `docs/validation/DASHBOARD_STEWARD_D0_D3_BUILD_2026-11-08.md`

## Offline tools (local files only)

```
cd apps/house_brain_dashboard_steward
python3 -m dashboard_steward audit  <dashboard.yaml> --url <dashboard-url>    # debt report
python3 -m dashboard_steward ledger <dashboard.yaml> --url <dashboard-url>    # Capability Access Ledger
python3 -m dashboard_steward check  <baseline.yaml> <candidate.yaml> --url <dashboard-url>   # validators + NDEC
```

## Service

`python3 -m dashboard_steward run --options /data/options.json --db /data/steward.sqlite` is the future App entrypoint. It needs `SUPERVISOR_TOKEN`.

**Options:**

| Option | Meaning |
|---|---|
| `model_url` | Must be loopback, private or Tailscale. A public host is refused. |
| `model_name` | The model to call. |
| `model_calls_per_day` | Default 48. |
| `generation_targets` | Empty by default. Each entry needs the owner's pre-YAML approval. |
| `main_dashboard` | 0.1.1. The owner's main dashboard, default `home-center`. Coverage, data health and pins are measured against it. |
| `port` | Read-only status page, default 8099. |

**Pages (0.1.1, all GET only, all 503 while the kill switch holds; they show the last gated live read):**

| Page | What it shows |
|---|---|
| `coverage` (`coverage.json`) | For every other dashboard: refs, dead, covered, missing, missing controls, ready, against the main dashboard. Dead = not in the registry and not in states, or unavailable/unknown for over 24 h. Ready is necessary, not sufficient: deleting still needs the owner's pop-up and an archive copy first. |
| `data-health` (`data-health.json`) | Entities on the main dashboard that are unavailable, unknown or missing, longest first, with how long (from states; the recorder database is never opened). |
| `usage` (`usage.json`) | Home Assistant's `usage_prediction/common_control` list per time of day, each marked pinned, on main or not on main. It is the App's Supervisor identity's usage, not the owner's; display only. One read per hour. |
| `export` | Download any dashboard's full config (`export/<url_path>.json` or `.yaml`) as last read. Secret-looking values become `<set>`; the file carries `live_config_sha256` to check a repository copy against Deployer scans. Saved only where the owner chooses. |

**Tests:**
- `python3 -m pytest tests/house_brain_dashboard_steward`
- mutation pass: `python3 tests/house_brain_dashboard_steward/mutants.py`
