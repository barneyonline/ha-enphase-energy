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
- Site and battery energy telemetry, including derived grid-import, grid-export, and battery power sensors for Home Assistant Energy Dashboard use
- Optional IQ Battery Scheduler controls and CFG, DTG, and RBD schedule sensors, exposed only when Enable Battery Scheduler is selected under integration Options > Devices
- Optional current site weather on the Enphase Cloud device, created only when the authenticated Enphase weather endpoint is available
- Site tariff visibility for next billing date and Energy-dashboard-ready current import/export price sensors, plus default-on Pricing Edits under Options > Devices > Device Features for editable rate entities; the Update Tariff action remains available for billing, rate, and structural updates
- Installer-only Grid Profile Control through Enphase cloud Activation, with country-scoped region/profile selection under Options > Advanced and current profile monitoring on the IQ Gateway
- Optional manual Grid Mode controls under Options > Advanced; the read-only Grid
  Mode sensor remains available while Grid Control Status, Request Grid Toggle OTP,
  and related service actions remain disabled until explicitly enabled
- Health diagnostics, service-availability tracking, and actionable repair issues
- Read-only System Dashboard event and standing-alarm monitoring, including a
  diagnostic Problem sensor with bounded sanitized event context and optional,
  default-off Repair notifications sourced from authoritative standing alarms
- Detailed diagnostic and inventory entities remain available but are disabled by default when they are mainly useful for troubleshooting
- Restored discovery data creates known entities early during startup; live power
  acquisition starts alongside the minimal setup refresh and is attempted within
  55 seconds, while optional feature data fills in incrementally afterward
- Device-category selection is grouped under Devices, while EV charger and battery schedulers, Pricing Edits, Weather, and the EV charger nominal-voltage fallback are grouped under Device Features in Options > Devices; default-off degraded-service and system-event Repair controls have their own Notifications page, polling has its own Polling page, and credential actions have their own titled Authentication page
- Rate-conscious microinverter telemetry uses limited-concurrency bulk reads,
  preserves fresh partial results, and exposes power plus available AC/DC,
  frequency, temperature, signal, and firmware details in a disabled-by-default
  sensor entity
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
- In v2.0.0, the integration display name changed to `Enphase Energy`.
- The integration domain remains `enphase_ev`, so existing entity IDs, automations, and scripts do not require migration.
- Users migrating from the core Enphase Envoy integration can optionally use the `Migrate Envoy history` assistant in the integration Options flow to take over compatible Energy-dashboard entity IDs. The assistant archives the migrated Envoy energy entities, swaps the entity IDs, and restores the remaining Envoy entities after the migration. Create a full Home Assistant backup first. Full steps: [Envoy History Migration](https://github.com/barneyonline/ha-enphase-energy/wiki/Envoy-History-Migration).

## Authentication

Sign in with your Enlighten credentials; MFA is supported. See the wiki for details.

Installer-only Enphase features require additional account permissions. If you own
or are authorised to service the site, see
[Requesting Enphase installer access](docs/installer_access.md) for the current
training, certification, and Enphase Support request process. Enphase makes the
final access decision, and requirements vary by region and product.

Grid Profile Control is available only when the signed-in Enphase account has installer-level Activation permissions. It uses Enphase cloud endpoints only; no local Gateway authentication or LAN access is required. Open the integration's Options flow and choose `Advanced` > `Grid Profile Control` to select a region, choose commonly used or all profiles, and confirm an apply request. Accepted changes can take up to five minutes to appear on the Grid Profile sensor.

Manual Grid Mode controls are disabled by default because they can intentionally
connect or disconnect a supported system from the grid. The read-only Grid Mode
sensor remains available. To expose Grid Control Status and Request Grid Toggle
OTP—and allow the related service actions—open the integration's Options flow and
choose `Advanced` > `Grid Mode`.

Pricing Edits are enabled by default. To hide editable tariff rate entities and
retain only read-only pricing sensors, open the integration's Options flow, choose
`Devices`, and clear `Enable Pricing Edits` under `Device Features`. The `Update
Tariff` action remains available when Pricing Edits are disabled.

## Documentation

See [device automation triggers and conditions](docs/device_automations.md) for the
automation features provided directly by the integration.

Refer to the [Wiki](https://github.com/barneyonline/ha-enphase-energy/wiki), including [Envoy History Migration](https://github.com/barneyonline/ha-enphase-energy/wiki/Envoy-History-Migration) for preserving Energy dashboard history when migrating from Enphase Envoy.
