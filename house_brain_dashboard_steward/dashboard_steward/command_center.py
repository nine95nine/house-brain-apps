"""Home Command Center v0.1-R1: the D3 candidate for a NEW, separate dashboard ``home-command-center``.

Owner pre-YAML approval 2026-11-08 (pop-up "Approve candidate YAML"): repository candidate only; the existing
``home-dashboard`` and every other dashboard stay untouched; not installed, not default, no deploy.

Provenance: authored in this repository by Claude (Dashboard Steward build chat) as a deterministic builder,
so the committed YAML is reproducible and checkable. It was not written by the runtime model.

Design rules (directive §4, §9):
* one Home view: pinned Favorites (6), House status, Attention (conditional; never the only place a safety
  signal appears), Climate now, Cooling shown as intent / permission / observed (never merged), Go to;
* eight category subviews plus Advanced & Engineering, each two taps from Home at most;
* read-only: no toggle, no perform-action; the only actuator shown is the thermostat with explicit
  more-info actions (its dialog shows Home Assistant's own controls, as on the existing dashboards);
* pool chemistry is shown only as status; values with their trust gates stay on Systems.
"""
from __future__ import annotations

URL = "home-command-center"
VERSION = "0.1-R1"
EXISTING_DASHBOARDS = {          # url_path -> label; verified by the Deployer scan of 2026-11-02 (#561)
    "house-brain": "House Brain",
    "dashboard-systems": "Systems",
    "environmental-dashboard": "Environmental",
    "engineering": "Engineering",
    "apocalypse-aews": "Preparedness (AEWS)",
    "home-dashboard": "Previous Home (kept)",
}
NONE = {"action": "none"}
MORE = {"action": "more-info"}


def _tile(entity, name=None, **extra):
    t = {"type": "tile", "entity": entity}
    if name:
        t["name"] = name
    t.update(extra)
    return t


def _control_tile(entity, name=None):
    """An actuator shown read-first: every action explicit, nothing toggles from the card."""
    return _tile(entity, name, tap_action=MORE, hold_action=MORE, double_tap_action=NONE, icon_tap_action=MORE)


def _go(name, icon, path):
    return {"type": "button", "name": name, "icon": icon, "show_state": False,
            "tap_action": {"action": "navigate", "navigation_path": path},
            "hold_action": NONE, "double_tap_action": NONE}


def _section(heading, cards, icon=None):
    h = {"type": "heading", "heading": heading}
    if icon:
        h["icon"] = icon
    return {"type": "grid", "cards": [h] + cards}


def _alert(entity, name):
    return {"type": "conditional", "conditions": [{"condition": "state", "entity": entity, "state": "on"}],
            "card": _tile(entity, name, color="red")}


CATEGORIES = [
    ("climate", "Climate", "mdi:thermostat"),
    ("pool-spa", "Pool & Spa", "mdi:pool"),
    ("safety", "Doors & Safety", "mdi:shield-home"),
    ("water", "Water & Irrigation", "mdi:water"),
    ("energy", "Energy & Solar", "mdi:solar-power"),
    ("vehicles", "Vehicles", "mdi:car-electric"),
    ("environment", "Environment", "mdi:weather-partly-cloudy"),
    ("advanced", "Advanced & Engineering", "mdi:cog"),
]

COOLING = [   # intent / permission / observed: separate tiles, never one green light
    ("binary_sensor.cooling_arbiter_v2_control_active", "Arbiter in control (intent)"),
    ("binary_sensor.cooling_arbiter_auto_permitted", "Auto permitted (qualification)"),
    ("binary_sensor.house_brain_ac_condenser_running", "A/C condenser running (observed power)"),
    ("binary_sensor.evap_power", "Evap cooler power (observed)"),
    ("binary_sensor.cooling_inputs_stale", "Cooling inputs stale"),
    ("binary_sensor.ecobee_data_stale", "Thermostat data stale"),
]
SAFETY = [
    ("binary_sensor.any_smoke_detected", "Smoke detected"),
    ("binary_sensor.any_co_detected", "CO detected"),
    ("binary_sensor.co_reading_stale", "CO reading stale"),
    ("binary_sensor.living_room_smoke", "Living room smoke"),
    ("binary_sensor.master_bedroom_smoke", "Main bedroom smoke"),
    ("binary_sensor.living_room_carbon_monoxide", "Living room CO"),
    ("binary_sensor.master_bedroom_carbon_monoxide", "Main bedroom CO"),
    ("binary_sensor.ups_on_battery", "UPS on battery"),
    ("binary_sensor.ups_low_battery", "UPS battery low"),
    ("binary_sensor.freeze_protection", "Freeze protection"),
]
RO = "fish_room_ro_tank_pressure_monitor_"
WATER_RO = [
    (f"sensor.{RO}ro_tank_pressure", "RO tank pressure"),
    (f"sensor.{RO}ro_tank_pressure_trend", "RO pressure trend"),
    (f"binary_sensor.{RO}fish_room_leak_ro_monitor", "Fish room leak"),
    (f"binary_sensor.{RO}ro_monitor_online", "RO monitor online"),
    (f"sensor.{RO}fish_room_tank_water_temperature_ro_monitor", "Tank water temperature"),
    (f"sensor.{RO}fish_room_air_humidity_ro_monitor", "Fish room humidity"),
]
IRRIGATION = [
    ("binary_sensor.house_brain_irrigation_watering_active", "Watering now"),
    ("binary_sensor.house_brain_irrigation_fault", "Irrigation fault"),
    ("binary_sensor.house_brain_irrigation_flow_anomaly", "Flow anomaly"),
    ("binary_sensor.house_brain_irrigation_connectivity", "Irrigation connectivity"),
]
PINS = [{"view": "home", "section": 0, "card": i} for i in range(1, 7)]
CRITICAL = ("binary_sensor.house_needs_attention", "binary_sensor.house_critical", "binary_sensor.any_smoke_detected",
            "binary_sensor.any_co_detected", f"binary_sensor.{RO}fish_room_leak_ro_monitor")
ALLOWED_CONTROLS = frozenset({"climate.ecobee_thermostat"})   # shown more-info only (owner approval note)


def _sub(path, title, icon, sections):
    return {"title": title, "path": path, "icon": icon, "subview": True, "type": "sections", "max_columns": 3,
            "back_path": f"/{URL}/home", "sections": sections}


def build() -> dict:
    home = {"title": "Home", "path": "home", "icon": "mdi:home", "type": "sections", "max_columns": 4, "sections": [
        _section("Favorites", [
            _tile("sensor.average_indoor_temperature", "Inside"),
            _tile("sensor.outdoor_temperature_combined", "Outside"),
            _tile("sensor.pool_water_verdict", "Pool"),
            _tile("sensor.solar_power", "Solar now"),
            _tile("sensor.tesla_status", "Tesla"),
            _tile("binary_sensor.house_needs_attention", "Needs attention"),
        ], "mdi:star"),
        _section("House", [
            _tile("binary_sensor.house_critical", "Critical"),
            _tile("binary_sensor.house_decision_urgent", "Decision urgent"),
            _tile("binary_sensor.house_decision_blocked", "Decision blocked"),
            {"type": "markdown", "content": "No alert below does **not** prove the house is healthy. "
                                            "Unknown or stale data shows as its own tile."},
        ], "mdi:home-heart"),
        _section("Attention", [_alert(e, n) for e, n in SAFETY[:2] + [WATER_RO[2]] + SAFETY[7:8] + SAFETY[9:10]]
                 + [_alert("binary_sensor.house_brain_irrigation_fault", "Irrigation fault")], "mdi:alert"),
        _section("Climate now", [
            _tile("sensor.average_indoor_temperature", "Indoor average"),
            _tile("sensor.indoor_dew_point", "Indoor dew point"),
            _control_tile("climate.ecobee_thermostat", "Thermostat"),
            _tile("weather.forecast_home", "Forecast"),
        ], "mdi:thermometer"),
        _section("Cooling", [_tile(e, n) for e, n in COOLING[:4]], "mdi:snowflake"),
        _section("Go to", [_go(t, i, f"/{URL}/{p}") for p, t, i in CATEGORIES], "mdi:view-grid"),
    ]}
    links = lambda keys: [_go(EXISTING_DASHBOARDS[k], "mdi:open-in-app", f"/{k}") for k in keys]  # noqa: E731
    subs = [
        _sub("climate", "Climate", "mdi:thermostat", [
            _section("Inside", [_tile("sensor.average_indoor_temperature", "Indoor average"),
                                _tile("sensor.cooling_arbiter_v2_indoor_house_humidity", "Indoor humidity"),
                                _tile("sensor.indoor_dew_point", "Indoor dew point"),
                                _control_tile("climate.ecobee_thermostat", "Thermostat")]),
            _section("Cooling: intent, permission, observed", [_tile(e, n) for e, n in COOLING]),
            _section("More", links(["house-brain", "dashboard-systems"]))]),
        _sub("pool-spa", "Pool & Spa", "mdi:pool", [
            _section("Status", [_tile("sensor.pool_water_verdict", "Pool verdict"),
                                _tile("sensor.pool_water_temperature_mixed", "Water temperature"),
                                _tile("binary_sensor.pool_water_temperature_disagreement", "Temperature sources disagree"),
                                _tile("binary_sensor.pool_monitor_stale", "Pool monitor stale"),
                                _tile("binary_sensor.spa_running", "Spa running")]),
            _section("Chemistry (advisory)", [
                {"type": "markdown", "content": "Chemistry values and their trust gates are on **Systems**. "
                                                "These flags are advisory and not trusted while the monitor is stale."},
                _tile("binary_sensor.pool_ph_out_of_range", "pH out of range (advisory)"),
                _tile("binary_sensor.pool_sanitizer_low", "Sanitizer low (advisory)")]),
            _section("More", links(["dashboard-systems", "house-brain"]))]),
        _sub("safety", "Doors & Safety", "mdi:shield-home", [
            _section("Alarms", [_tile(e, n) for e, n in SAFETY[:7]]),
            _section("Power and freeze", [_tile(e, n) for e, n in SAFETY[7:]]),
            _section("More", links(["apocalypse-aews"]))]),
        _sub("water", "Water & Irrigation", "mdi:water", [
            _section("RO tank (fish room)", [_tile(e, n) for e, n in WATER_RO]),
            _section("Irrigation", [_tile(e, n) for e, n in IRRIGATION]),
            _section("More", links(["house-brain"]))]),
        _sub("energy", "Energy & Solar", "mdi:solar-power", [
            _section("Now", [_tile("sensor.solar_power", "Solar now"),
                             _tile("sensor.home_solar_coverage_now", "Solar coverage"),
                             _tile("binary_sensor.aps_on_peak", "APS on-peak"),
                             _tile("binary_sensor.energy_brain_importing", "Importing"),
                             _tile("binary_sensor.energy_brain_exporting", "Exporting"),
                             _tile("binary_sensor.energy_brain_attention_needed", "Energy attention")]),
            _section("Today", [_tile("sensor.solar_daily_energy", "Solar today")]),
            _section("More", [_go("Energy panel", "mdi:lightning-bolt", "/energy")] + links(["house-brain"]))]),
        _sub("vehicles", "Vehicles", "mdi:car-electric", [
            _section("Tesla", [_tile("sensor.tesla_status", "Status"),
                               _tile("binary_sensor.tesla_plugged_in", "Plugged in"),
                               _tile("binary_sensor.tesla_charging_now", "Charging"),
                               _tile("binary_sensor.tesla_on_peak_charging", "Charging on-peak"),
                               _tile("sensor.tesla_power", "Charge power"),
                               _tile("sensor.tesla_data_age", "Data age"),
                               _tile("binary_sensor.house_brain_tesla_energy_data_fresh", "Energy data fresh")]),
            _section("More", links(["house-brain"]))]),
        _sub("environment", "Environment", "mdi:weather-partly-cloudy", [
            _section("Outside", [_tile("weather.forecast_home", "Forecast"),
                                 _tile("sensor.outdoor_temperature_combined", "Outdoor"),
                                 _tile("sensor.average_outdoor_temperature", "Outdoor average")]),
            _section("More", links(["environmental-dashboard"]))]),
        _sub("advanced", "Advanced & Engineering", "mdi:cog", [
            _section("Dashboards", links(list(EXISTING_DASHBOARDS))),
            _section("System", [_go("Settings", "mdi:cog", "/config"), _go("History", "mdi:chart-line", "/history"),
                                _go("Logbook", "mdi:format-list-bulleted", "/logbook")])]),
    ]
    return {"title": "Home Command Center", "views": [home] + subs}


def entities() -> list:
    from . import lovelace as L
    return sorted(L.walk(build()).entity_ids())
