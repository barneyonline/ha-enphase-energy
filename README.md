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

IQ Battery, microinverter, and EV charger device models use friendly names when available. The Device info card shows a shared SKU in brackets, matching IQ Gateway presentation; mixed models use a summary. Hardware is shown only when a hardware revision is reported, rather than repeating the SKU. Grouped devices retain full firmware versions with counts when versions differ, and omit a single serial number when members have different serials. Device and cloud service cards link to the site in Enlighten.

## Supported device categories

- IQ Gateway / System Controller entities and controls
- IQ Battery telemetry and BatteryConfig controls (where supported); Battery Overall Charge rounds down to a whole percentage (for example, 20.5% becomes 20%) to align with Enphase reporting
- IQ EV Charger controls and session telemetry
- IQ Microinverter connectivity, inventory, lifetime production, and optional installer-level parameter telemetry
- **Total Array Size** (kW DC nameplate) and **Total Inverter Capacity** (kVA continuous AC rating) under IQ Microinverters, each with an `arrays` attribute containing per-array values in the sensor's unit. Panel size uses configured per-array panel STC ratings, not rounded production estimates or site-wide panel metadata. Sensors are created only after complete data is available for each total. Denied Array Builder access (401/403) creates neither sensor; denied Settings access still permits Total Inverter Capacity. Optional metadata refreshes every six hours, including access-denied retries. Transient failures retry after one hour and make existing sensors unavailable without deleting their history. Access depends on the Enlighten account; general homeowner access to these metadata routes has not been verified.
- Site and cloud energy telemetry (including supported HEMS channels such as Heat Pump and Water Heater lifetime energy)

Legacy AC Battery devices are no longer supported. Upgrading removes their sensors,
sleep controls, and empty AC Battery device record. An AC-Battery-only selection
does not enable other device categories on upgrade. IQ Battery support is unchanged.

Enphase Cloud includes two diagnostic sensors:

- **Site information** uses the site ID as its state and exposes site ID, timezone, country, and currency as attributes. Country and currency come from the installation summary, not the account profile.
- **Account access** summarizes the highest observed role using this display priority: Administrator, Installer, Owner, Host, Consumption data, then Viewer. Its attributes expose each role as a boolean. This is descriptive account information, not a guarantee that every API operation is authorized. Viewer means no elevated role was reported; it is not a separate Enphase role flag. Missing or failed access metadata makes the sensor unavailable rather than reporting false permissions.

Metadata refreshes every six hours, with a 15-minute retry interval for missing or failed responses. A manual integration refresh bypasses these intervals. Raw account identifiers and personal details are discarded. The Service info card no longer displays the integration version; the integration page continues to show it.

## Key features

- Guided onboarding for site selection and device-category enablement
- Unified support for EV chargers, gateway, battery, and microinverter entities
- Multi-gateway topology awareness for primary/default Gateway and phase selection
- EV charging controls and session telemetry, including charge-mode aware behavior and persistent default charge-level controls when exposed by Enphase
- Advisory firmware update entities for gateway and EV charger devices with locale-aware release-note links; the gateway entity also monitors read-only live update progress, percentage, timing, and sanitized component status when Enphase exposes it
- Heat-pump runtime status, connectivity, SG-Ready mode, power, and current-day consumption details sourced from HEMS endpoints
- Site and battery energy telemetry, including Current Power Consumption calculated from the available power sensors, plus derived grid-import, grid-export, and battery power sensors for Home Assistant Energy Dashboard use
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

## Documentation

Refer to the [Wiki](https://github.com/barneyonline/ha-enphase-energy/wiki) for setup,
configuration, and troubleshooting guidance.

### Current Power Consumption calculation

Current Power Consumption uses **production + grid power + battery power**,
where grid import and battery discharge are positive, and export and charging
are negative. It becomes available as soon as the input sensors have numeric
readings, including after startup, and updates whenever any input changes.
For example, 1,992 W production with 0 W grid and battery power gives 1,992 W
consumption. Negative totals are clamped to zero.

The inputs can represent different cloud reporting times; this is an estimate
from their latest published values, not a synchronized meter measurement. Source
entities and their reported timestamps are included in the attributes. Missing or
unavailable inputs are not treated as zero; battery power is omitted only when
the site is known to have no battery. Until the inputs are ready, the sensor can
use its existing consumption-energy calculation. Once a power balance has been
calculated, it retains that result if an input becomes unavailable and marks it
`using_cached`, until all required power inputs are available again.

### Gateway connection method

Gateway Connection Method also uses the site-today response, matched by gateway
serial number, during the inventory refresh. Explicit per-gateway transport flags
take precedence over dashboard details. The site-level connection type is not
assigned to individual gateways, and `primary_gw_connectivity` describes a parent
gateway connection rather than Ethernet, Wi-Fi, or cellular transport.

### Features and Grid Profile access

Use **Configure → Devices** to choose device categories and **Configure → Features**
to configure Device Features and Advanced Features. Advanced Features contains
**Enable installer Grid Profile controls**, **Enable Export Limit controls**, and
**Enable Microinverter Power**.

When Enphase rate-limits microinverter power telemetry, open **Microinverter
Connectivity Status** to see **Power telemetry status** and **Next power telemetry
retry**. The retry deadline survives integration reloads and Home Assistant
restarts; restarting does not make Enphase accept requests sooner. These
attributes are available even before individual microinverter power sensors
have been discovered.

With Grid Profile controls enabled, **Configure → Advanced → Grid Profile Control**
remains visible even when discovery fails. Expired Enlighten sessions use the
shared automatic refresh when remembered credentials are available. If installer
session recovery fails, Home Assistant prompts for reauthentication; you can also
start it under **Configure → Authentication → Start reauthentication**. Permission
denials do not trigger login prompts. Enabling this feature does not apply a grid
profile.

### Export Limit (installer access)

Under **Configure → Features → Advanced Features**, enable **Enable Export Limit
controls** to configure system export limits. This feature is off by default;
enabling or disabling it never changes the gateway's existing configuration.
After enabling, open **Configure → Advanced → Export Limit** to set an absolute
watt limit, request zero export, or disable gateway export limiting. Changes in
this guided workflow require confirmation; automation actions have no confirmation field.

The first version supports an unambiguous single-gateway configuration using
absolute export watts, including compatible disabled configurations that retain
production or percentage defaults. Replaced gateway records are matched to the
single current gateway by its dashboard device ID; ambiguous identity blocks
control. Enabled percentage or production limits and digital-input relay
configurations remain read-only. Missing
settings or an invalid existing slew rate block writes. Existing slew rate is
preserved unless explicitly overridden. Installer access and access to the live PEL
form are both required. Writes also require a complete, unambiguous form whose
current mode, watts, and slew rate match gateway readback; reload the settings
if another client has changed them.

When Enphase's form contains a zero slew-rate default,
the guided confirmation page offers **Restore slew rate from gateway**. This
explicit choice preserves the positive gateway rate shown on that page, provided
fresh gateway identity and every other setting still match. It is unavailable
when enabling or changing a limit with a different saved slew-rate override;
restore the default first if needed. Disabling a limit always preserves the current
gateway rate. This reconciliation supports enabling, changing, and disabling a limit.
Nonzero disagreements remain blocked. This exception is not used by the selector
or automation actions, and accepted submissions still require matching gateway
readback before they are confirmed.

The IQ Gateway **Export Limit** selector provides **Enable Limit** and **Disable
Limit**. Enable applies the saved default limit (initially **0 W**, zero export).
Under **Advanced → Export Limit → Default settings**, save the default watts and
**Slew rate (W/sec)**. Slew rate initially comes from the gateway. Saving defaults
does not change gateway configuration. **Restore slew rate from gateway** clears
the saved override and uses a fresh gateway reading, not a factory default.
Disable Limit preserves the current gateway slew rate. The control’s attributes
show the default limit, effective default slew rate, and whether the rate comes
from the gateway or a saved override.

Automations and scripts can use:

```yaml
action: enphase_ev.set_export_limit
data:
  site_id: "YOUR_SITE_ID"
  limit_watts: 5000
```

The range is 0–100,000 whole watts; this API input range does not establish your
site's permitted export capacity. Set `limit_watts: 0` for zero export, which
still allows solar to supply local loads and charging. Use
`enphase_ev.disable_export_limit` with the same site target
to disable gateway limiting. Other utility or grid-profile constraints may remain.
Use `enphase_ev.refresh_export_limit` with the site target to refresh status.
Actions are also available in Home Assistant's action picker and support existing
integration entity/device/config-entry target routing.
Both write actions accept optional `slew_rate` in W/sec, a positive finite number
with up to two decimal places. Omit it to preserve the current gateway reading
independently of the selector’s saved default. Refresh has no slew-rate input.

The **Export Limit** sensor displays **Pending** while a submitted change awaits
matching gateway readback, or **Unconfirmed** when the result remains uncertain.
Like System Profile, its attributes include `pending`, `pending_requested_at`,
and requested settings alongside confirmed watts and slew rate. Once confirmed,
the sensor returns to Disabled, Zero export, Limited, or Unsupported. Readback is
checked immediately and at the configured fast polling interval for 10 minutes.
If still unresolved, a repair warning is raised and checks continue at the
configured standard (slow) polling interval. System Profile uses the same
10-minute readback window and polling policy.
Unconfirmed requests block further writes until matching readback is observed.
Reloads resume verification without resending a command. No write is automatically
replayed after an uncertain response, including connection failures that the HTTP
client would normally retry. Disabling this feature stops polling and
blocks its actions without disabling the gateway's limit.

Cloud configuration confirmation is not proof of physical export enforcement.
The integration-session write contract still requires validation on an authorized
supported site; browser-observed success alone does not establish compatibility.
