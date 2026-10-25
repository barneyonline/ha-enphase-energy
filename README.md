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

## Quick install (HACS)

1. HACS -> Integrations -> Enphase Energy
2. Install and restart Home Assistant
3. Add the integration and sign in

Manual install steps: see the wiki Installation page.

## Compatibility

- Minimum supported Home Assistant version is `2026.10.0` (Python `3.14`+).
- Users migrating from the core Enphase Envoy integration can preserve compatible Energy-dashboard history with the [Envoy History Migration](https://github.com/barneyonline/ha-enphase-energy/wiki/Envoy-History-Migration) assistant. Create a full Home Assistant backup first.

## Authentication

Sign in with your Enlighten credentials; MFA is supported. See the wiki for details.

## Documentation

Refer to the [Wiki](https://github.com/barneyonline/ha-enphase-energy/wiki) for setup,
configuration, and troubleshooting guidance.
