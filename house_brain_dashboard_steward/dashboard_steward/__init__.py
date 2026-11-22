"""House Brain Dashboard Steward: the always-on dashboard specialist of the House Brain AI family.

Observes Home Assistant registries and dashboards (read-only), finds dashboard debt and new capabilities,
asks a local model to write Lovelace YAML, and checks every candidate with deterministic validators and the
No-Loss Dashboard Evolution Contract before an owner ever sees it. It has no write path and no control
authority. See docs/architecture/DASHBOARD_STEWARD_R1_ARCHITECTURE_2026-11-08.md.
"""
from .runtime import VERSION  # noqa: F401
