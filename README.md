<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/readme-banner-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="docs/images/readme-banner-light.svg">
  <img src="docs/images/readme-banner-light.svg" alt="Enphase Energy — your energy system in Home Assistant. Community integration." width="1200">
</picture>

<p align="center">
  <a href="https://github.com/barneyonline/ha-enphase-energy/releases"><img src="https://img.shields.io/github/v/release/barneyonline/ha-enphase-energy?display_name=tag&amp;sort=semver" alt="Latest release"></a>
  <a href="https://hacs.xyz"><img src="https://img.shields.io/badge/HACS-Default-41BDF5.svg" alt="HACS Default"></a>
  <a href="https://github.com/barneyonline/ha-enphase-energy/wiki/Service-Status-History"><img src="https://img.shields.io/endpoint?url=https%3A%2F%2Fcdn.jsdelivr.net%2Fgh%2Fbarneyonline%2Fha-enphase-energy%40service-status%2Fstatus_badge.json&amp;cacheSeconds=60" alt="Enphase service status"></a>
</p>

<p align="center">Monitor solar, batteries and charging. Control your Enphase system from Home Assistant.</p>

<p align="center">
  <a href="#get-started">Install</a> ·
  <a href="#at-a-glance">Screenshots</a> ·
  <a href="https://github.com/barneyonline/ha-enphase-energy/wiki">Documentation</a>
</p>

> [!IMPORTANT]
> This is an unofficial community project, not affiliated with, endorsed by, or supported by Enphase Energy. It relies on undocumented cloud APIs that may change or stop working without notice.

## At a glance

EV charging modes, current and session controls alongside battery reserve, grid charging and schedules.

<!-- Inline images wrap onto separate rows on narrow screens. Keep their natural proportions. -->
<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/images/charger-controls-dark.png">
    <source media="(prefers-color-scheme: light)" srcset="docs/images/charger-controls-light.png">
    <img src="docs/images/charger-controls-light.png" alt="EV charging controls: charger switch, charge mode, charging current, default charge level, and battery charging options" width="320" align="top">
  </picture>
  &nbsp;
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/images/battery-controls-dark.png">
    <source media="(prefers-color-scheme: light)" srcset="docs/images/battery-controls-light.png">
    <img src="docs/images/battery-controls-light.png" alt="Battery controls: reserve, shutdown level, grid charging, minimum battery for EV charging, PowerMatch, and discharge schedule" width="320" align="top">
  </picture>
</p>

<details>
<summary>More screenshots · devices, gateway, solar and cloud</summary>

### Connected devices

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/devices-dark.png">
  <source media="(prefers-color-scheme: light)" srcset="docs/images/devices-light.png">
  <img src="docs/images/devices-light.png" alt="Home Assistant device overview with IQ Battery, IQ EV Charger, IQ Gateway, IQ Microinverters, and Enphase Cloud" width="1000">
</picture>

### Gateway controls and microinverter sensors

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/images/gateway-controls-dark.png">
    <source media="(prefers-color-scheme: light)" srcset="docs/images/gateway-controls-light.png">
    <img src="docs/images/gateway-controls-light.png" alt="Gateway controls: export limit, system profile, Storm Guard, and Storm Alert opt in and opt out" width="320" align="top">
  </picture>
  &nbsp;
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/images/microinverters-sensors-dark.png">
    <source media="(prefers-color-scheme: light)" srcset="docs/images/microinverters-sensors-light.png">
    <img src="docs/images/microinverters-sensors-light.png" alt="Microinverter array sensors: current power and lifetime energy for the North and West arrays" width="320" align="top">
  </picture>
</p>

### Cloud energy sensors

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/images/cloud-sensors-dark.png">
    <source media="(prefers-color-scheme: light)" srcset="docs/images/cloud-sensors-light.png">
    <img src="docs/images/cloud-sensors-light.png" alt="Cloud sensors: battery, consumption, grid and production power, site energy totals, EV charging, and system event history" width="320">
  </picture>
</p>

</details>

## Get started

1. Find **Enphase Energy** in HACS.
2. Install and restart Home Assistant.
3. Add the integration and sign in with your Enlighten credentials. MFA is supported.

Requires **Home Assistant 2026.10.0+** (Python 3.14+).

See the [Wiki](https://github.com/barneyonline/ha-enphase-energy/wiki) for installation, setup and troubleshooting.

<details>
<summary>Device selection</summary>

Choose the device categories to include. Availability depends on your site and account access.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/setup-dark.png">
  <source media="(prefers-color-scheme: light)" srcset="docs/images/setup-light.png">
  <img src="docs/images/setup-light.png" alt="Device selection with Gateway, Battery, EV Chargers, Heat Pump, and Microinverters categories" width="580">
</picture>

</details>

<details>
<summary>Supported devices and features</summary>

### Supported device categories

- IQ Gateway / System Controller entities and controls
- IQ Battery telemetry and BatteryConfig controls (where supported)
- IQ EV Charger controls and session telemetry
- IQ Microinverter connectivity, inventory, lifetime production, and optional installer-level parameter telemetry
- Site and cloud energy telemetry (including supported HEMS channels such as Heat Pump and Water Heater lifetime energy)

### Key features

- Guided setup with site, device-category, and multi-gateway selection
- Site and battery energy monitoring for the Home Assistant Energy Dashboard
- EV charging controls, actual charging status, and session history
- Battery profiles, reserve settings, schedules, and supported PowerMatch controls
- Array monitoring and optional per-microinverter energy and power sensors
- Heat-pump status, SG-Ready mode, and energy monitoring
- Gateway and charger firmware information with release-note links
- Editable tariffs, optional installer Grid Profile controls, and OTP-confirmed Grid Mode changes
- Storm Alert controls and guided battery profile recovery through Repairs
- System events, alarms, and calendars, plus optional VPP/ELRP monitoring
- Optional site weather from Enphase
- Cloud recovery, health diagnostics, and automation actions
- User-facing strings localized in more than 20 languages

</details>

<details>
<summary>Migrating from Enphase Envoy</summary>

Users migrating from the core Enphase Envoy integration can preserve compatible Energy-dashboard history with the [Envoy History Migration](https://github.com/barneyonline/ha-enphase-energy/wiki/Envoy-History-Migration) assistant. Create a full Home Assistant backup first.

</details>

<details>
<summary>Project status</summary>

[![Tests](https://img.shields.io/github/actions/workflow/status/barneyonline/ha-enphase-energy/tests.yml?branch=main&label=tests)](https://github.com/barneyonline/ha-enphase-energy/actions/workflows/tests.yml)
[![codecov](https://codecov.io/gh/barneyonline/ha-enphase-energy/graph/badge.svg?token=ichJ6LKzFK)](https://codecov.io/gh/barneyonline/ha-enphase-energy)
[![Hassfest](https://img.shields.io/github/actions/workflow/status/barneyonline/ha-enphase-energy/hassfest.yml?branch=main&label=hassfest)](https://github.com/barneyonline/ha-enphase-energy/actions/workflows/hassfest.yml)
[![Quality Scale](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fraw.githubusercontent.com%2Fbarneyonline%2Fha-enphase-energy%2Fmain%2Fcustom_components%2Fenphase_ev%2Fmanifest.json&query=%24.quality_scale&label=quality%20scale&cacheSeconds=3600)](https://developers.home-assistant.io/docs/integration_quality_scale_index)
[![Stars](https://img.shields.io/github/stars/barneyonline/ha-enphase-energy)](https://github.com/barneyonline/ha-enphase-energy/stargazers)
[![License](https://img.shields.io/github/license/barneyonline/ha-enphase-energy)](LICENSE)
[![Open Issues](https://img.shields.io/github/issues/barneyonline/ha-enphase-energy)](https://github.com/barneyonline/ha-enphase-energy/issues)
![Development Status](https://img.shields.io/badge/development-active-success?style=flat-square)

</details>
