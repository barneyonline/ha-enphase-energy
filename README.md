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
- IQ Battery telemetry and BatteryConfig controls (where supported); Battery Overall Charge rounds down to a whole percentage (for example, 20.5% becomes 20%) to align with Enphase reporting
- IQ EV Charger controls and session telemetry
- IQ Microinverter connectivity, inventory, lifetime production, and optional installer-level parameter telemetry
- Site and cloud energy telemetry (including supported HEMS channels such as Heat Pump and Water Heater lifetime energy)

## Key features

- Guided onboarding for site selection and device-category enablement
- Isolate charger-status server failures during startup and normal polling, keeping fresh battery and gateway readings available. Expose endpoint retry times and retain safe incident history across restarts; see [charger-status recovery](docs/architecture.md#charger-status-recovery).
- Unified support for EV chargers, gateway, battery, and microinverter entities
- Multi-gateway topology awareness for primary/default Gateway and phase selection
- EV charging switch and session telemetry, including charge-mode aware behavior and persistent default charge-level controls when exposed by Enphase. Use the switch to start or stop charging, or the `enphase_ev.start_charging` and `enphase_ev.stop_charging` actions in automations. The Start Charging and Stop Charging button entities have been removed; replace `button.press` calls with `switch.turn_on` or `switch.turn_off`.
- Advisory firmware update entities for gateway and EV charger devices with locale-aware release-note links; the gateway entity also monitors read-only live update progress, percentage, timing, and sanitized component status when Enphase exposes it
- Heat-pump runtime status, connectivity, SG-Ready mode, power, and current-day consumption details sourced from HEMS endpoints
- Site and battery energy telemetry, including Consumption Power calculated from the available power sensors, plus derived grid-import, grid-export, and battery power sensors for Home Assistant Energy Dashboard use
- Controls retain their confirmed values and remain available while Enphase applies a change. Each gateway, battery system, and EV charger has a diagnostic **Update Status** sensor showing Pending, Unconfirmed, Failed, or Idle; its `updates` attribute separates that device’s controls, requested values, and confirmed values. System Profile and Storm Guard progress follow their controls on the gateway; the shared Storm Guard EV charging setting appears on each charger. Grid Mode, Grid Profile, Export Limit, and tariff progress appear on the gateway, which also groups the system controller. When tariff controls are on Enphase Cloud because no selected gateway has been discovered, that device has a tariff-only Update Status sensor. Charger progress remains visible through cloud refresh failures once data has been received. The former central Control Update Status sensor is removed on reload; update dashboards and automations that referenced it. Conflicting writes are rejected with an actionable error while a change is pending. Start/Stop preserves command ordering and Stop remains usable during a pending Start. Confirmation requires fresh matching readback, with extra checks bounded to ten minutes and endpoint backoff respected. Local schedule/installer drafts and the desired charging-current setting update immediately; saving or applying a draft uses the confirmation pattern. Export Limit retains its existing durable pending request and repair/recovery workflow.
- Optional IQ Battery Scheduler controls and CFG, DTG, and RBD schedule sensors
- Capability-gated PowerMatch cloud control for supported IQ Battery sites with permitted BatteryConfig write access
- Optional current site weather on the Enphase Cloud device, created only when the authenticated Enphase weather endpoint is available
- Independent per-microinverter Lifetime Energy and optional installer-level Power sensors are both off by default. Enable Lifetime Energy under Configure > Features > Device Features and Power under Advanced Features. Turning either feature off removes its per-inverter entities; turning it on discovers them again. These switches do not control the total array capacity sensors.
- Microinverter power requests run before diagnostic parameters and are paced to avoid bursts. Power refreshes every 15 minutes; diagnostic parameters refresh hourly. A rate-limit response stops the remaining requests.
- Site tariff visibility, editable rate entities, and tariff update actions
- Optional installer-only Grid Profile Control through Enphase cloud Activation,
  with country-scoped profile selection and current profile monitoring. Enable it
  under Options > Features > Advanced Features; it is disabled by default and makes
  no Grid Profile requests until enabled
- Read-only Grid Mode monitoring with a guided, OTP-confirmed control workflow under Configure > Advanced > Grid Mode and admin-only actions for scripts
- Administrator-only service actions for charger control, cloud reauthentication, live streaming, battery schedule changes, tariff updates, and Grid Profile application
- Health diagnostics, service-availability tracking, persistent stored-credential login counters, and actionable repair issues
- Read-only System Dashboard event and standing-alarm monitoring, including a
  diagnostic Problem sensor with bounded sanitized event context and optional,
  default-off Repair notifications sourced from authoritative standing alarms
- A site-level System Event History calendar on the Enphase Cloud device, with
  localized descriptions, bounded on-demand pagination, and identifier redaction
- Optional read-only VPP/ELRP monitoring for enrolled sites, with a VPP Events
  calendar and next-event start, end, type, subtype, and status sensors on the
  Enphase Cloud device. Enable it under Options > Features > Device Features;
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

- Minimum supported Home Assistant version is `2026.9.0` (Python `3.14`+).
- Users migrating from the core Enphase Envoy integration can preserve compatible Energy-dashboard history with the [Envoy History Migration](https://github.com/barneyonline/ha-enphase-energy/wiki/Envoy-History-Migration) assistant. Create a full Home Assistant backup first.

## Authentication

Sign in with your Enlighten credentials; MFA is supported. See the wiki for details.

## Cloud updates and recovery

Cloud endpoint failures are isolated so an optional service outage does not stop
unrelated telemetry. Site energy, charger session history, weather, and firmware
catalogs honor Enphase or hosting-provider rate-limit retry deadlines. Previously
valid schedules and session history are retained when responses are malformed or
incomplete; a valid empty collection still clears the corresponding cache.
Overlapping session-history pages are deduplicated before calculating daily
charging energy. Diagnostic pagination counts indicate incomplete responses
without exposing session identifiers.

Schedule writes are serialized for each charger, and older in-flight reads cannot
undo successful local edits. Cloud readback still determines confirmed state.
Public firmware catalogs are cached once per URL across integration entries in
the same Home Assistant instance. Endpoint telemetry and authentication remain
scoped to each entry.

## Documentation

Refer to the [Wiki](https://github.com/barneyonline/ha-enphase-energy/wiki) for setup,
configuration, and troubleshooting guidance.

### Descriptive Activity entries

Enable **Configure → Notifications → Enable Descriptive Activity Entries** to
add reported condition details alongside normal Home Assistant Activity entries.
The feature is **disabled by default**. See [Descriptive Activity entries](docs/descriptive_activity.md)
for supported entities and behavior.
