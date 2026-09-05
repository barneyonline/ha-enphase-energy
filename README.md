# Enphase Energy — Home Assistant Custom Integration

<!-- Badges -->
[![Release](https://img.shields.io/github/v/release/barneyonline/ha-enphase-energy?display_name=tag&sort=semver)](https://github.com/barneyonline/ha-enphase-energy/releases)
[![Stars](https://img.shields.io/github/stars/barneyonline/ha-enphase-energy)](https://github.com/barneyonline/ha-enphase-energy/stargazers)
[![License](https://img.shields.io/github/license/barneyonline/ha-enphase-energy)](LICENSE)

[![Tests](https://img.shields.io/github/actions/workflow/status/barneyonline/ha-enphase-energy/tests.yml?branch=main&label=tests)](https://github.com/barneyonline/ha-enphase-energy/actions/workflows/tests.yml)
[![codecov](https://codecov.io/gh/barneyonline/ha-enphase-energy/graph/badge.svg?token=ichJ6LKzFK)](https://codecov.io/gh/barneyonline/ha-enphase-energy)
[![Hassfest](https://img.shields.io/github/actions/workflow/status/barneyonline/ha-enphase-energy/hassfest.yml?branch=main&label=hassfest)](https://github.com/barneyonline/ha-enphase-energy/actions/workflows/hassfest.yml)

[![Quality Scale](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fbarneyonline%2Fha-enphase-energy%2Fmain%2Fcustom_components%2Fenphase_ev%2Fmanifest.json&query=%24.quality_scale&label=quality%20scale&cacheSeconds=3600)](https://developers.home-assistant.io/docs/integration_quality_scale_index)
[![HACS](https://img.shields.io/badge/HACS-Default-41BDF5.svg)](https://hacs.xyz)

[![Open Issues](https://img.shields.io/github/issues/barneyonline/ha-enphase-energy)](https://github.com/barneyonline/ha-enphase-energy/issues)
![Development Status](https://img.shields.io/badge/development-active-success?style=flat-square)

[![Enphase Service Status](https://img.shields.io/endpoint?url=https%3A%2F%2Fcdn.jsdelivr.net%2Fgh%2Fbarneyonline%2Fha-enphase-energy%40service-status%2Fstatus_badge.json&cacheSeconds=60)](https://github.com/barneyonline/ha-enphase-energy/wiki/Service-Status-History)

Cloud-based Home Assistant integration for Enphase Energy systems.

> [!IMPORTANT]
> This is an unofficial community project. It is not affiliated with, endorsed by, or supported by Enphase Energy.
>
> The integration relies on undocumented Enphase APIs. Those APIs may change or stop working without notice, which can break features until the integration is updated.

## Supported device categories

- IQ Gateway / System Controller entities and controls
- IQ Battery telemetry and BatteryConfig controls (where supported)
- IQ EV Charger controls and session telemetry
- IQ Microinverter connectivity, inventory, lifetime production, and optional installer-level parameter telemetry
- Site and cloud energy telemetry (including supported HEMS channels such as Heat Pump and Water Heater lifetime energy)

## Key features

- Guided onboarding for site selection and device-category enablement
- Unified support for EV chargers, gateway, battery, and microinverter entities
- Multi-gateway topology awareness for primary/default Gateway and phase selection
- EV charging controls and session telemetry, including charge-mode aware behavior and persistent default charge-level controls when exposed by Enphase
- Advisory firmware update entities for gateway and EV charger devices with locale-aware release-note links; the gateway entity also monitors read-only live update progress, percentage, timing, and sanitized component status when Enphase exposes it
- Heat-pump runtime status, connectivity, SG-Ready mode, power, and current-day consumption details sourced from HEMS endpoints
- Site and battery energy telemetry, including a non-negative Current Power Consumption sensor averaged from Enphase consumption buckets, plus derived grid-import, grid-export, and battery power sensors for Home Assistant Energy Dashboard use
- Optional IQ Battery Scheduler controls and CFG, DTG, and RBD schedule sensors
- Capability-gated PowerMatch cloud control for supported IQ Battery sites with permitted BatteryConfig write access
- Optional current site weather on the Enphase Cloud device, created only when the authenticated Enphase weather endpoint is available
- Independent Microinverter Lifetime Energy and optional installer-level power telemetry
- Site tariff visibility, editable rate entities, and tariff update actions
- Optional installer-only Grid Profile Control through Enphase cloud Activation,
  with country-scoped profile selection and current profile monitoring. Enable it
  under Options > Devices > Device Features; it is disabled by default and makes
  no Grid Profile requests until enabled
- Read-only Grid Mode monitoring with a guided, OTP-confirmed control workflow under Configure > Advanced > Grid Mode and admin-only actions for scripts
- Administrator-only service actions for charger control, cloud reauthentication, live streaming, battery schedule changes, tariff updates, and Grid Profile application
- Health diagnostics, service-availability tracking, and actionable repair issues
- Read-only System Dashboard event and standing-alarm monitoring, including a
  diagnostic Problem sensor with bounded sanitized event context and optional,
  default-off Repair notifications sourced from authoritative standing alarms
- A site-level System Event History calendar on the Enphase Cloud device, with
  localized descriptions, bounded on-demand pagination, and identifier redaction
- Optional read-only VPP/ELRP monitoring for enrolled sites, with a VPP Events
  calendar and next-event start, end, type, subtype, and status sensors on the
  Enphase Cloud device. Enable it under Options > Devices > Device Features;
  it is disabled by default and makes no VPP service requests until enabled
- Detailed diagnostic and inventory entities remain available but are disabled by default when they are mainly useful for troubleshooting
- Restored discovery data creates known entities early during startup; live power
  acquisition starts alongside the minimal setup refresh and is attempted within
  55 seconds, while optional feature data fills in incrementally afterward
- Rate-conscious microinverter telemetry runs no more than once every 15 minutes,
  uses limited-concurrency bulk reads, preserves fresh partial results, and
  exposes power plus available AC/DC, frequency, temperature, signal, and
  firmware details when available
- Broad localization support across all user-facing integration strings

Localized strings cover English (default plus US, Canada, Australia, New Zealand, and Ireland variants), French, German, Spanish, Italian, Dutch, Swedish, Danish, Finnish, Norwegian Bokmal, Polish, Greek, Romanian, Czech, Hungarian, Bulgarian, Latvian, Lithuanian, Estonian, and Brazilian Portuguese.

## Screenshots

Screenshots below are from a mixed Enphase site and show multiple supported device categories.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/setup-dark.png">
  <source media="(prefers-color-scheme: light)" srcset="docs/images/setup-light.png">
  <img alt="Add integration flow showing category-based device selection (gateway, battery, EV chargers, and microinverters)" src="docs/images/setup-light.png">
</picture>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/devices-dark.png">
  <source media="(prefers-color-scheme: light)" srcset="docs/images/devices-light.png">
  <img alt="Device overview showing Enphase entities grouped across battery, EV charger, gateway, microinverters, and cloud" src="docs/images/devices-light.png">
</picture>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/gateway-controls-dark.png">
  <source media="(prefers-color-scheme: light)" srcset="docs/images/gateway-controls-light.png">
  <img alt="Gateway controls card with site operation controls" src="docs/images/gateway-controls-light.png">
</picture>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/battery-controls-dark.png">
  <source media="(prefers-color-scheme: light)" srcset="docs/images/battery-controls-light.png">
  <img alt="Battery controls card with profile and reserve controls" src="docs/images/battery-controls-light.png">
</picture>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/microinverters-sensors-dark.png">
  <source media="(prefers-color-scheme: light)" srcset="docs/images/microinverters-sensors-light.png">
  <img alt="Microinverter device sensors with per-inverter lifetime production telemetry" src="docs/images/microinverters-sensors-light.png">
</picture>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/charger-controls-dark.png">
  <source media="(prefers-color-scheme: light)" srcset="docs/images/charger-controls-light.png">
  <img alt="EV charger controls card with charge mode, amps control, and charge actions" src="docs/images/charger-controls-light.png">
</picture>

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/cloud-sensors-dark.png">
  <source media="(prefers-color-scheme: light)" srcset="docs/images/cloud-sensors-light.png">
  <img alt="Cloud sensor entities with site-level energy and connectivity telemetry" src="docs/images/cloud-sensors-light.png">
</picture>

## Quick install (HACS)

1. HACS -> Integrations -> Enphase Energy
2. Install and restart Home Assistant
3. Add the integration and sign in

Manual install steps: see the wiki Installation page.

## Compatibility

- Minimum supported Home Assistant version is `2026.6.0` (Python `3.14`+).
- Users migrating from the core Enphase Envoy integration can preserve compatible Energy-dashboard history with the [Envoy History Migration](https://github.com/barneyonline/ha-enphase-energy/wiki/Envoy-History-Migration) assistant. Create a full Home Assistant backup first.

## Authentication

Sign in with your Enlighten credentials; MFA is supported. See the wiki for details.

## Data updates and recovery

Core telemetry updates independently of optional cloud services. Startup enrichment
preserves newer charging changes, and changing device options recreates the runtime
from detached discovery data so known entities can return before optional requests
finish. Incomplete inverter discovery retains existing devices until a complete
inventory response is available.

Instantaneous site measurements become unavailable after 15 minutes of a core
outage. Battery and heat-pump measurements expire after 30 minutes without a
successful family update, even if other cloud services keep responding. Historical
energy totals remain available; current-power and VPP data retain their own
source-specific freshness policies.

Daily heat-pump energy uses the source day's reset time, including its timezone,
so midnight starts a new statistics cycle while same-day corrections remain valid.
Invalid nonfinite inverter energy readings retain the last valid measurement.
Diagnostic timestamps and repeated detail payloads remain visible as live entity
attributes but are excluded from recorder history where they duplicate telemetry.

## Action targets

Charging, battery schedule, grid-profile, refresh, and authentication actions
accept the entity targets offered in Developer Tools → Actions. Site actions
resolve the selected Enphase entity to its site; charging actions resolve it to
its charger. Device, area, floor, and label targets are also supported for these
actions. Charging actions ignore unrelated devices selected through an area, floor,
or label, but reject explicitly selected invalid devices. Actions operating on a
single site reject selections spanning multiple sites.
Existing explicit `device_id`, `site_id`, and `config_entry_id` fields remain
supported where offered by the action.

Actions remain registered while the integration is unloaded or retrying setup,
so Home Assistant can still validate automations. Calling an action requires its
target config entry to be loaded; unavailable targets return a validation error.

```yaml
action: enphase_ev.validate_schedule
target:
  entity_id: switch.my_discharge_to_grid_schedule
data:
  schedule_type: dtg
```

## Live-stream actions

`enphase_ev.start_live_stream` and `enphase_ev.stop_live_stream` are
administrator-only, site-scoped actions for IQ EV Charger live updates. Select an
Enphase entity, device, area, floor, or label belonging to the intended site, or
enter the numeric Enphase site ID under Advanced options. A selected entity only
identifies its owning site; it does not limit updates to that entity.

When writing YAML manually, `device_id` means the Home Assistant device-registry
ID. Use `site_id` for the numeric identifier shown by Enphase.

```yaml
action: enphase_ev.start_live_stream
target:
  entity_id: sensor.iq_battery_battery_available_power
```

## Documentation

Refer to the [Wiki](https://github.com/barneyonline/ha-enphase-energy/wiki) for setup,
configuration, and troubleshooting guidance.
