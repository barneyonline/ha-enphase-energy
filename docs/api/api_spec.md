# Enphase Energy Cloud API Specification

_This reference consolidates observed Enlighten mobile/web APIs across EV charging, site energy, gateway, battery, and microinverter features._

---

## 1. Overview
- **Base URL:** `https://enlighten.enphaseenergy.com`
- **Auth:** The current implementation is cookie-first. Login establishes an Enlighten session cookie jar, then the client best-effort fetches an access token from `/users/self/token` and adds endpoint-specific headers on top. Many read endpoints work with cookies plus `e-auth-token`; scheduler, HEMS, and timeseries families prefer or require `Authorization: Bearer <jwt>`. BatteryConfig is now the main exception: reads prefer the homeowner web app's session-cookie request shape, while compatibility variants remain for sites that still accept token-backed shapes.
- **Privacy:** Example identifiers, account details, LAN metadata, and credentials in this document use placeholders. Raw browser-export request headers often contain JWTs, cookies, email addresses, user IDs, LAN IPs, MAC addresses, and serial numbers; those values must be redacted before captures are shared or committed. When this spec lists "observed values", it intentionally preserves non-sensitive enum/flag values so newly seen behavior is not lost.
- **Evidence date:** Implementation statements reflect `origin/main` at `0c71ed03`, synced locally on 2026-05-12. New browser/mobile captures should record the capture date, client surface, and endpoint family near the affected section.
- **Payload examples:** Examples should be minimal shape excerpts. Prefer fields that drive parsing, auth, controls, or diagnostics, and omit long arrays or app-shell metadata unless those details affect implementation.
- **Path Variables:**
  - `<site_id>` - numeric site identifier
  - `<sn>` - charger serial number
  - `<gateway_sn>` - Envoy/IQ Gateway serial number used by site live-status streams
  - `connectorId` - connector index; currently always `1`
- **Discovery:** `GET /app-api/search_sites.json?searchText=&favourite=false` enumerates the account's accessible sites, returning IDs and display titles.
- **Evidence labels used below:**
  - `Implementation:` describes behavior verified in the current integration code.
  - `Observed:` describes behavior seen in browser/mobile captures.
  - `External client source:` describes behavior inferred from another public custom integration source; useful for comparison, but weaker than a first-party browser/mobile capture.
  - `Inference:` describes reasoned interpretation that is plausible but not yet directly confirmed.

---

### 1.1 Site Discovery (Search API)
```
GET /app-api/search_sites.json?searchText=&favourite=false
```
Returns the sites tied to the authenticated account. `id` is the numeric site identifier and `title` is the display name when present.
`searchText` filters results by name/id, while `favourite=false` returns all sites instead of just starred entries.

Implementation notes:
- Site lists may be wrapped in `sites`, `data`, `items`, or `systems`.
- Site ids are accepted from `site_id`, `siteId`, `id`, or `system_id`; names are accepted from `name`, `site_name`, `siteName`, `title`, `displayName`, or `display_name`.
- If discovery yields no options, the config flow allows manual numeric site-id entry.

Example response:
```json
{
  "sites": [
    {
      "id": 1234567,
      "path": "/web/1234567?v=3.4.0",
      "title": "Main Site",
      "favourite": false
    }
  ]
}
```

---

### 1.2 Endpoint Families (Quick Layout)

- **Auth and discovery:** `1.1`, `6.1`-`6.6`
- **Site/system inventory and telemetry:** `2.9`-`2.21`
- **Enlighten Manager and installer diagnostics:** `2.24`-`2.29`
- **EV charger telemetry and metadata:** `2.1`-`2.8`
- **EV charger controls and scheduling:** `3.1`-`3.3`, `4.1`-`4.5`
- **BatteryConfig controls:** `5.1`-`5.11`
- **Activation grid profile controls:** `5A.1`-`5A.5`
- **Cross-cutting references:** `7`, `8`, `9`

### 1.3 Table of Contents

- `1. Overview`
  - `1.4 Endpoint Matrix`
  - `1.5 Current Runtime Surface`
  - `1.6 Parser Normalization Summary`
- `2. Core Site and Device Endpoints`
- `2.F HEMS (IQ Energy Router / Heat Pump Monitoring)`
- `2.G Mobile/Web Shared Constants`
- `2.H Remaining Enlighten Manager and Installer Read Endpoints`
- `3. EV Charger Control Operations`
- `4. EV Scheduler (Charge Mode) API`
- `5. BatteryConfig APIs (System Profile and Battery Controls)`
- `5A. Activation Grid Profile APIs (Installer-Level)`
- `6. Authentication Flow (Shared Across Services)`
- `7. Response Field Reference`
- `8. Error Handling and Rate Limiting`
- `9. Known Variations and Open Questions`
- `10. References`

### 1.4 Endpoint Matrix (High-Level)

Status labels:
- `Runtime`: used by setup, refresh, entities, or services.
- `Diagnostics`: collected only for diagnostics/system-health surfaces.
- `Client helper only`: a client helper exists, but no current entity/service path consumes it.
- `Documented only`: retained from public docs or another custom client source; not implemented and not verified in the current runtime.
- `Browser capture only`: preserved from browser/mobile observation for future compatibility work.
- `Not implemented`: intentionally not called by the current integration.

| Domain | Method | Endpoint | Auth | Status |
| --- | --- | --- | --- | --- |
| Site discovery | `GET` | `/app-api/search_sites.json` | authenticated session cookies; implementation also sends `X-CSRF-Token` and, when available, `Authorization: Bearer <token>` + `e-auth-token: <token>` | Runtime |
| Enlighten session token bootstrap | `GET` | `/users/self/token` | authenticated Enlighten session cookies; optional `client_id` is not required by the current runtime | Runtime |
| Entrez token bootstrap (retired) | `POST` | `https://entrez.enphaseenergy.com/tokens` | formerly authenticated session cookies + JSON body `{session_id,email}` | Documented only |
| JWT token bootstrap (legacy / documented capture) | `GET` | `/app-api/jwt_token.json` | authenticated Enlighten session cookies | Documented only |
| JWT token fallback (legacy / documented capture) | `GET` | `/service/auth_ms_enho/api/v1/session/token` | session cookies + `_enlighten_4_session` echoed as `e-auth-token` | Browser capture only |
| Mobile/web shared constants | `GET` | `https://enlighten-mobile-38d22.firebaseio.com/enho_constants.json` | none observed | Browser capture only |
| EV runtime status | `GET` | `/service/evse_controller/<site_id>/ev_chargers/status` and `/service/evse_controller/api/v2/<site_id>/ev_chargers/status` | `e-auth-token` + cookies | Runtime |
| EV metadata summary | `GET` | `/service/evse_controller/api/v2/<site_id>/ev_chargers/summary` | `e-auth-token` + cookies | Runtime |
| EV last-reported timestamps | `GET` | `/service/evse_controller/api/v2/<site_id>/ev_chargers/last_reported_at` | `e-auth-token` + cookies | Browser capture only |
| EV firmware details | `GET` | `/service/evse_management/fwDetails/<site_id>` | `e-auth-token` + cookies | Runtime |
| EV feature flags | `GET` | `/service/evse_management/api/v1/config/feature-flags?site_id=<site_id>[&country=<country>]` | `e-auth-token` + cookies | Runtime |
| EV daily timeseries | `GET` | `/service/timeseries/evse/timeseries/daily_energy?site_id=<site_id>&source=evse&requestId=<uuid>&start_date=<YYYY-MM-DD>[&username=<user_id>]` | bearer token + session headers | Runtime |
| EV lifetime timeseries | `GET` | `/service/timeseries/evse/timeseries/lifetime_energy?site_id=<site_id>&source=evse&requestId=<uuid>[&username=<user_id>]` | bearer token + session headers | Runtime |
| Site inventory | `GET` | `/app-api/<site_id>/devices.json` | `e-auth-token` + cookies | Runtime |
| Multi-gateway phase map | `GET` | `/app-api/<site_id>/phase_map_multiple_envoy` | authenticated session cookies + `e-auth-token` | Runtime |
| Site bootstrap payload | `GET` | `/app-api/<site_id>/data.json?app=<id>&device_status=non_retired&is_mobile=<id>` | authenticated session cookies + `e-auth-token` | Browser capture only |
| Filtered site-device inventory | `POST` | `/service/site-device/api/v2/devices/list` | `e-auth-token` + cookies | Browser capture only |
| Site live-stream flags | `GET` | `/app-api/<site_id>/show_livestream` | authenticated session cookies | Runtime |
| Site live-status MQTT authorizer | `GET` | `/pv/aws_sigv4/livestream.json?serial_num=<gateway_sn>` | authenticated Enlighten session cookies + `e-auth-token`; browser capture used `Accept: */*`, `Referer: /web/<site_id>/today/graph/hours?v=3.4.0`, `X-Requested-With: XMLHttpRequest` | Runtime |
| Site live-debug MQTT authorizer | `GET` | `/service/system_dashboard/api_internal/cs/sites/livestream?serial_num=<gateway_sn>&live_debug=true` | authenticated Enlighten session cookies + `e-auth-token`; browser XHR request | Runtime (gateway update progress) |
| Site latest power | `GET` | `/app-api/<site_id>/get_latest_power` | `e-auth-token` + cookies | Runtime |
| Site weather | `GET` | `/systems/<site_id>/weather.json?locale=<locale>` | authenticated session cookies + XHR headers; `e-auth-token` when available | Runtime (opt-in) |
| Site currency conversion settings | `GET` | `/app-api/<site_id>/get_currency_conversion.json` | authenticated session cookies + `e-auth-token` | Browser capture only |
| Requested battery usage hint | `GET` | `/app-api/<site_id>/get_requested_battery_usage` | authenticated session cookies + `e-auth-token` | Browser capture only |
| Site performance widget flags | `GET` | `/app-api/<site_id>/performance_widgets` | authenticated session cookies + `e-auth-token` | Browser capture only |
| PowerMatch UI flags | `GET` | `/app-api/<site_id>/powermatch_details` | authenticated session cookies + `e-auth-token` | Browser capture only |
| Site today snapshot | `GET` | `/pv/systems/<site_id>/today` | authenticated Enlighten session cookies | Runtime |
| Legacy site tariff flags | `GET` | `/app-api/<site_id>/tariff.json?country=<country>` | authenticated session cookies + `e-auth-token` | Browser capture only |
| Site billing-cycle details | `GET/POST` | `/service/tariff/tariff-ms/systems/<site_id>/billing-details[?date=<YYYY-MM-DD>]` | tariff web headers: authenticated cookies, `e-auth-token`, `Username`, `Requestid`; writes add `Origin`, `Content-Type`, and `x-xsrf-token` | Runtime |
| Site tariff configuration | `GET/PUT` | `/service/tariff/tariff-ms/systems/<site_id>/tariff?include-site-details=true` and `/service/tariff/tariff-ms/systems/<site_id>/tariff?user-id=<user_id>` | tariff web headers: authenticated cookies, `e-auth-token`, `Username`, `Requestid`, `X-Requested-With`; writes add `Origin`, `Content-Type`, and `x-xsrf-token` | Runtime |
| Dated site tariff rates | `GET` | `/service/tariff/tariff-ms/systems/<site_id>/tariffs?rateType=<BUYBACK>&date=<YYYY-MM-DD>&includeUtility=` | tariff web headers: authenticated cookies, `e-auth-token`, `Username`, `Requestid`, `X-Requested-With` | Runtime |
| Tariff settings MQTT authorizer | `GET` | `/pv/aws_sigv4/tariff_settings_response_stream?serial_number=<gateway_sn>` | authenticated Enlighten session cookies + `e-auth-token` + `x-xsrf-token` | Browser capture only |
| EVSE tariff-change notification | `PUT` | `/service/evse_scheduler/api/v1/siteConfig/<site_id>/tariff_change` | tariff web write headers + JSON `null` body | Runtime (best-effort) |
| System dashboard summary | `GET` | `/service/system_dashboard/api_internal/cs/sites/<site_id>/summary` | session cookies + optional `Authorization: Bearer <token>` (current implementation adds bearer when available) | Runtime |
| System dashboard events | `GET` | `/service/system_dashboard/api_internal/cs/sites/<site_id>/events?range=today&cassandra_toggle=false&filter_columns=<columns>&serial_numbers=&type=table&event_state=default&page=1&per_page=200` | dashboard-read headers: authenticated cookies, optional bearer | Runtime |
| System dashboard master data | `GET` | `/service/system_dashboard/api_internal/cs/sites/<site_id>/data/master-data` | dashboard-read headers: authenticated cookies, optional bearer, XSRF when present | Runtime |
| Per-gateway microinverter inventory | `GET` | `/service/system_dashboard/api_internal/dashboard/sites/<site_id>/envoy_inverters?serial_number=<gateway_sn>` | dashboard-read headers: authenticated cookies, optional bearer, XSRF when present | Runtime |
| Device-level parameter columns | `GET` | `/service/system_dashboard/api_internal/cs/sites/<site_id>/data/columns?serial_num=<gateway_sn>&type=device_level` | dashboard-read headers: authenticated cookies, optional bearer, XSRF when present | Runtime |
| Bulk device parameter readings | `GET` | `/service/system_dashboard/api_internal/cs/sites/<site_id>/data/parameter-view?serial_numbers=<csv>&parameter_id=<id>&...` | dashboard-read headers: authenticated cookies, optional bearer, XSRF when present | Runtime |
| Activation checklist | `GET` | `/service/system_dashboard/api_internal/cs/sites/<site_id>/updated_activation_checklist` | dashboard-read headers: authenticated cookies, optional bearer | Browser capture only |
| System dashboard devices table | `GET` | `/service/system_dashboard/api_internal/cs/sites/<site_id>/devices?range=<range>&filter_columns=<...>&serial_numbers=<...>&type=table&page=<page>&per_page=<n>` | dashboard-read headers: authenticated cookies, optional bearer | Browser capture only |
| System dashboard status | `GET` | `/service/system_dashboard/api_internal/dashboard/sites/<site_id>/status` | dashboard-read headers: authenticated cookies, optional bearer | Browser capture only |
| System dashboard range testing | `GET` | `/service/system_dashboard/api_internal/dashboard/sites/<site_id>/range_testing` | dashboard-read headers: authenticated cookies, optional bearer | Browser capture only |
| System dashboard device tree | `GET` | `/service/system_dashboard/api_internal/dashboard/sites/<site_id>/devices-tree` | dashboard-read headers: authenticated cookies, optional bearer | Runtime |
| Standing alarms | `GET` | `/service/system_dashboard/api_internal/dashboard/sites/<site_id>/alarms` | dashboard-read headers: authenticated cookies, optional bearer | Browser capture only |
| System dashboard device details | `GET` | `/service/system_dashboard/api_internal/dashboard/sites/<site_id>/devices_details?type=<type>` | dashboard-read headers: authenticated cookies, optional bearer | Runtime |
| System configuration | `GET` | `/service/enlm_ms/api_internal/<site_id>/system_config` | dashboard-read headers: authenticated cookies, optional bearer, XSRF when present | Browser capture only |
| Dashboard A/B feature catalog | `GET` | `/service/system_dashboard/api_internal/dashboard/ab_features?feature[email]=<email>&feature[company_id]=<company_id>&feature[site_id]=<site_id>` | authenticated session cookies; response and request contain sensitive account identifiers | Browser capture only |
| Dashboard account profile | `GET` | `/service/system_dashboard/api_internal/ms/account` | authenticated session cookies; response contains a credential-bearing token | Browser capture only |
| Enlighten shell account metadata | `GET` | `/service/enlm_ms/api_internal/account/data` | authenticated session cookies | Browser capture only |
| Activation-log query | `POST` | `/service/system_dashboard/api_internal/dashboard/sites/<site_id>/activation_logs?range=<range>&type=table&page=<page>&per_page=<n>` | dashboard-read headers + JSON filter body; read-only despite `POST` | Browser capture only |
| ITK raw-log query | `POST` | `/service/system_dashboard/api_internal/dashboard/sites/<site_id>/itk_raw_logs` | dashboard-read headers + JSON filter body; read-only despite `POST` | Browser capture only |
| Enlighten-log inventory | `GET` | `/service/system_dashboard/api_internal/dashboard/sites/<site_id>/enho_logs?range=<range>&type=table&page=<page>&per_page=<n>` | dashboard-read headers | Browser capture only |
| Diagnostic task definitions | `GET` | `/service/system_dashboard/api_internal/cs/sites/<site_id>/task_types` | dashboard-read headers | Browser capture only |
| Diagnostic task history | `GET` | `/service/system_dashboard/api_internal/cs/sites/<site_id>/tasks?page=<page>&per_page=<n>` | dashboard-read headers | Browser capture only |
| PLC FFT MQTT bootstrap | `GET` | `/service/system_dashboard/api_internal/dashboard/fft_scan?serial_number=<gateway_sn>&mode=<mode>` | dashboard-read headers; response contains short-lived AWS IoT credentials | Browser capture only |
| Generator/live MQTT bootstrap | `GET` | `/admin/aws_sigv4/livestream?serial_num=<gateway_sn>` | authenticated session cookies; response contains short-lived AWS IoT credentials | Browser capture only |
| Site lifetime energy | `GET` | `/pv/systems/<site_id>/lifetime_energy` | `e-auth-token` + cookies | Runtime |
| Homeowner events | `GET` | `/service/events-platform-service/v1.0/<site_id>/events/homeowner` | `e-auth-token` + cookies | Runtime |
| Battery backup history | `GET` | `/app-api/<site_id>/battery_backup_history.json` | `e-auth-token` + cookies | Runtime |
| Grid eligibility | `GET` | `/app-api/<site_id>/grid_control_check.json` | `e-auth-token` + cookies | Runtime |
| Dry contact settings | `GET` | `/pv/settings/<site_id>/dry_contacts` | `e-auth-token` + cookies | Runtime |
| Microinverter inventory | `GET` | `/app-api/<site_id>/inverters.json` | `e-auth-token` + cookies | Runtime |
| Microinverter array layout | `GET` | `/systems/<site_id>/site_array_layout_x` | authenticated Enlighten session cookies | Browser capture only |
| Microinverter jellyfish bootstrap | `GET` | `/systems/<site_id>/jellyfish_initializer?range=<range>&view=<view>` | authenticated Enlighten session cookies | Browser capture only |
| EV supported-country catalog | `GET` | `/service/evse_management/api/v1/config/supported_countries` | authenticated session cookies | Browser capture only |
| Manager site weather | `GET` | `/systems/<site_id>/weather.json?locale=<locale>` | authenticated session cookies + browser XHR headers | Browser capture only |
| Manager site statistics fragment | `GET` | `/systems/<site_id>/show_stat_data` | authenticated session cookies + browser XHR headers | Browser capture only |
| Manager date-range production | `GET` | `/systems/<site_id>/energy?start_date=<YYYY-MM-DD>&end_date=<YYYY-MM-DD>` | authenticated session cookies + browser XHR headers | Browser capture only |
| Manager lifetime production | `GET` | `/systems/<site_id>/energy_lifetime?all_production_sources=true` | authenticated session cookies + browser XHR headers | Browser capture only |
| Manager lifetime estimate | `GET` | `/systems/<site_id>/energy_lifetime_est` | authenticated session cookies + browser XHR headers | Browser capture only |
| Manager lifetime consumption | `GET` | `/systems/<site_id>/consumption_lifetime` | authenticated session cookies + browser XHR headers | Browser capture only |
| Manager lifetime EV consumption | `GET` | `/systems/<site_id>/evse_consumption_lifetime` | authenticated session cookies + browser XHR headers | Browser capture only |
| Manager EV power timeseries | `GET` | `/systems/<site_id>/evse_power_timeseries?days=<days>&date=<YYYY-MM-DD>` | authenticated session cookies + browser XHR headers | Browser capture only |
| Manager Microinverter DataTables feed | `GET` | `/systems/<site_id>/pcu_datatable.js?<datatable_query>` | authenticated session cookies + browser XHR headers | Browser capture only |
| Manager event feeds | `GET` | `/systems/<site_id>/events.json?<datatable_query>` and device-scoped variants | authenticated session cookies + browser XHR headers | Browser capture only |
| Manager Microinverter statistics | `GET` | `/systems/<site_id>/inverters/<inverter_id>/show_stat_data_x` | authenticated session cookies + browser XHR headers | Browser capture only |
| Manager Microinverter multistat timeseries | `GET` | `/systems/<site_id>/inverters/<inverter_id>/time_series_x?date=<YYYY-MM-DD>&stat=<csv>` | authenticated session cookies + browser XHR headers | Browser capture only |
| Array Builder block status | `GET` | `/service/array-builder/api/<site_id>/v1-block-status` | authenticated session cookies | Browser capture only |
| Manager Settings fragments | `GET` | `/enlm_internal/site_photos?site_id=<site_id>`, `/systems/<site_id>/third_party_reporting_details`, `/systems/<site_id>/async_tariff_details` | authenticated session cookies; tariff fragment may embed short-lived session material | Browser capture only |
| Battery status | `GET` | `/pv/settings/<site_id>/battery_status.json` | `e-auth-token` + cookies | Runtime |
| AC Battery devices page | `GET` | `/systems/<site_id>/devices?status=active` | `e-auth-token` + cookies; browser-style HTML headers | Runtime |
| AC Battery detail page | `GET` | `/systems/<site_id>/ac_batteries/<battery_id>` | `e-auth-token` + cookies; browser-style HTML headers | Runtime |
| AC Battery telemetry fragment | `GET` | `/systems/<site_id>/ac_batteries/<battery_id>/show_stat_data` | `e-auth-token` + cookies; XHR/browser-style HTML headers | Runtime |
| AC Battery sleep | `GET` | `/systems/<site_id>/ac_batteries/<battery_id>/sleep?sleep_min_soc=<value>` | `e-auth-token` + cookies; browser-style HTML headers | Runtime |
| AC Battery wake | `GET` | `/systems/<site_id>/ac_batteries/<battery_id>/wake` | `e-auth-token` + cookies; browser-style HTML headers | Runtime |
| AC Battery events page | `GET` | `/systems/<site_id>/ac_batteries/<battery_id>/events` | `e-auth-token` + cookies; browser-style HTML headers | Diagnostics |
| HEMS device inventory | `GET` | `https://hems-integration.enphaseenergy.com/api/v1/hems/<site_id>/hems-devices[?include-retired=true|refreshData=false]` | HEMS read headers: bearer-preferred auth, cookies/base headers, `requestId`, `username` when available | Runtime |
| HEMS heat-pump runtime state | `GET` | `https://hems-integration.enphaseenergy.com/api/v1/hems/<site_id>/heatpump/<device_uid>/state?timezone=<iana_tz>` | HEMS read headers: bearer-preferred auth, cookies/base headers, `requestId`, `username` when available | Runtime |
| HEMS daily device energy consumption | `GET` | `https://hems-integration.enphaseenergy.com/api/v1/hems/<site_id>/energy-consumption?from=<iso8601>&to=<iso8601>&timezone=<iana_tz>&step=<period>` | HEMS read headers: bearer-preferred auth, cookies/base headers, `requestId`, `username` when available | Runtime |
| HEMS supported device models | `GET` | `https://hems-integration.enphaseenergy.com/api/v1/hems/list-supported-models?deviceType=<device_type>` | HEMS read headers: bearer-preferred auth, cookies/base headers, `e-auth-token`, `requestId`, `username` when available | Browser capture only |
| HEMS power timeseries | `GET` | `/systems/<site_id>/hems_power_timeseries[?device-uid=<device_uid>]` | `e-auth-token` + cookies | Observed only; not used |
| HEMS lifetime consumption | `GET` | `/systems/<site_id>/hems_consumption_lifetime` | `e-auth-token` + cookies | Runtime |
| HEMS live stream toggle | `PUT` | `https://hems-integration.enphaseenergy.com/api/v1/hems/<site_id>/live-stream/status` | Enlighten session cookies | Browser capture only |
| HEMS live vitals toggle | `PUT` | `https://hems-integration.enphaseenergy.com/api/v1/hems/<site_id>/live-stream/vitals` | Enlighten session cookies | Browser capture only |
| Start charging | `POST` | `/service/evse_controller/<site_id>/ev_chargers/<sn>/start_charging` | `e-auth-token` + cookies | Runtime |
| Stop charging | `PUT` | `/service/evse_controller/<site_id>/ev_chargers/<sn>/stop_charging` | `e-auth-token` + cookies | Runtime |
| EV charger config read/write | `POST/PUT` | `/service/evse_controller/api/v1/<site_id>/ev_chargers/<sn>/ev_charger_config` | `Authorization: Bearer <token>` overlay on top of session cookies / base EV headers | Runtime |
| Charge mode preference | `GET/PUT` | `/service/evse_scheduler/api/v1/iqevc/charging-mode/<site_id>/<sn>/preference` | bearer token + session headers | Runtime |
| BatteryConfig site settings | `GET` | `/service/batteryConfig/api/v1/siteSettings/<site_id>?userId=<user_id>` | current web shape: fresh session `Cookie`, `Username`, `requestid`, battery-profile `Origin`/`Referer`, and no `Authorization` or `e-auth-token`; token-backed primary and lean fallbacks remain for compatibility | Runtime |
| BatteryConfig MQTT authorizer bootstrap | `GET` | `/service/batteryConfig/api/v1/mqttSignedUrl/<site_id>` | official-web BatteryConfig shape: `Accept`, `Origin`, `Referer`, Chrome-style `User-Agent`, `Username`; suppress `Authorization`, `Cookie`, `X-CSRF-Token`, `X-Requested-With`; current client uses primary variant with `e-auth-token` + `requestid` and lean fallback without them | Browser capture only |
| BatteryConfig third-party settings | `GET` | `/service/batteryConfig/api/v1/<site_id>/thirdPartyControlSettings` | official-web BatteryConfig shape: `Accept`, `Origin`, `Referer`, Chrome-style `User-Agent`, `Username`; suppress `Authorization`, `Cookie`, `X-CSRF-Token`, `X-Requested-With`; current client uses primary variant with `e-auth-token` + `requestid` and lean fallback without them | Browser capture only |
| BatteryConfig schedules | `GET` | `/service/batteryConfig/api/v1/battery/sites/<site_id>/schedules` | current web shape: fresh session `Cookie`, `Username`, `requestid`, battery-profile `Origin`/`Referer`, and no `Authorization` or `e-auth-token`; token-backed primary and lean fallbacks remain for compatibility | Runtime |
| BatteryConfig schedule create | `POST` | `/service/batteryConfig/api/v1/battery/sites/<site_id>/schedules` | BatteryConfig write shape plus `X-XSRF-Token`; live verification on the affected site showed create should send explicit `isEnabled`; on affected sites the verified working shape is the raw-cookie browser request (`Cookie`, `e-auth-token`, `Username`, `X-XSRF-Token`, `X-Requested-With`) sent from a stateless client session so aiohttp does not merge cookie-jar state; current client falls back across cookie-backed, primary, lean, and mixed-auth variants | Runtime |
| BatteryConfig schedule validation / XSRF bootstrap | `POST` | `/service/batteryConfig/api/v1/battery/sites/<site_id>/schedules/isValid` | explicit validation first acquires `X-XSRF-Token` from `GET /siteSettings/<site_id>?userId=<user_id>`, then uses the official-web BatteryConfig write shape with that token and without `Authorization`, `Cookie`, `X-CSRF-Token`, or `X-Requested-With`; success shape is `{"isValid": true}`; the internal XSRF-bootstrap helper also prefers `GET /siteSettings/<site_id>?userId=<user_id>` and falls back to this route where it may include the currently held `X-XSRF-Token` while harvesting a fresh token from the response header, `Set-Cookie`, response cookies, or the session cookie jar; when this route returns `4xx`, the client still checks error-response cookies/session cookies, then tries a final cookie-based `GET /siteSettings/<site_id>` bootstrap before keeping the existing token; current client uses primary variant with `e-auth-token` + `requestid` and lean fallback without them | Runtime |
| BatteryConfig schedule update | `PUT` | `/service/batteryConfig/api/v1/battery/sites/<site_id>/schedules/<schedule_id>` | BatteryConfig write shape plus `X-XSRF-Token`; ordinary time/limit edits can omit `isEnabled`, while explicit schedule-entry toggle writes may include it; verified working update uses the raw-cookie browser request (`Cookie`, `e-auth-token`, `Username`, `X-XSRF-Token`, `X-Requested-With`) from a stateless client session; current client falls back across cookie-backed, primary, lean, and mixed-auth variants | Runtime |
| BatteryConfig schedule legacy delete alias | `POST` | `/service/batteryConfig/api/v1/battery/sites/<site_id>/schedules/<schedule_id>/delete` | same BatteryConfig write planner as schedule create/update; cookie-backed browser request is the verified working compatibility shape on affected sites | Runtime |
| BatteryConfig disclaimer accept | `POST` | `/service/batteryConfig/api/v1/batterySettings/acceptDisclaimer/<site_id>` | same BatteryConfig write planner as other battery settings mutations | Runtime |
| Activation reference data | `GET` | `/service/activation_service/api/details/reference_data` | Activation UI authenticated session cookies plus `enlm-token` / manager token context; browser capture did not include an explicit `Authorization` header | Runtime |
| Activation record | `GET` | `/service/activation_backend/api/gateway/v4/activations/<site_id>?expand=owner,host` | Activation UI authenticated session cookies plus Activation bearer auth; requires account role with Activation access | Runtime |
| Activation device list and grid-profile status | `GET` | `/service/activation_backend/api/gateway/v4/systems/<site_id>/devices/list` | Activation UI authenticated session cookies plus `Authorization: Bearer <activation_jwt>`; requires installer-level Activation access | Runtime |
| Grid profile discovery | `POST` | `/service/activation_backend/api/gateway/v4/systems/<site_id>/grid_profiles_filtered` | Activation UI authenticated session cookies plus `Authorization: Bearer <activation_jwt>`; requires installer-level Activation access | Runtime |
| Apply grid profile | `PUT` | `/service/activation_backend/api/gateway/v4/systems/<site_id>/envoys` | same Activation UI bearer/cookie shape as discovery; requires installer-level Activation access | Runtime |
| PES in-app banner/status | `GET` | `/service/pes_management/systems/<site_id>/inapp?type=<type>` | authenticated session cookies | Browser capture only |
| Login | `POST` | `/login/login.json` | credentials; session/XSRF cookies are established by the response rather than pre-required | Runtime |

---

### 1.5 Current Runtime Surface

The integration currently implements these feature groups:
- Auth and discovery: cookie login, MFA/resend handling, Enlighten session-token bootstrap, site discovery, runtime auth refresh, and manual site-id fallback.
- EV charging: status, summary, firmware details, feature flags, daily/lifetime EVSE energy, start/stop controls, charge-level configuration, and scheduler preference writes.
- Site energy and inventory: latest power, today snapshot, lifetime energy, battery backup/grid eligibility, device inventory, microinverter inventory, live-stream capability flags, tariff reads/writes, dry contacts, and system dashboard summary/tree/details.
- HEMS: inventory, heat-pump runtime state, HEMS daily energy split metadata, HEMS lifetime merge, and heat-pump power derivation from site-today heat-pump deltas.
- Battery and BatteryConfig: site settings, profile/details, battery settings writes, ITC disclaimer acceptance, DTG/RBD/CFG controls, dry-contact parsing, schedules, validation/XSRF bootstrap, and AC Battery HTML runtime/control paths.
- Firmware catalog and diagnostics: repository-managed runtime firmware catalog, endpoint health/backoff summaries, payload health, tariff health, heat-pump diagnostics, and redacted system-health output.

Captured-only sections remain in the spec when they explain feature flags, auth variants, fallback endpoints, or likely future implementation paths. They should stay concise and should not read like required runtime behavior.

### 1.6 Parser Normalization Summary

The implementation is intentionally tolerant of common Enphase wrapper and naming variants:
- List-like endpoints may return a bare list or a wrapper such as `data`, `items`, `result`, `sites`, or `systems`.
- Time values may be ISO strings, epoch seconds, or epoch milliseconds depending on endpoint family.
- EVSE runtime payloads merge status, summary, firmware, feature flags, and EVSE timeseries by charger serial.
- Site energy payloads preserve missing-vs-zero flow semantics and may derive grid/battery flows when direct arrays are absent.
- HEMS inventory accepts router/gateway/heat-pump buckets across direct and nested shapes, skips retired members, and treats HEMS split energy as metadata for daily heat-pump totals and derived heat-pump power.
- BatteryConfig writes use endpoint-specific compatibility planning rather than a single universal header/body shape.

---

## 2. Core Site and Device Endpoints

This section groups both EV charger endpoints and non-EV site/system endpoints exposed by Enlighten service APIs.

### 2.A EV Charger Telemetry and Metadata

### 2.1 Status Snapshot
```
GET /service/evse_controller/<site_id>/ev_chargers/status
GET /service/evse_controller/api/v2/<site_id>/ev_chargers/status
```
Returns charger state (plugged, charging, session energy, etc.).

Recent cloud responses wrap the data in `meta`/`data` objects:
```json
{
  "meta": { "serverTimeStamp": 1774675080415 },
  "data": {
    "site": "1234567",
    "tz": "Region/City",
    "chargers": [
      {
        "smartEV": { "hasToken": false, "hasEVDetails": false },
        "evManufacturerName": "Example OEM",
        "offGrid": "ON_GRID",
        "sn": "EV000000000000",
        "name": "IQ EV Charger_ABCD",
        "lst_rpt_at": "2026-03-28T04:54:34.360Z[UTC]",
        "offlineAt": "2026-03-22T05:39:50.897Z[UTC]",
        "connected": true,
        "auth_token": null,
        "mode": 1,
        "charging": false,
        "pluggedIn": false,
        "faulted": false,
        "commissioned": 1,
        "isEVDetailsSet": true,
        "sch_d": {
          "status": 1,
          "info": [
            {
              "type": "greencharging",
              "startTime": 1770591600,
              "endTime": 1771196400,
              "limit": 0
            }
          ]
        },
        "session_d": {
          "plg_in_at": "2026-01-29T12:06:21.074Z[UTC]",
          "strt_chrg": 1769688441763,
          "plg_out_at": "2026-01-29T14:40:37.428Z[UTC]",
          "e_c": 9867,
          "miles": 20.38028,
          "session_cost": 2.56,
          "auth_status": 4,
          "auth_type": null,
          "auth_id": null,
          "charge_level": null
        },
        "connectors": [
          {
            "connectorId": 1,
            "connectorStatusType": "AVAILABLE",
            "connectorStatusInfo": "",
            "connectorStatusReason": "",
            "safeLimitState": 1,
            "dlbActive": false,
            "pluggedIn": false
          }
        ]
      }
    ]
  },
  "error": {}
}
```
Legacy responses may still return the flatter `evChargerData` shape.
The `connectors[]` payload includes `dlbActive`, `safeLimitState`, and connector status fields.
The web-app Live Vitals capture also used the `api/v2` status alias, returning a keyed `data` object where each key was a charger serial and the value included `connected`, `pluggedIn`, `charging`, and `lst_rpt_at` in epoch milliseconds.

Observed field behavior:
- `session_d` may still describe the most recent completed charge session even when `charging=false` and `pluggedIn=false`.
- Session energy units vary by response shape; IQ-EVSE and modern session payloads report `session_d.e_c` as Wh even below `200`, while some legacy payloads may already report smaller values as kWh.
- `sch_d.status=1` with `sch_d.info[].type="greencharging"` indicates an active green-charging policy window; `startTime` and `endTime` are Unix seconds.
- `connectorStatusType="AVAILABLE"` can coexist with `connected=true`, meaning the charger is reachable but idle.
- `smartEV.hasEVDetails` and top-level `isEVDetailsSet` are separate flags and can disagree in the same payload.

Observed property values from the web capture:
- `offGrid="ON_GRID"`, `mode=1`, `commissioned=1`
- `connected=true`, `charging=false`, `pluggedIn=false`, `faulted=false`
- `smartEV.hasToken=false`, `smartEV.hasEVDetails=false`, `isEVDetailsSet=true`
- `sch_d.status=1`, `sch_d.info[].type="greencharging"`, `sch_d.info[].limit=0`
- `session_d.auth_status=4`, `session_d.auth_type=null`, `session_d.auth_id=null`, `session_d.charge_level=null`
- `connectors[].connectorId=1`, `connectors[].connectorStatusType="AVAILABLE"`, `connectors[].connectorStatusInfo=""`, `connectors[].connectorStatusReason=""`, `connectors[].safeLimitState=1`, `connectors[].dlbActive=false`
- The `api/v2` status alias was observed with `data.<charger_sn>.connected=true`, `pluggedIn=false`, `charging=false`, and `lst_rpt_at=<epoch_ms>`, matching the web-app charger card state "Not Plugged-in" and `0 kW`.
- During an active charging capture, the original status endpoint returned `connected=true`, `pluggedIn=true`, `charging=true`, `faulted=false`, `mode=0`, `offGrid="ON_GRID"`, and `connectors[].connectorStatusType="CHARGING"` with `safeLimitState=0`.
- The numeric status `mode` is the charger’s effective runtime mode; it is not a substitute for the stored scheduler preference returned by the charging-mode preference endpoint.
- The `api/v2` status alias during the same active charging capture still only exposed the keyed charger booleans/timestamp shape (`connected`, `pluggedIn`, `charging`, `lst_rpt_at`); it did not include the instantaneous charger voltage/current/power rendered by the web UI.

### 2.2 Extended Summary (Metadata)
```
GET /service/evse_controller/api/v2/<site_id>/ev_chargers/summary?filter_retired=true
GET /service/evse_controller/<site_id>/ev_chargers/summary
GET /service/evse_controller/api/v2/<site_id>/ev_chargers/<sn>/summary
```
Provides hardware/software versions, model names, operating voltage, IP addresses, and schedule information.
The list endpoint returns a `data` array; the per-charger endpoint returns a single `data` object and includes `supportsUseBattery`
to indicate whether the green-mode "Use Battery" toggle is supported. The observed web capture used the non-`api/v2`
site-summary alias and returned the same `meta`/`data`/`error` envelope shown below.

Example list response shape:
```json
{
  "meta": { "serverTimeStamp": 1774677008657, "rowCount": 1 },
  "data": [
    {
      "serialNumber": "EV000000000000",
      "displayName": "IQ EV Charger_ABCD",
      "isConnected": true,
      "hoControl": true,
      "supportsUseBattery": true,
      "chargeLevelDetails": { "min": "6", "max": "32", "granularity": "1", "defaultChargeLevel": "disabled" },
      "activeConnection": "ethernet",
      "dlbEnabled": 1,
      "maxCurrent": 32,
      "breakerRating": 32,
      "operatingVoltage": "230",
      "firmwareVersion": "25.37.1.14",
      "systemVersion": "25.37.1.14",
      "lifeTimeConsumption": 150657.53,
      "phaseMode": 3,
      "phaseCount": 3,
      "gridType": 4,
      "status": "NORMAL"
    }
  ],
  "error": {}
}
```

The per-charger endpoint returns the same charger object under `data` instead of `data[]`.

Observed field behavior:
- Some deployments omit `displayName`; the integration falls back to status payload names and serial-based labels when needed.
- `meta.rowCount` is present on the list endpoint and reflects the number of chargers returned after filters such as `filter_retired=true`.
- `networkConfig`, `wifiConfig`, `cellularConfig`, and `defaultRoute` are string-encoded diagnostics rather than nested JSON objects.
- `gatewayConnectivityDetails[].lastConnTime` and `functionalValDetails.lastUpdatedTimestamp` are epoch milliseconds, while most other timestamps are ISO-8601 strings with `[UTC]`.
- `lifeTimeConsumption` appears to be cumulative watt-hours.
- `phaseMode`, `phaseCount`, `gridType`, and `wiringConfiguration` together describe electrical topology.

Observed property values from the summary capture:
- `supportsUseBattery=true`, `hoControl=true`, `dlbEnabled=1`, `isConnected=true`, `isLocallyConnected=true`, `isRetired=false`
- `chargeLevelDetails.min="6"`, `chargeLevelDetails.max="32"`, `chargeLevelDetails.granularity="1"`, `chargeLevelDetails.defaultChargeLevel="disabled"`
- `activeConnection="ethernet"`, `reportingInterval=300`, `status="NORMAL"`, `skuScope="GEN2_EU"`
- `maxCurrent=32`, `breakerRating=32`, `ratedCurrent="32"`, `operatingVoltage="230"`
- `commissioningStatus=1`, `phaseMode=3`, `phaseCount=3`, `gridType=4`, `functionalValDetails.state=1`
- `gatewayConnectivityDetails[].gwConnStatus=0`, `gatewayConnectivityDetails[].gwConnFailureReason=0`, `rmaDetails=null`

### 2.2.1 Last Reported Timestamps
```
GET /service/evse_controller/api/v2/<site_id>/ev_chargers/last_reported_at
```
Returns a compact map of charger serial numbers to their latest report timestamp in epoch milliseconds.

Example response:
```json
{
  "meta": {
    "serverTimeStamp": 1770000001000
  },
  "data": {
    "EV0000000000": 1769999900000
  },
  "error": {}
}
```

### 2.2.2 Firmware Details
```
GET /service/evse_management/fwDetails/<site_id>
```
Returns site-scoped EV charger firmware rollout details as an array keyed by `serialNumber`.
Unlike summary v2, the path variable is the site identifier, not the charger serial number.

Observed request fields:
- Method: `GET`.
- Path parameter `site_id`: numeric Enlighten site identifier in the URL path.
- Query/body: none observed.
- `Accept: */*`.
- `Content-Type: application/json`.
- `X-Requested-With: XMLHttpRequest`.
- `Referer`: `/web/<site_id>/today/graph/hours?v=3.4.0?osv=1`.
- Browser capture authenticated with the normal Enlighten session cookie jar; the observed `e-auth-token` header value was the literal string `null`.

Example response:
```json
[
  {
    "serialNumber": "EVSE-SERIAL-0001",
    "siteId": 1234567,
    "upgradeStatus": 5,
    "currentFwVersion": "25.37.1.14",
    "targetFwVersion": "25.37.1.14",
    "lastSuccessfulUpgradeDate": "2025-12-08T22:41:46.568837098Z[UTC]",
    "lastUpdatedAt": "2025-12-08T15:52:59.806385175Z[UTC]",
    "statusDetail": null,
    "isAutoOta": false
  }
]
```

Observed structure:
- The response is a bare JSON array with no top-level `meta`, `data`, or `error` envelope.
- Each array item represents one charger at the site and can be joined to runtime/summary payloads via `serialNumber`.
- Timestamp fields use extended ISO-8601 strings with fractional seconds plus a bracketed zone suffix such as `Z[UTC]`.

Observed fields:
- `serialNumber`: charger serial number used to join the record to summary/runtime data.
- `siteId`: numeric site identifier echoed by the service.
- `upgradeStatus`: integer firmware-upgrade state code. Semantics are not yet decoded; preserve the raw value.
- `currentFwVersion`: currently installed charger firmware.
- `targetFwVersion`: target charger firmware for update comparison.
- `lastSuccessfulUpgradeDate`: timestamp of the last successful firmware upgrade.
- `lastUpdatedAt`: service timestamp for the current firmware-details record.
- `statusDetail`: optional additional upgrade-state detail; often `null`.
- `isAutoOta`: whether automatic OTA behavior is enabled for the charger.

Notes:
- Observed: the captured browser request succeeded with session cookies even though `e-auth-token` was `null`.
- Implementation: preserve the standard session headers for client behavior rather than treating the browser capture as proof that `e-auth-token` can be omitted generally.
- Implementation: `401`, `403`, and `404` are treated as optional endpoint unavailable; JSON `null` is normalized to an empty list; non-list JSON is invalid. Firmware details are cached for one hour with a 15-minute stale-on-error backoff.
- The original trace contained live cookies, XSRF tokens, JWT-bearing cookie values with account identifiers, a real site ID, a real charger serial number, and a client-facing proxy address. Those values are intentionally replaced with placeholders here.

### 2.2.3 EV Feature Flags
```
GET /service/evse_management/api/v1/config/feature-flags?site_id=<site_id>[&country=<country>]
```
Returns site-wide and per-charger capability flags.
Captured web requests include authenticated cookies, session tokens, user IDs, site IDs, and charger serials; redact those fields before sharing traces or committing samples.

Observed query parameters:
- `site_id`: numeric site identifier.
- `country`: optional ISO country code used by the web UI; observed values include `DE` and `AU`.

Example response:
```json
{
  "meta": {
    "serverTimeStamp": "2026-03-28T05:13:14.438+00:00"
  },
  "data": {
    "evse_charging_mode": true,
    "evse_launch_countries": true,
    "EVSE-SERIAL-0001": {
      "evse_charge_level_gen1": false,
      "evse_ble_control": true,
      "evse_enpki_support": true,
      "evse_ctep_certification": false,
      "dynamic_load_supported": true,
      "evse_operating_voltage": false,
      "phase_config_support": true,
      "evse_charge_level_control": false,
      "evse_authentication": true,
      "new_connect_to_internet_flow": false,
      "iqevse_itk_fw_upgrade_status": false,
      "local_green_charging": false,
      "evse_charging_modes_cancel_task": true,
      "iqevse_rfid": true,
      "na_gen2_add_devices": true,
      "max_current_config_support": true,
      "evse_network_settings": true,
      "iqevse_meter_connection": false,
      "evse_gateway_connectivity": true,
      "evse_v2_livestream": false,
      "plug_and_charge": false,
      "evse_connect_to_internet_flow_cellular": false,
      "evse_connector_lock": false,
      "evse_wifi_recommendation": true,
      "evse_ocpp_server_settings": true,
      "rcd_breaker_confirmation": true
    },
    "evse_charge_range_slider": false,
    "off_peak_schedule": true,
    "evse_phase_switching": true,
    "ev_charging": true,
    "evse_beta_users": false,
    "evse_prelogin_cta": false,
    "default_off_peak_schedule": false,
    "iqevse_smart_charging": false,
    "evse_tamper_detection": true,
    "iqevse_usebatterynew": false,
    "ev_charger_faqs": true,
    "evse_storm_guard": false,
    "evse_auto_local_connection": false,
    "evse_activation_logs": true,
    "evse_ev_integration": false
  },
  "error": {}
}
```

Observed structure:
- Top-level booleans under `data` are site-wide capability gates that drive global EVSE UI availability.
- Nested objects keyed by charger serial contain per-device capability flags. Replace the key with a placeholder such as `EVSE-SERIAL-0001` when documenting captures.
- `meta.serverTimeStamp` is an ISO 8601 timestamp string in this endpoint, unlike some other EVSE endpoints that return epoch milliseconds.

Observed site-level flags:
- `evse_charging_mode`, `evse_multi_step_auto_ota`, `evse_launch_countries`, `evse_charge_range_slider`, `off_peak_schedule`, `evse_phase_switching`, `ev_charging`
- `evse_beta_users`, `evse_prelogin_cta`, `default_off_peak_schedule`, `iqevse_smart_charging`, `iqevse_usebatterynew`
- `evse_tamper_detection`, `ev_charger_faqs`, `evse_storm_guard`, `evse_auto_local_connection`, `evse_activation_logs`, `evse_bi_di_ux`, `evse_ev_integration`

Observed per-charger flags:
- Connectivity and setup: `evse_ble_control`, `evse_enpki_support`, `evse_network_settings`, `evse_gateway_connectivity`, `evse_connect_to_internet_flow_cellular`, `evse_wifi_recommendation`, `new_connect_to_internet_flow`
- Electrical and load management: `dynamic_load_supported`, `phase_config_support`, `max_current_config_support`, `evse_operating_voltage`, `rcd_breaker_confirmation`
- Charging controls: `evse_charge_level_gen1`, `evse_charge_level_control`, `evse_charging_modes_cancel_task`, `local_green_charging`, `iqevse_external_meter_green_charging`, `plug_and_charge`
- Identity and integrations: `evse_authentication`, `iqevse_rfid`, `evse_ocpp_server_settings`, `iqevse_meter_connection`, `evse_v2_livestream`, `evse_connector_lock`, `evse_itk_external_meter_list`
- Rollout and certification gates: `evse_ctep_certification`, `iqevse_itk_fw_upgrade_status`, `iqevse_itk_additional_fw_upgrade`, `na_gen2_add_devices`, `ocmf_eichrecht_mail_report`

### 2.3 Start Live Stream
```
GET /service/evse_controller/<site_id>/ev_chargers/start_live_stream
```
Initiates a short burst of rapid status updates.

Example response:
```json
{
  "meta": {
    "serverTimeStamp": 1770000000000
  },
  "data": {
    "liveStreamTopicList": [
      "v1/evse/prod/live-stream/<stream_key>"
    ],
    "liveStreamDuration": 900
  },
  "error": {}
}
```

Observed behavior:
- The response returns one or more live topic identifiers plus a 15-minute duration.
- Observed EVSE topic shape is `v1/evse/prod/live-stream/<charger_sn>`.
- The topic can be subscribed over the same AWS IoT WebSocket/custom-authorizer transport documented in `2.9.2.a`, using the site live-status authorizer credentials and `Origin: https://enlighten.enphaseenergy.com`.
- A bounded 30-second local probe subscribed successfully to the returned EVSE topic while the charger was connected but not plugged in; no MQTT messages were emitted in that idle state.
- A bounded active-charging capture also subscribed successfully to the returned EVSE topic, but the topic emitted no MQTT messages during a labeled 30-second capture. In the same window the gateway Live Status topic emitted binary frames and Live Vitals emitted JSON frames.
- A later HAR did not preserve WebSocket/MQTT frames, but the captured web-app JavaScript shows the EVSE live-status component expects this topic to emit JSON payloads. The frontend parses `payloadString` as JSON, matches the destination topic with the EVSE live-stream regex, and decodes fields such as `power_W`, `power_W_l1`, `power_W_l2`, `power_W_l3`, `power_W_exp`, and phase-suffixed `meter` values. The EVSE-only energy-flow calculation treats `power_W / 1000` as charging kW, `power_W_exp / 1000` as discharging/export kW, and `meter / 1000` as grid kW. This is code-derived until a real EVSE MQTT frame is captured.
- The EVSE-specific AWS authorizer path observed in the frontend is `GET /service/evse_controller/api/v1/aws_sigv4/livestream/<site_id>`. It returns the same style of AWS IoT custom-authorizer fields used by the other MQTT live streams.
- The same JavaScript includes a per-charger retry path, `GET /service/evse_controller/<site_id>/ev_chargers/<charger_sn>/retry_start_live_stream`, used when the UI retries a device live-stream subscription.
- A bounded 60-second active-charging probe using the EVSE-specific authorizer connected and subscribed to the EVSE topic successfully, but still received no EVSE topic messages. A parallel probe mirroring the web-app Live Vitals behavior subscribed to both `v1/live-debug/json/<stream_id>` and the EVSE topic; it received Live Vitals JSON frames every roughly 5 seconds and no EVSE topic frames.
- Additional bounded 30-second probes tried the EVSE-specific authorizer with the exact topic, `v1/evse/prod/live-stream/+`, `v1/evse/prod/live-stream/#`, QoS 1, MQTT 3.1, MQTT 3.1.1, the gateway Live Status authorizer, the Live Vitals authorizer plus EVSE topic, retry-before-subscribe, and retry-after-subscribe. Exact-topic subscriptions received successful MQTT SUBACK grants, confirming the broker accepted the subscription, but no EVSE topic frames were received. Omitting `site-id` from the MQTT username caused AWS IoT disconnects, so `site-id` appears required.
- A subsequent request to `GET /service/evse_sse/subscribeEvent?key=<site_id>` was observed immediately afterward. The browser request used `Accept: text/event-stream`, `Cache-Control: no-cache`, `Pragma: no-cache`, a bearer JWT in `Authorization`, `e-auth-token`, and the same `/web/<site_id>/today/graph/hours?v=3.4.0` referer. A short active-charging local probe returned only the `INIT` event and an empty message payload.
- Open capture target: identify the web source for the charger card's voltage/current/power fields. They were visible in the browser UI during charging, but were not present in the EVSE MQTT topic, the Live Vitals MQTT JSON payload, the short SSE probe, or the two observed status endpoints.

### 2.4 Stop Live Stream
```
GET /service/evse_controller/<site_id>/ev_chargers/stop_live_stream
```
Ends the fast polling window.

Observed web-app response:
```json
{
  "meta": {
    "serverTimeStamp": 1770000000000
  },
  "data": null,
  "error": {}
}
```

The request uses the same authenticated web XHR shape as `start_live_stream`: `Accept: */*`, Enlighten session cookies, `e-auth-token`, `Referer: /web/<site_id>/today/graph/hours?v=3.4.0`, and `X-Requested-With: XMLHttpRequest`.

Legacy or normalized client responses may still use:
```json
{ "status": "accepted" }
```

### 2.5 EV Charger Config (Authentication, Phase, Default Charge Level)
```
POST /service/evse_controller/api/v1/<site_id>/ev_chargers/<sn>/ev_charger_config
Body: [
  { "key": "rfidSessionAuthentication" },
  { "key": "sessionAuthentication" }
]
Headers:
  Accept: application/json, text/javascript, */*; q=0.01
  Cookie: ...; XSRF-TOKEN=<token>; ...
  e-auth-token: <token-or-null>
  X-Requested-With: XMLHttpRequest
```
Fetches the current authentication requirements for charging sessions.
The same endpoint also returns other charger configuration keys when requested in the body array.
Observed from the Enlighten web UI as an `XMLHttpRequest` `POST`, with the requested config keys supplied in the JSON body rather than query parameters.

Example response:
```json
{
  "meta": { "serverTimeStamp": 1760000000000, "rowCount": 2 },
  "data": [
    {
      "key": "rfidSessionAuthentication",
      "value": null,
      "reqValue": null,
      "status": 1
    },
    {
      "key": "sessionAuthentication",
      "value": null,
      "reqValue": null,
      "status": 1
    }
  ],
  "error": {}
}
```

Observed phase/default-charge read request:
```json
[
  { "key": "DefaultChargeLevel" },
  { "key": "phase_switch_config" },
  { "key": "ConnectorLock" }
]
```

Observed response (anonymized):
```json
{
  "meta": { "serverTimeStamp": 1760000000000, "rowCount": 3 },
  "data": [
    {
      "key": "DefaultChargeLevel",
      "value": null,
      "reqValue": null,
      "status": 1
    },
    {
      "key": "phase_switch_config",
      "value": "3",
      "reqValue": null,
      "status": 1
    },
    {
      "key": "ConnectorLock",
      "value": null,
      "reqValue": null,
      "status": 1
    }
  ],
  "error": {}
}
```

Enable or disable app authentication:
```
PUT /service/evse_controller/api/v1/<site_id>/ev_chargers/<sn>/ev_charger_config
Body: [ { "key": "sessionAuthentication", "value": "enabled" } ]
```

Example response (enable request):
```json
{
  "meta": { "serverTimeStamp": 1760000000000, "rowCount": 1 },
  "data": [
    {
      "key": "sessionAuthentication",
      "value": "disabled",
      "reqValue": "enabled",
      "status": 2
    }
  ],
  "error": {}
}
```

Disable request payload:
```json
[
  { "key": "sessionAuthentication", "value": "disabled" }
]
```

Update the default charge level:
```
PUT /service/evse_controller/api/v1/<site_id>/ev_chargers/<sn>/ev_charger_config
Body: [ { "key": "DefaultChargeLevel", "value": <amps> } ]
```

Observed request payloads used integer amp values:
```json
[
  { "key": "DefaultChargeLevel", "value": 30 }
]
```

Observed response (anonymized):
```json
{
  "meta": { "serverTimeStamp": 1760000000000, "rowCount": 1 },
  "data": [
    {
      "key": "DefaultChargeLevel",
      "value": "30",
      "reqValue": null,
      "status": 2
    }
  ],
  "error": {}
}
```

Notes:
- `sessionAuthentication` controls "Auth via App"; `rfidSessionAuthentication` controls RFID auth.
- `phase_switch_config` appears to expose the charger's phase-switching mode; observed read values include `"3"`, and earlier captures used `"auto"`.
- `ConnectorLock` can be requested through the same config read endpoint; the observed value was `null`.
- `DefaultChargeLevel` was observed as `null` before update, which appears to indicate unset/disabled state. After writing `30`, the service returned `"30"` as a string with `status=2` and `reqValue=null`.
- Implementation: Home Assistant exposes the Default Charge Level number entity only when this config read returns the `DefaultChargeLevel` key for that charger. Existing charge-level feature flags are treated as advisory because firmware/cloud rollout can differ across otherwise similar chargers.
- Implementation: the Default Charge Level entity uses the same discovered charger amp limits as Charging Amps, but writes this persistent config value instead of updating the immediate/session charging setpoint.
- When either setting is enabled, charging sessions require user authentication before starting.
- Observed: read responses use `status=1`; update responses use `status=2`. Authentication-setting update responses have shown `value` as the prior state and `reqValue` as the desired state, while `DefaultChargeLevel` returned the accepted target in `value` and left `reqValue` as `null`.
- Observed: both `sessionAuthentication` and `rfidSessionAuthentication` can return `null` for both `value` and `reqValue`. When the setting key is present, the Enphase app represents this state as Off, so the integration normalizes it to disabled. A missing setting key remains unknown/unsupported.
- Observed web captures succeeded with session cookies, XSRF cookies, `e-auth-token`, and sometimes an `Authorization: Bearer <token>` overlay. Treat the auth requirements here as UI-path dependent.
- Implementation: config reads first use normal today JSON headers plus the control `Authorization` overlay. If that returns `Unauthorized` or `403` while bearer auth was present, the client retries the same POST with both `Authorization` and `e-auth-token` explicitly suppressed.
- Privacy: real captures include site IDs, charger serial numbers, cookies, JWTs, names, and email addresses. Redact all such values when preserving examples.

### 2.6 Session History (Filter Criteria)
```
GET /service/enho_historical_events_ms/<site_id>/filter_criteria?source=evse&requestId=<uuid>&username=<user_id>
Headers:
  Accept: application/json, text/javascript, */*; q=0.01
  Authorization: Bearer <jwt>
  Cookie: ...; XSRF-TOKEN=<token>; ...
  e-auth-token: <session_id>
  requestid: <uuid>
  username: <user_id>
  X-Requested-With: XMLHttpRequest
```
Returns the chargers available for session history lookups (IDs + display names).
Notes:
- Implementation: the current client uses the stored access token first for session-history calls, then falls back to the manager-token cookie JWT.
- Implementation: `e-auth-token` is derived from the JWT `session_id` claim rather than reusing the raw bearer token value.
- Implementation: `username` is derived from the JWT `user_id` claim when present.
- `requestid` is a UUID generated per request.

### 2.7 Session History
```
POST /service/enho_historical_events_ms/<site_id>/sessions/<sn>/history
Body: {
  "source": "evse",
  "params": {
    "offset": 0,
    "limit": 20,
    "startDate": "16-10-2025",
    "endDate": "16-10-2025",
    "timezone": "Region/City"
  }
}
Headers:
  Accept: application/json, text/javascript, */*; q=0.01
  Authorization: Bearer <jwt>
  Cookie: ...; XSRF-TOKEN=<token>; ...
  e-auth-token: <session_id>
  requestid: <uuid>
  username: <user_id>
  X-Requested-With: XMLHttpRequest
```
Returns a list of recent charging sessions for the requested charger. `startDate`/`endDate` are `DD-MM-YYYY` in the site's local timezone. The response indicates whether more pages are available via `hasMore`.
Notes:
- Implementation: the current client uses the stored access token first for session-history calls, then falls back to the manager-token cookie JWT.
- Implementation: `e-auth-token` is derived from the JWT `session_id` claim rather than reusing the raw bearer token value.
- Implementation: `username` is derived from the JWT `user_id` claim when present.
- `requestid` is a UUID generated per request.

Example response:
```json
{
  "source": "evse",
  "timestamp": "2025-10-16T08:45:14.230924038Z",
  "data": {
    "result": [
      {
        "id": "123456789012:1700000001",
        "sessionId": 1700000001,
        "startTime": "2025-10-16T00:02:08.826Z[UTC]",
        "endTime": "2025-10-16T04:39:50.618Z[UTC]",
        "authType": null,
        "authIdentifier": null,
        "authToken": null,
        "aggEnergyValue": 29.94,
        "activeChargeTime": 15284,
        "milesAdded": 120.7,
        "sessionCost": 0.77,
        "costCalculated": true,
        "manualOverridden": true,
        "avgCostPerUnitEnergy": 0.03,
        "sessionCostState": "COST_CALCULATED",
        "chargeProfileStackLevel": 4
      }
    ],
    "hasMore": true,
    "startDate": "10-08-2022",
    "endDate": "16-10-2025",
    "offset": 0,
    "limit": 20
  }
}
```
Fields of interest:
- `aggEnergyValue` — energy delivered in kWh for the session.
- `activeChargeTime` — session duration in seconds while actively charging.
- `milesAdded` — range added in miles (region-specific; may be `null`).
- `sessionCost`/`avgCostPerUnitEnergy` — cost metadata when tariffs are configured.
- `authType`/`authIdentifier`/`authToken` — authentication metadata recorded by Enlighten (often `null` for residential accounts).
- `sessionCostState` — cost calculation status such as `COST_CALCULATED`.

### 2.7.1 EVSE Timeseries (Daily + Lifetime Energy)
```
GET /service/timeseries/evse/timeseries/daily_energy?site_id=<site_id>&source=evse&requestId=<uuid>&start_date=<YYYY-MM-DD>[&username=<user_id>]
GET /service/timeseries/evse/timeseries/lifetime_energy?site_id=<site_id>&source=evse&requestId=<uuid>[&username=<user_id>]
Headers:
  Accept: application/json, text/javascript, */*; q=0.01
  Authorization: Bearer <jwt>
  Cookie: ...; XSRF-TOKEN=<token>; ...
  e-auth-token: <session_id>
  requestid: <uuid>
  username: <user_id>
  X-Requested-With: XMLHttpRequest
```
Returns EV charger daily or lifetime energy keyed by charger serial.

Notes:
- The request parameter must be `site_id`. Requests using `siteId` were rejected by Enphase with `400 BAD_REQUEST` and the message `Required request parameter 'site_id' ... is not present`.
- The daily endpoint also requires `start_date` in `YYYY-MM-DD`. Omitting it produced `400 BAD_REQUEST` with `Required request parameter 'start_date' ... is not present`.
- The integration refreshes both endpoints for EVSE sites and merges normalized `evse_daily_energy_kwh`, `evse_lifetime_energy_kwh`, interval, last-report, and source metadata into charger runtime payloads.
- The current client uses a bearer token from the stored access token when available, otherwise from the `enlighten_manager_token_production` cookie.
- The current client does not reuse the raw bearer token as `e-auth-token` here. Instead it decodes the JWT locally and sends the JWT `session_id` claim as `e-auth-token` when present.
- `username` should match the JWT `user_id` claim when present.
- `requestId` / `requestid` is a UUID generated per request.

Example daily request:
```
GET /service/timeseries/evse/timeseries/daily_energy?site_id=<site_id>&source=evse&requestId=<uuid>&start_date=2026-03-13&username=<user_id>
```

Example lifetime request:
```
GET /service/timeseries/evse/timeseries/lifetime_energy?site_id=<site_id>&source=evse&requestId=<uuid>&username=<user_id>
```

Observed lifetime response:
```json
{
  "iqevc": [11882.44, 17529.02, 0, 0, 0, "..."],
  "system_id": 1234567,
  "start_date": "2025-08-11",
  "last_report_date": 1774674802,
  "update_pending": false,
  "charger_iqevc": {
    "2025": {
      "EV000000000000": 116300.14
    },
    "2026": {
      "EV000000000000": 34357.39
    }
  }
}
```

Observed response behavior:
- The lifetime endpoint returns a flat JSON object rather than a `meta`/`data` envelope.
- `iqevc` is a dense energy-bucket array for the site-level EVSE stream; zero-valued buckets are preserved and should not be dropped.
- `charger_iqevc` is nested by calendar year and then charger UUID/serial, allowing yearly per-charger totals to coexist with the site-level series.
- `start_date` is the earliest bucket date in the returned history and `last_report_date` is an epoch-seconds cursor for the most recent charger report.
- `update_pending` was observed as `false`.
- The observed web capture succeeded with the simpler URL variant `?site_id=<site_id>` and no `Authorization`, `requestId`, or `username` headers; `e-auth-token` was the literal string `null`. Treat that as a web-session variant, not proof that non-browser clients can omit the documented auth/session headers.

### 2.8 Lifetime Energy (time-series buckets)
```
GET /pv/systems/<site_id>/lifetime_energy
Headers:
  Accept: application/json, text/javascript, */*; q=0.01
  Cookie: BP-XSRF-Token=<token>; XSRF-TOKEN=<token>; ...   # normal Enlighten session cookies
  e-auth-token: <token>
  X-Requested-With: XMLHttpRequest
```
Returns aggregated Wh buckets for production/consumption and related flows. Cloud responses present arrays of equal length representing historical intervals (15 min or daily depending on site configuration).

Example shape (values truncated/obfuscated):
```json
{
  "system_id": 1234567,
  "start_date": "2023-08-10",
  "last_report_date": 1765442709,
  "update_pending": false,
  "production": [12000, 8300, 9000, 26000, ...],
  "consumption": [7100, 13400, 15800, 14100, ...],
  "solar_home": [2700, 3300, 5400, 6000, ...],
  "solar_grid": [8300, 4400, 2600, 18600, ...],
  "grid_home": [4200, 9800, 10700, 7700, ...],
  "import": [null, null, ...],
  "export": [null, null, ...],
  "charge": [null, null, ...],
  "discharge": [null, null, ...],
  "solar_battery": [null, null, ...],
  "battery_home": [null, null, ...],
  "battery_grid": [null, null, ...],
  "grid_battery": [null, null, ...],
  "evse": [0, 0, ...],
  "heatpump": [],
  "water_heater": []
}
```
Notes:
- `start_date` marks the earliest bucket; `last_report_date` is an epoch seconds cursor.
- Arrays are long. The implementation treats missing or zero-only `evse`, `heatpump`, and `water_heater` channels as absent unless they came from the HEMS lifetime fallback.
- The integration can merge `/systems/<site_id>/hems_consumption_lifetime` into this lifetime payload. Grid and battery flows may also be derived from component flows when direct channels are absent.
- When present, `evse` values report charging energy attributed to the EVSE.

### 2.8.1 Site Today Snapshot (Quarter-Hour Energy + Battery Context)
```
GET /pv/systems/<site_id>/today
Headers:
  Accept: */*
  Cookie: <authenticated Enlighten session cookies>
  X-Requested-With: XMLHttpRequest
```
Returns the current-day site-energy view used by Enlighten, including quarter-hour arrays, totals, battery context, and verbose diagnostic/logger payloads.

Example response shape:
```json
{
  "system_id": 1234567,
  "siteStatus": "normal",
  "pending": null,
  "next_report": null,
  "start_date": "2026-03-28",
  "end_date": "2026-03-28",
  "stats": [
    {
      "grid_changes": {},
      "production": [0, 0, 0, "..."],
      "consumption": [93, 93, 210, 372, "..."],
      "import": [93, 93, 210, "..."],
      "export": [0, 0, 0, "..."],
      "charge": [0, 0, 0, "..."],
      "discharge": [0, 0, 0, "..."],
      "soc": [5, 5, 5, "..."],
      "generator": [0, 0, 0, "..."],
      "grid_import": [93, 93, 210, "..."],
      "solar_home": [0, 0, 0, "..."],
      "solar_battery": [0, 0, 0, "..."],
      "solar_grid": [0, 0, 0, "..."],
      "generator_home": [0, 0, 0, "..."],
      "generator_battery": [0, 0, 0, "..."],
      "generator_grid": [0, 0, 0, "..."],
      "battery_home": [0, 0, 0, "..."],
      "battery_grid": [0, 0, 0, "..."],
      "grid_battery": [0, 0, 0, "..."],
      "grid_home": [93, 93, 210, "..."],
      "start_time": 1774652400,
      "interval_length": 900,
      "totals": {
        "production": 0,
        "consumption": 3957,
        "import": 1911,
        "export": 44,
        "charge": 71,
        "discharge": 2157,
        "soc": 360,
        "generator": 0,
        "grid_import": 6728400,
        "solar_home": 0,
        "solar_battery": 0,
        "solar_grid": 44,
        "generator_home": 0,
        "generator_battery": 0,
        "generator_grid": 0,
        "battery_home": 2139,
        "battery_grid": 18,
        "grid_battery": 71,
        "grid_home": 1861
      },
      "evse": [0, 0, 0, "..."],
      "heatpump": [1, 2, 1, "..."],
      "water_heater": [0, 0, 0, "..."]
    }
  ],
  "isExportRate": true,
  "isImportRate": true,
  "statusDetails": {
    "reason": null,
    "statusSeverity": "warning",
    "errorCount": 0,
    "totalCount": 24,
    "substatusApplicable": false
  },
  "battery_details": {
    "aggregate_soc": 5
  },
  "connectionDetails": [
    {
      "cellular": false,
      "wifi": null,
      "ethernet": true,
      "interface_ip": {
        "wifi": null,
        "ethernet": "192.0.2.10"
      }
    }
  ],
  "batteryConfig": {
    "battery_backup_percentage": 5,
    "buyback_export_plan": "",
    "charge_from_grid": false,
    "env_storage_settings": {
      "GW0000000000": {
        "soc": 5,
        "cfg": "NOT_ALLOWED",
        "vls": 5,
        "src": "ENL",
        "configState": "COMPLETED"
      }
    },
    "grid_mode_settings": {
      "battery_grid_mode": 3
    },
    "usage": "self-consumption",
    "very_low_soc": 5
  },
  "system": {
    "connection_type": "ethernet",
    "statusCode": "normal"
  },
  "loggers": ["<redacted>"],
  "update_pending": false,
  "last_report_date": 1774674802
}
```

Observed structure:
- `stats[0]` contains 96 quarter-hour buckets (`interval_length=900`) plus a `totals` object using the same metric names.
- The payload co-locates energy-flow arrays, battery state, connection details, and internal logging strings in one response.
- `heatpump` can be populated even when `production` and `evse` are all zero for the same day.
- The integration treats this `heatpump` daily total as the authoritative displayed heat-pump energy and the source for derived heat-pump power. Running and idle power may be smoothed over longer same-day monotonic energy windows when enough history is available, with raw delta values retained in diagnostics. The HEMS `energy-consumption` endpoint is optional split metadata for diagnostics and source attribution.
- `siteStatus="normal"` coexisted with `statusDetails.statusSeverity="warning"` in the observed capture.
- `batteryConfig` mirrors several BatteryConfig-service concepts (`usage`, backup percentage, grid-mode settings, storm state) but adds internal class/ID fields and gateway-serial keyed maps.

### 2.B Site-Level Energy, Inventory, and Events

### 2.9 Device Inventory (Site Hardware Cards)
```
GET /app-api/<site_id>/devices.json
Headers:
  Accept: */*
  Cookie: ...; XSRF-TOKEN=<token>; ...
  e-auth-token: <token>
  X-Requested-With: XMLHttpRequest
```
Returns grouped device inventory for the site (Gateway, batteries, system controller, relays, meters, EV charger, etc.).

Example response shape:
```json
{
  "result": [
    {
      "type": "envoy",
      "devices": [
        {
          "name": "IQ Gateway",
          "serial_number": "GW0000000000",
          "sku_id": "SC100G-M000ROW",
          "connected": true,
          "status": "normal",
          "statusText": "Normal",
          "ip": "192.0.2.10",
          "ap_mode": true,
          "envoy_sw_version": "D8.X.XXXX",
          "supportsEntrez": true,
          "last_report": 1770000000,
          "show_connection_details": true,
          "warranty_end_date": "2030-09-18"
        }
      ]
    },
    {
      "type": "encharge",
      "devices": [
        {
          "name": "IQ Battery 5P FlexPhase",
          "serial_number": "BT0000000001",
          "sku_id": "IQBATTERY-5P-3P-INT",
          "channel_type": "IQ Battery",
          "status": "normal",
          "statusText": "Normal",
          "last_report": 1770000010,
          "sw_version": "522-00002-01-vX.Y.Z_rel/31.44",
          "warranty_end_date": "2040-09-18"
        }
      ]
    },
    {
      "type": "enpower",
      "devices": [
        {
          "name": "IQ System Controller 3 INT",
          "serial_number": "SC0000000000",
          "sku_id": "SC100G-M000ROW",
          "channel_type": "IQ System Controller",
          "status": "normal",
          "statusText": "Normal",
          "last_report": 1770000020,
          "sw_version": "522-00003-01-vX.Y.Z_rel/31.44",
          "warranty_end_date": "2035-09-18"
        }
      ]
    },
    { "curr_date_site": "2026-02-08" }
  ]
}
```
Additional observed buckets (anonymized excerpt):
```json
{
  "result": [
    {
      "type": "meter",
      "devices": [
        {
          "name": "IQ Envoy",
          "serial_number": "GW0000000000EIM1",
          "sku_id": null,
          "channel_type": "Localized production meter label",
          "status": "normal",
          "statusText": "Normal",
          "last_report": 1772183700
        }
      ]
    },
    {
      "type": "dryContactLoads",
      "devices": [
        {
          "name": "NC1",
          "load_name": "Aux Load 1",
          "load_type": "others",
          "status": "normal",
          "statusText": "Normal",
          "last_report": 1772183578
        }
      ]
    },
    {
      "type": "hemsDevices",
      "devices": [
        {
          "gateway": [
            {
              "device-type": "IQ_ENERGY_ROUTER",
              "device-uid": "<site_id>_IQ_ENERGY_ROUTER_1",
              "ip-address": "192.0.2.11"
            }
          ],
          "heat-pump": [{ "device-type": "HEAT_PUMP", "device-uid": "<site_id>_HEAT_PUMP_1" }],
          "evse": [],
          "water-heater": []
        }
      ]
    },
    { "curr_date_site": "2026-02-27" }
  ]
}
```
Observed structure:
- `result[]` is a mixed array containing typed buckets (`{type, devices}`) and metadata objects (for example `curr_date_site`).
- The implementation also accepts a raw list response and wrapped `data`, `items`, `systems`, or `result` lists.
- Each bucket's `type` identifies the device family; `devices[]` may be empty.
- Bucket type keys may be `type`, `deviceType`, or `device_type`; member arrays may be `devices`, `items`, or `members`.
- Retired members are skipped by the integration.
- Common device fields: `name`, `serial_number`, `sku_id`, `status`, `statusText`, `last_report`.
- Observed `type` values include `envoy`, `storage`, `q_relay`, `meter`, `encharge`, `enpower`, `generator`, `wirelessRangeExtender`, `dryContactLoads`, `stringInverters`, `iqCollars`, and `hemsDevices`.
- Optional fields vary by type (`ip`, `ap_mode`, `connected`, `supportsEntrez`, `envoy_sw_version`, `channel_type`, `sw_version`, `warranty_end_date`, `load_name`, `load_type`, etc.).
- `channel_type` labels may be localized by site locale (for example French meter labels).
- Meter examples observed both production and consumption labels via synthetic serial suffixes such as `EIM1` and `EIM2`.
- `dryContactLoads` entries may expose user-assigned names such as garage/PV load labels.
- Some sites include a nested `type: "hemsDevices"` bucket in `/devices.json`, reusing the hierarchical HEMS shape documented in `2.17`.

### 2.9.1 Filtered Site-Device Inventory
```
POST /service/site-device/api/v2/devices/list
Headers:
  Accept: application/json
  Content-Type: application/json
  Cookie: ...; XSRF-TOKEN=<token>; ...
  e-auth-token: <token>
```
Returns a filtered device list for the system dashboard and device-management views. The request body carries the site identifier plus device-family filters and requested extra fields.

Example request body:
```json
{
  "site_id": "1234567",
  "filters": {
    "include_retired": false,
    "include_sub_device": false,
    "core_device_types": ["IQ_AIR"],
    "extra_fields": ["WARRANTY", "STATUS"]
  }
}
```

Example response:
```json
{
  "type": "device-details",
  "timestamp": "2026-03-09T05:46:35.782815934Z[UTC]",
  "data": {
    "devices": []
  }
}
```

Observed request fields:
- `site_id`: numeric site identifier passed in the JSON body rather than the URL path.
- `filters.include_retired`: includes retired devices when `true`.
- `filters.include_sub_device`: includes nested or subordinate devices when `true`.
- `filters.core_device_types`: array of requested device-family codes, observed with `IQ_AIR`.
- `filters.extra_fields`: optional extra metadata groups to hydrate in the response, observed with `WARRANTY` and `STATUS`.

Observed response fields:
- `type`: envelope discriminator, observed as `device-details`.
- `timestamp`: server-side generation timestamp.
- `data.devices`: array of matching devices; may be empty when no devices match the filter set.

Notes:
- This endpoint complements `/app-api/<site_id>/devices.json` by allowing the web UI to request a narrow device subset instead of the full site inventory.
- Additional `core_device_types` values were not present in this capture; preserve unknown codes verbatim until more examples are collected.

### 2.9.2 Live Stream Capability Flags
```
GET /app-api/<site_id>/show_livestream
```
Returns booleans indicating live site status and live vitals availability.

Example response:
```json
{
  "live_status": true,
  "live_vitals": true
}
```

Observed request fields:
- Path parameter `site_id`: numeric Enlighten site identifier in the URL path.
- Method: `GET`.
- Query/body: none observed.
- `Accept: application/json`.
- Browser capture authenticated with Enlighten session cookies; no explicit `e-auth-token` header was present in the observed request.
- `Referer` was the site summary page: `/app/system_dashboard/sites/<site_id>/summary`.

Observed response fields:
- `live_status`: boolean gate for live site-status streaming availability.
- `live_vitals`: boolean gate for live vitals/telemetry streaming availability.

Notes:
- The raw browser trace included session cookies, a JWT-bearing cookie, user identifiers, and an exact site ID; those values are intentionally omitted here and replaced with placeholders.
- This endpoint appears to be a lightweight capability check used before the UI enables live monitoring flows.
- It complements the HEMS live-stream toggle endpoints documented in `2.F`, but does not itself start a stream or return a stream topic/key.

### 2.9.2.a Site Live-Status MQTT Authorizer
```
GET /pv/aws_sigv4/livestream.json?serial_num=<gateway_sn>
```
Returns an AWS IoT custom-authorizer payload for the Enlighten web-app **Live Status** modal. This stream is distinct from the BatteryConfig MQTT response stream documented in `5.1`.

Example response (anonymized):
```json
{
  "live_stream_duration": 900,
  "live_stream_topic": "v1/live-stream/<stream_id>",
  "dr_event_mode": "",
  "has_load_controls": false,
  "timeout": 15,
  "aws_iot_endpoint": "a1b2c3d4e5f6g7-ats.iot.us-east-1.amazonaws.com",
  "aws_authorizer": "aws-lambda-authoriser-prod",
  "aws_token_key": "enph_token",
  "aws_token_value": "<session_id>",
  "aws_digest": "<base64_signature>"
}
```

Observed request fields:
- Query parameter `serial_num`: Envoy/IQ Gateway serial number. A charger serial was rejected with `400` and an `Envoy not found` style message in local probing.
- Browser capture sent `Accept: */*`, `X-Requested-With: XMLHttpRequest`, Enlighten session cookies, and `e-auth-token`.
- Browser capture referer was the web live graph route: `/web/<site_id>/today/graph/hours?v=3.4.0`.

Observed MQTT usage:
- MQTT WebSocket URL is `wss://<aws_iot_endpoint>/mqtt`.
- WebSocket request uses `Sec-WebSocket-Protocol: mqtt`.
- MQTT authentication is carried in the MQTT `username` field:
  `?x-amz-customauthorizer-name=<aws_authorizer>&<aws_token_key>=<aws_token_value>&site-id=<site_id>&x-amz-customauthorizer-signature=<urlencoded aws_digest>&env=production`
- The web-app stream used MQTT 3.1.1 over WebSockets, no password, path `/mqtt`, and `Origin: https://enlighten.enphaseenergy.com`.
- Subscribe to the returned `live_stream_topic`.
- Observed `live_stream_duration` was `900` seconds; local investigation deliberately disconnected after 60 seconds.
- In a 60-second local capture, the topic emitted one binary message per second after successful subscribe.

Observed payload format:
- The web-app live grid-status chip is driven from the MQTT payload field `meters.gridRelay`.
- `meters.gridRelay=OPER_RELAY_CLOSED` maps to UI type `onGrid` and displays "On Grid".
- `meters.gridRelay=OPER_RELAY_OPEN` maps to UI type `offGrid` and displays "Off Grid".
- Production Live Status frames use the `DataMsg` protobuf schema. `DataMsg.meters` is field 3 and `MeterSummaryData.grid_relay` is nested field 5; the frontend converts that enum to the `OPER_RELAY_*` strings above.
- Numeric power values in the binary shape are signed varints scaled by `1_000_000` to kW.
- The first subfield in each repeated power group matched the visible web-app Live Status values. The second subfield often held a nearby related value; its meaning is not confirmed.

Decoded field mapping from two web-app screenshot comparisons:

| Live Status UI value | Protobuf-like path | Conversion | Notes |
| --- | --- | --- | --- |
| Solar producing | root `field3.field1.field1` | divide by `1_000_000` | Positive value matched the blue solar node. |
| Battery charging/discharging | root `field3.field2.field1` | divide by `1_000_000` | Positive value matched green "Discharging" in observed captures. Negative/charging semantics still need a direct charge-mode capture. |
| Grid import/export | root `field3.field3.field1` | divide by `1_000_000` | Negative value matched "Exporting"; near-zero matched "Idle". |
| Home consuming | root `field3.field4.field1` | divide by `1_000_000` | Matched orange home load. The balance relationship was `home ~= solar + battery + grid`. |
| EV charger consuming | root `field3.field4.field2` | divide by `1_000_000` | Candidate mapping. During an active charging capture, early frames were around `7.10-7.12` kW while the Live Status charger card showed `7.1` kW. A later labeled 30-second capture saw this path fluctuate from roughly `7.25` to `9.45` kW while the EVSE MQTT topic remained silent. |
| Battery state of charge | root `field3.field6` | integer percent | Matched the battery charge percentage. |
| System profile / mode | root `field3.field5` | enum/integer | Observed value `2` while the UI showed Self-Consumption; mapping needs more modes. |
| Grid status | root `field3.field5` | enum | `1` open, `2` closed, `3` off-grid with AC present, `4` off-grid ready for resync, `5` waiting to initialize on-grid |
| Generator relay | root `field3.field8` | enum | Uses generator relay enum values beginning at `6` |
| Battery reserve/limit hint | root `field3.field11.field1`, `field3.field11.field2` | integer / scaled integer | Observed values aligned with charge percentage and a `9900`-style value, likely a percent scaled by 100; exact semantics unconfirmed. |

Implementation notes:
- Treat this stream as an experimental short-lived live-status source. Do not auto-renew indefinitely.
- Do not log raw authorizer payloads, MQTT usernames, cookies, JWTs, exact topic IDs, site IDs, serial numbers, email addresses, or binary frames from real accounts.
- Keep captures bounded and disconnect explicitly even when the server advertises a 15-minute duration.
- The BatteryConfig MQTT authorizer at `5.1` returned a `v1/server/response-stream/<stream_id>` topic and emitted no passive telemetry in a 60-second local capture; do not conflate that response stream with this `v1/live-stream/<stream_id>` site telemetry stream.

### 2.9.2.b Site Live-Debug MQTT Authorizer
```
GET /service/system_dashboard/api_internal/cs/sites/livestream?serial_num=<gateway_sn>&live_debug=true
```
Returns an AWS IoT custom-authorizer payload used by the System Dashboard **Software Updates** page and Enlighten live-vitals surfaces. This is a separate topic from the Live Status stream in `2.9.2.a`. An older capture used `/pv/aws_sigv4/livestream.json?...&live_debug=true`; the runtime uses the newer confirmed System Dashboard route.

Example response (anonymized):
```json
{
  "live_debug_duration": 900,
  "live_debug_topic": "v1/live-debug/json/<stream_id>",
  "timeout": 15,
  "aws_iot_endpoint": "a1b2c3d4e5f6g7-ats.iot.us-east-1.amazonaws.com",
  "aws_authorizer": "aws-lambda-authoriser-prod",
  "aws_token_key": "enph_token",
  "aws_token_value": "<session_id>",
  "aws_digest": "<base64_signature>"
}
```

Observed request and MQTT usage:
- Query parameter `serial_num` is the Envoy/IQ Gateway serial number.
- Query parameter `live_debug=true` switches the authorizer response from `live_stream_topic` to `live_debug_topic`.
- Observed Live Vitals topic shape is `v1/live-debug/json/<stream_id>`.
- Browser capture used the same XHR headers as Live Status: `Accept: */*`, authenticated Enlighten cookies, `e-auth-token`, `Referer: /web/<site_id>/today/graph/hours?v=3.4.0`, and `X-Requested-With: XMLHttpRequest`.
- MQTT WebSocket URL, username custom-authorizer format, protocol version, password behavior, path `/mqtt`, and `Origin: https://enlighten.enphaseenergy.com` match `2.9.2.a`.
- Subscribe to the returned `live_debug_topic`.
- Observed `live_debug_duration` was `900` seconds; local investigation deliberately disconnected after bounded 20-second and 60-second captures.

Observed payload format:
- Payloads are plain JSON text, not protobuf.
- The stream emitted roughly every 5 seconds in local captures.
- Captured frames were approximately 8-9 KB.
- Top-level keys observed: `data_ingest_type`, `site`, `meters`, and `devices`.
- Captured JSON contains account/site/device identifiers and exact serial numbers. Store only redacted fixtures or schema summaries.
- Gateway update state is carried in `site[].site_update_info[]`, including numeric/string current and last statuses, estimated time left, total duration, and `Current essimg version`. The enclosing `site[].timestamp` identifies the source observation time.
- Per-component progress is carried in `devices[].update_info`, including component name/type, status, progress, transfer speed, and `e3_progress`. The adjacent `fw_image` contains a gateway-local path and image hash and must not be retained in state, diagnostics, fixtures, or logs.
- During an active charging capture, Live Vitals emitted one JSON message roughly every 5 seconds and still only exposed `enpower` and `encharge` device objects. No explicit EVSE/charger object was observed.

Example payload shape (anonymized and abbreviated):
```json
{
  "data_ingest_type": "new",
  "site": [
    {
      "timestamp": 1770000000,
      "agg_soc": 97,
      "freq_bias_hz": 0.005,
      "voltage_bias_v": 0.066,
      "is_split_phase": false,
      "phase_count": 1,
      "system_np": 21120,
      "production_mode": 1
    }
  ],
  "meters": [
    {"type": "production", "status": "enabled"},
    {"type": "consumption", "status": "enabled"},
    {"type": "storage", "status": "disabled"}
  ],
  "devices": [
    {
      "type": "enpower",
      "device_state": 24,
      "comm_state": "Communicating",
      "enpower_led_status": 13,
      "grid_ac_l1_v": 236218,
      "grid_freq": 49960,
      "com_ac_l1_v": 236031,
      "com_ac_freq": 49960,
      "internal_state": 16,
      "relay_state_bitmap": 963,
      "grid_relay_state": 1
    },
    {
      "type": "encharge",
      "serial_num": "<battery_serial>",
      "soc": 97,
      "led_status": 14,
      "grid_state": 1,
      "pcu": [
        {
          "type": "pcu",
          "serial_num": "<battery_microinverter_serial>",
          "comm_state": 6,
          "oper_state": 5,
          "enph_oper_state": 3,
          "grid_state": 1,
          "ac_power": -3
        }
      ]
    }
  ]
}
```

Decoded field mapping from Live Vitals screenshots:

| Live Vitals UI value | JSON path | Conversion | Notes |
| --- | --- | --- | --- |
| Site battery charge | `site[].agg_soc` | integer percent | Matched top-card battery percentage. |
| System voltage | `devices[type=enpower].grid_ac_l1_v` | divide by `1000` | Matched the top-card/system-controller voltage. |
| System frequency | `devices[type=enpower].grid_freq` | divide by `1000` | Matched the top-card/system-controller frequency. |
| System controller status | `devices[type=enpower].comm_state`, `device_state`, `enpower_led_status` | enum/string | `comm_state="Communicating"` and LED/status enums matched operational UI. |
| System controller relay state | `devices[type=enpower].relay_state_bitmap`, `grid_relay_state` | bitfield/enum | Browser UI showed MID/DER relay labels; bit mapping still needs full decoding. |
| Utility grid parameters | `devices[type=enpower].grid_ac_l1_v`, `grid_freq` | divide by `1000` | Used by the expanded system-controller view. |
| Microgrid parameters | `devices[type=enpower].com_ac_l1_v`, `com_ac_freq` | divide by `1000` | Used by the expanded system-controller view. |
| Battery device charge | `devices[type=encharge].soc` | integer percent | Matched battery card percentage. |
| Battery operational/grid state | `devices[type=encharge].device_state`, `led_status`, `grid_state`, `enchg_internal_state`, `bmu_state`, `ec_power_state` | enum/integer | Observed values matched operational/on-grid/idle UI states, but enum tables need more states. |
| Battery microinverter watts | `devices[type=encharge].pcu[].ac_power` | watts | Matched the individual IQ8D-BAT microinverter rows. |
| Meter presence/status | `meters[].type`, `meters[].status` | strings | Observed `production`, `consumption`, and `storage` meter rows. |
| EV charger vitals | not observed in Live Vitals MQTT capture | n/a | The captured JSON had system controller and battery devices but no explicit EVSE object, even while the web UI showed charger voltage/current/power during active charging. |

Implementation notes:
- Prefer this JSON stream for short-lived diagnostics/live-vitals displays rather than trying to infer controller/battery internals from the binary Live Status stream.
- Keep it opt-in/experimental and do not continuously renew the 15-minute stream window.
- Redact `serial_num`, any device IDs, site IDs, account fields, raw AWS authorizer values, and raw payloads before committing fixtures.

### 2.9.3 Latest Site Power
```
GET /app-api/<site_id>/get_latest_power
```
Returns the latest observed site power sample.

Example response:
```json
{
  "latest_power": {
    "value": 752,
    "units": "W",
    "precision": 0,
    "time": 1773207600
  }
}
```

Observed fields:
- `value`: current site power in watts.
- `units`: reported unit string, observed as `W`.
- `precision`: reported precision hint for the sample.
- `time`: Unix timestamp in seconds for the sampled value.

Notes:
- Requires the standard authenticated Enlighten session headers (`e-auth-token` plus cookies).
- The current implementation may also attach `Authorization: Bearer <token>` to some dashboard-family GETs when a usable bearer is available, even though browser captures succeeded with cookies alone.
- The implementation accepts the sample under `data`, `latest_power`, or at the top level.
- Treat a missing, non-numeric, or non-finite `value` as no sample rather than coercing to `0`.
- Normalize reported `W`, `kW`, and `mW` values to watts; payloads without a
  unit retain the historical watts assumption, while unknown units are rejected.
- Observed timestamps are epoch seconds, but the parser also accepts epoch milliseconds.
- The observed capture returned `value=-30`, confirming the field can go negative. Preserve negative samples rather than clamping to `0`; they likely represent net import or reverse power flow.
- Values at or above 1 MW are quarantined until a second comparable sample with
  a newer source timestamp confirms sustained extreme power. Repeated payloads
  with the same timestamp and timestamp-less extreme samples are not sufficient
  confirmation.
- The normalized runtime field is `current_power_consumption_w` with source `app-api:get_latest_power`.

### 2.9.3.a Multi-Gateway Phase Map

```http
GET /app-api/<site_id>/phase_map_multiple_envoy
```

Returns a map keyed by Gateway serial number. The integration uses it to select
the primary/default Gateway on multi-Gateway sites and to expose compact phase
and topology diagnostics.

```json
{
  "<gateway_sn>": {
    "connectedLoadControl": false,
    "hasGenerator": false,
    "hasIqBattery": true,
    "hasAcb": false,
    "isSplitPhase": false,
    "isProductionOnly": false,
    "isConsumptionOnly": false,
    "showSmartMeterConsumption": false,
    "totalPhase": 3,
    "hasEnpower": true,
    "hasEncharge": true,
    "showStorage": true,
    "isEnsemble": true,
    "gatewayStatus": "normal",
    "isDefaultGateway": true,
    "isPrimaryGateway": true,
    "phases": {"0": true, "1": true, "2": true},
    "any": true,
    "all": true
  }
}
```

Implementation notes:

- The dynamic object keys are device serial numbers and must be redacted in
  logs and diagnostics.
- Prefer `isPrimaryGateway`, then `isDefaultGateway`, when selecting a Gateway
  for site-level controls. A one-entry map is an unambiguous fallback.
- The payload is topology metadata and is cached for six hours. Failures use a
  one-hour retry backoff and preserve the last successful map.
- The optional endpoint must not make the authoritative device inventory
  unavailable.

### 2.9.3.b Site Bootstrap Payload
```
GET /app-api/<site_id>/data.json?app=1&device_status=non_retired&is_mobile=0
```
Returns the site bootstrap payload used by the mobile/web app shell. It includes site paths, locale/timezone, and sanitized user/owner metadata.

Example response excerpt (anonymized):
```json
{
  "app": {
    "selector": "app",
    "userId": 4,
    "user": {
      "id": 4,
      "first_name": "<redacted>",
      "last_name": "<redacted>",
      "email": "<redacted>",
      "phone": "<redacted>",
      "isHost": false,
      "country": "PT",
      "isInstaller": false,
      "hasConsumptionDataAccess": true,
      "isAdmin": false
    },
    "owner": {
      "id": 4,
      "first_name": "<redacted>",
      "last_name": "<redacted>"
    },
    "ownerOrHostMaskedEmail": "m**************<redacted>",
    "timezone": "Europe/Lisbon",
    "locale": "en",
    "weatherDisabled": false,
    "tariff": null,
    "basePath": "/pv/systems/<site_id>",
    "systemPath": "/pv/systems/<site_id>",
    "showNewView": true,
    "update_pending": false
  }
}
```

Observed notes:
- Query parameters observed in the HAR were `app=1`, `device_status=non_retired`, and `is_mobile=0`.
- Real captures contain personal data, support URLs, authenticity tokens, and signed links; those must be redacted before sharing.
- This appears to be an app bootstrap/config payload rather than a stable telemetry endpoint.

### 2.9.3.c Site Currency Conversion Settings
```
GET /app-api/<site_id>/get_currency_conversion.json
```
Returns currency-conversion display settings for the site dashboard.

Example response:
```json
{
  "site_id": 5,
  "currency_conversion": {
    "show_currency_conversion": true,
    "value_per_kwh": "0.25",
    "currency_unit": "EUR",
    "currency_symbol": "€"
  }
}
```

Observed notes:
- `value_per_kwh` was observed as a string rather than a numeric JSON value.
- `currency_symbol` is display-oriented text and should not be interpreted as a normalized ISO code.

### 2.9.3.d Requested Battery Usage Hint
```
GET /app-api/<site_id>/get_requested_battery_usage
```
Returns a battery-usage hint consumed by the site UI.

Example response:
```json
{
  "requestedBatteryUsage": null
}
```

Observed notes:
- The only observed value so far is `null`.
- Semantics remain unclear; preserve future non-null values verbatim until more captures exist.

### 2.9.3.e Site Performance Widget Flags
```
GET /app-api/<site_id>/performance_widgets
```
Returns booleans controlling which site-performance widgets the UI should render.

Example response:
```json
{
  "show_energy_performance": true,
  "show_environmental_impact": true,
  "show_consumption_offset": true,
  "show_currency_conversion": true,
  "site_id": 5
}
```

### 2.9.3.f PowerMatch UI Flags
```
GET /app-api/<site_id>/powermatch_details
```
Returns lightweight PowerMatch visibility/state flags.

Example response:
```json
{
  "enabled": true,
  "show": true
}
```

Observed notes:
- The HAR does not establish backend semantics for `enabled` versus `show`; document them as UI flags only until more evidence exists.

### 2.9.3.g Legacy Site Tariff Flags
```
GET /app-api/<site_id>/tariff.json?country=<country>
```
Returns tariff-related site configuration for the legacy app path.

Observed notes:
- The HAR observed `country=us`.
- The response body was not captured, so only the endpoint shape and query parameter are documented here.
- This appears to be separate from the newer tariff microservice endpoint documented in `2.9.8.a`.

### 2.9.3.g Site Weather

```http
GET /systems/<site_id>/weather.json?locale=<locale>
Headers:
  Accept: application/json
  Cookie: <authenticated Enlighten session cookies>
  e-auth-token: <token, when available>
  X-Requested-With: XMLHttpRequest
```

Returns the current weather associated with the Enphase site. The integration
calls this endpoint only when weather is enabled in integration options. It
creates the Home Assistant weather entity only after a successful response with
a usable current temperature; later failures remain isolated from the main site
and charger refresh.

Anonymized example response (identifier-free values are illustrative):

```json
{
  "string": "MostlyCloudy",
  "code": "cloudy",
  "temperature": {
    "value": 17,
    "min": 15,
    "max": 19,
    "display": "17°C"
  }
}
```

Observed in an Enlighten web capture on 2026-07-11. The source capture was
sanitized before documenting this endpoint: the site ID, account details,
cookies, tokens, request identifiers, client network metadata, and exact
temperature values are intentionally omitted or replaced.

### 2.9.4 System Dashboard Summary Flags
```
GET /service/system_dashboard/api_internal/cs/sites/<site_id>/summary
```
Returns high-level capability and region flags used by the system dashboard summary view.

Headers:
  Accept: application/json
  Cookie: <authenticated Enlighten session cookies>
  Referer: https://enlighten.enphaseenergy.com/app/system_dashboard/sites/<site_id>/summary

Example response (anonymized capture):
```json
{
  "is_ensemble": true,
  "is_ensemble3": true,
  "is_ensemble3_na": false,
  "is_ensemble3_row": true,
  "is_nem3": false,
  "is_dt": false,
  "currency_unit": "CUR",
  "currency_symbol": "$",
  "geo": "REGION",
  "country_code": "XX",
  "is_hems": false
}
```

Observed structure:
- The endpoint returns a flat JSON object; there is no top-level `data`, `meta`, or `error` envelope.
- `is_ensemble`, `is_ensemble3`, `is_ensemble3_na`, and `is_ensemble3_row` are site capability flags related to Ensemble / IQ Battery platform support. Preserve raw booleans because variant naming is product-specific.
- `is_nem3` appears to indicate whether the site is configured for a NEM 3 tariff/export regime. This interpretation is inferred from the field name and should be treated as provisional.
- `is_dt` is another site capability/configuration flag surfaced by the dashboard, but its exact meaning was not confirmed from this capture.
- `currency_unit`, `currency_symbol`, `geo`, and `country_code` provide region and localization metadata for downstream UI formatting.
- `is_hems` was observed on sites also exposing IQ Energy Router / heat-pump endpoints.
- `currency_*`, `geo`, and `country_code` are region-dependent.

Notes:
- Observed: the captured browser request succeeded with authenticated session cookies and did not include an `e-auth-token` header.
- The current implementation groups this route with other dashboard reads and may add `Authorization: Bearer <token>` when a bearer can be derived from cookies or stored auth state.
- The original trace contained live cookies, a site ID, account identifiers, and a client-facing IP address; those values are intentionally replaced with placeholders in this document.

### 2.9.4.a Activation Checklist
```
GET /service/system_dashboard/api_internal/cs/sites/<site_id>/updated_activation_checklist
```
Returns the commissioning and activation checklist shown in the system dashboard for battery / controller capable sites.

Example response (anonymized capture):
```json
[
  {
    "label": "IQ Battery(s) Entered",
    "done": "18/09/2025 04:27 PM TZ",
    "color": "GREEN"
  },
  {
    "label": "IQ System Controller Entered",
    "done": "18/09/2025 04:34 PM TZ",
    "color": "GREEN"
  },
  {
    "label": "Comms Kit Detected",
    "done": "18/09/2025 04:35 PM TZ",
    "color": "GREEN"
  },
  {
    "label": "Cell Modem Connectivity",
    "done": "18/09/2025 04:35 PM TZ",
    "color": "GREEN"
  },
  {
    "label": "WiFi/Ethernet Connectivity",
    "done": "18/09/2025 04:35 PM TZ",
    "color": "GREEN"
  },
  {
    "label": "IQ Battery(s) Provisioned",
    "done": "18/09/2025 04:36 PM TZ",
    "color": "GREEN"
  },
  {
    "label": "IQ System Controller Provisioned",
    "done": "18/09/2025 04:36 PM TZ",
    "color": "GREEN"
  },
  {
    "label": "Production CT Enabled",
    "done": "10/08/2022 12:42 PM TZ",
    "color": "GREEN"
  },
  {
    "label": "Consumption CT Enabled",
    "done": "10/08/2022 12:42 PM TZ",
    "color": "GREEN"
  },
  {
    "label": "Consumption CT - Load with Solar Production (Net-Consumption)",
    "done": "10/08/2022 12:42 PM TZ",
    "color": "GREEN"
  },
  {
    "label": "Battery Profile Set",
    "done": null,
    "color": "AMBER"
  },
  {
    "label": "Tariff Set",
    "done": "03/09/2025 06:32 PM TZ",
    "color": "GREEN"
  },
  {
    "label": "IQ System Controller FW Upgrade",
    "done": "22/01/2026 08:02 PM TZ",
    "color": "GREEN"
  },
  {
    "label": "IQ Battery FW Upgrade",
    "done": "21/01/2026 08:37 PM TZ",
    "color": "GREEN"
  },
  {
    "label": "Functional Validation Done",
    "done": "18/09/2025 04:44 PM TZ",
    "color": "GREEN"
  }
]
```

Observed structure:
- The endpoint returns a plain array; there is no top-level `data` envelope.
- `label` is a localized string, not a stable enum. Expect wording differences across locales and Enphase revisions.
- `done` is either `null` or a pre-formatted site-local timestamp string that already includes a timezone abbreviation.
- `color` is an uppercase status token observed as `GREEN` and `AMBER`; preserve unknown values rather than coercing them.

### 2.9.4.b System Dashboard Master Data Catalog
```
GET /service/system_dashboard/api_internal/cs/sites/<site_id>/data/master-data
```
Returns the reference catalogs used by the system dashboard UI for device pickers, parameter filters, activity-type labels, and installer/user selectors.
Unlike the runtime/status endpoints, this payload is mostly lookup metadata rather than live telemetry.

Example response (anonymized capture):
```json
{
  "devices": [
    {
      "name": "Microinverter",
      "serial_num": "INV0000000001"
    },
    {
      "name": "IQ System Controller",
      "serial_num": "SC0000000001"
    },
    {
      "name": "IQ Battery PCU",
      "serial_num": "PCU0000000001"
    },
    {
      "name": "Production Meter",
      "serial_num": "GW0000000001EIM1"
    },
    {
      "name": "Gateway",
      "serial_num": "GW0000000001"
    }
  ],
  "parameters": [
    {
      "id": "ac_frequency",
      "name": "AC Frequency"
    },
    {
      "id": "energy_consumed",
      "name": "Energy Consumed"
    },
    {
      "id": "state_of_charge",
      "name": "State of Charge"
    },
    {
      "id": "temperature",
      "name": "Temperature"
    }
  ],
  "ranges": [
    {
      "id": "today",
      "name": "Today"
    },
    {
      "id": "past_7_days",
      "name": "Past 7 Days"
    },
    {
      "id": "month_to_date",
      "name": "Month to Date"
    },
    {
      "id": "custom",
      "name": "Custom"
    }
  ],
  "activity_types": [
    {
      "id": "owner_details_entered",
      "name": "Owner Details Entered"
    },
    {
      "id": "envoy_upgrade",
      "name": "Gateway Upgrade"
    },
    {
      "id": "evse_maintenance_success",
      "name": "Evse maintenance success"
    },
    {
      "id": "FW upgrade complete",
      "name": "Fw upgrade complete"
    }
  ],
  "users": [
    {
      "id": "installer.one@example.invalid",
      "name": "installer.one@example.invalid"
    },
    {
      "id": "installer.two@example.invalid",
      "name": "installer.two@example.invalid"
    }
  ]
}
```

Observed structure:
- The response is a plain object with five top-level arrays: `devices`, `parameters`, `ranges`, `activity_types`, and `users`.
- `devices` is a flat site inventory keyed by display `name` and `serial_num`. Observed names included microinverters, IQ Battery PCUs, IQ Batteries, IQ System Controller, meters, and the gateway.
- `parameters` exposes stable metric/filter IDs such as `ac_frequency`, `energy_consumed`, `power`, `state_of_charge`, and `temperature`.
- `ranges` enumerates the built-in dashboard date filters. Observed values were `today`, `past_7_days`, `month_to_date`, and `custom`.
- `activity_types` is a large catalog of commissioning, provisioning, maintenance, and firmware event identifiers mapped to human-readable labels.
- `users` contained email-address identifiers in the captured response; treat this array as personally identifiable data and anonymize or redact it in logs and documentation.

Notes:
- The browser capture used an authenticated same-origin Enlighten session with XSRF/session cookies; no bearer token was observed on this request.
- `activity_types.id` values are not normalized. The sample contained mixed casing, embedded spaces, and duplicate-looking variants, so clients should preserve the raw string rather than coercing it.
- Meter `serial_num` values may derive from the gateway serial with suffixes such as `EIM1` and `EIM2`.
- Because the payload is catalog-like and changed infrequently in the capture, it is a better candidate for caching than the live status endpoints.

Runtime behavior:
- The integration caches this catalog for six hours and only requests parameter IDs that the site advertises.
- User and activity catalogs are not retained in runtime diagnostics. This avoids persisting email-address identifiers or unrelated commissioning history.

### 2.9.4.b.1 Per-Gateway Microinverter Inventory
```
GET /service/system_dashboard/api_internal/dashboard/sites/<site_id>/envoy_inverters?serial_number=<gateway_sn>
```
Returns a flattened `data[]` list with `id`, `name`, `serial_number`, `status`, `sub_status`, `type`, and `parent_id`. The runtime batches one request per discovered gateway, caches the result for six hours, and uses it only to enrich or fill gaps in the legacy microinverter inventory. Failures preserve the last successful optional payload and do not fail the coordinator refresh.

### 2.9.4.b.2 Device Parameter Columns
```
GET /service/system_dashboard/api_internal/cs/sites/<site_id>/data/columns?serial_num=<gateway_sn>&type=device_level
```
Returns `columns[]` metadata including `name`, `header`, `attribute_name`, visibility/sort flags, and filter type. Runtime retains only the non-sensitive column/attribute names; gateway and device identifiers are not copied into diagnostics.

### 2.9.4.b.3 Bulk Device Parameter Readings
```
GET /service/system_dashboard/api_internal/cs/sites/<site_id>/data/parameter-view?serial_numbers=<csv>&per_page=500&page=1&range=today&parameter_id=<id>&start_date=&end_date=&sort_by_date=desc
```
Returns paginated device readings for one parameter. The response includes
`intervals[]`, per-device `columns[]`, paging fields, and range metadata. The
integration passes every active microinverter serial in one CSV request per
supported parameter, uses the site master-data catalog to avoid unsupported
probes, and runs up to two parameter requests concurrently with an independent
15-second timeout per request. Supported values are normalized to power, AC/DC
voltage/current, AC frequency, temperature, signal strength, and firmware when
advertised and present.

Successful parameters retain independent freshness timestamps. Partial batches
remain healthy while they publish useful telemetry and any reused parameter values
are younger than 30 minutes. A batch is degraded when it has no usable current or
cached result, when a reused parameter becomes stale, or after three consecutive
full-batch failures. Sanitized failure reasons and UTC retry times remain visible
through the Service Status diagnostic attributes even while a fresh-cache retry is
classified as healthy.

### 2.9.4.c System Dashboard Devices Table
```
GET /service/system_dashboard/api_internal/cs/sites/<site_id>/devices?range=today&start_date=<iso8601>&end_date=<iso8601>&filter_columns=<csv>&serial_numbers=<csv>&type=table&page=<page>&per_page=<n>
```
Returns the paginated device inventory table shown in the commissioning/system-dashboard UI.

Observed query parameters:
- `range`: observed as `today`.
- `start_date`, `end_date`: site-local ISO-8601 timestamps with offset.
- `filter_columns`: comma-separated column list controlling which fields the table returns.
- `serial_numbers`: comma-separated inventory scope. The capture included true serials plus synthetic group tokens such as `PcuDevice`.
- `type`: observed as `table`.
- `page`, `per_page`: pagination controls; captured values were `1..3` and `15`.
- `serial_number`, `device_type`, `hw_version`, `sw_version`, `last_report`: optional UI filter inputs; when unused, the web UI still sent them as empty strings on later pages.

Example response (anonymized):
```json
{
  "total_devices": 39,
  "page": "2",
  "per_page": "15",
  "devices": [
    {
      "device_type": "Gateway",
      "serial_number": "GW0000000000",
      "device_link": "https://enlighten.example/systems/<site_id>/envoys/200001",
      "device_status": "Normal",
      "sw_version": "D8.3.5228.250724 (abcdef)",
      "hw_version": "-",
      "created_at": "2026/03/01 12:04:44 +1100 (TZ)",
      "soc": "N/A",
      "delta_soc": "N/A",
      "plc_comm": 5,
      "profile": "Regional Grid Profile",
      "last_report": "2026/03/09 16:59:08 +1100 (TZ)",
      "time_since_last_report": "1 minute",
      "operation_mode": "N/A",
      "enc_serial_number": null,
      "enc_serial_number_link": null,
      "dmir_version": "-",
      "devimg_version": "500-00005-r01-v01.02.537 (abcdef)",
      "essimg_version": "500-00020-r01-v31.44.11 (abcdef)",
      "app_version": "500-00002-r01-v08.03.5228 (abcdef)",
      "ibl_fw_version": "N/A",
      "swift_asic_fw": "N/A"
    },
    {
      "device_type": "IQ Battery",
      "serial_number": "BAT0000000001",
      "device_link": "https://enlighten.example/systems/<site_id>/ac_batteries/300001",
      "device_status": "Normal",
      "sw_version": "522-00002-01-v3.0.8557_rel/31.44",
      "hw_version": "892-00030-r83",
      "created_at": "2025/09/18 16:35:47 +1000 (TZ)",
      "soc": 98,
      "delta_soc": 0,
      "plc_comm": 5,
      "rssi_dbm": 0,
      "profile": "N/A",
      "last_report": "2026/03/09 16:53:26 +1100 (TZ)",
      "time_since_last_report": "9 minutes",
      "operation_mode": "Multi-mode On Grid, Discharging",
      "enc_serial_number": null,
      "enc_serial_number_link": null,
      "dmir_version": "546-00002-01-v01",
      "devimg_version": null,
      "essimg_version": null,
      "app_version": "3.0.8557_rel/31.44",
      "ibl_fw_version": "3.1.813-abcdef",
      "swift_asic_fw": "001.002.1.7.2"
    },
    {
      "device_type": "IQ Battery PCU",
      "serial_number": "PCU0000000001",
      "device_link": "https://enlighten.example/systems/<site_id>/inverters/310001",
      "device_status": "Normal",
      "sw_version": "521-00008-r00-v4.63.1-D63",
      "hw_version": "880-01691-r44",
      "created_at": "2025/09/18 16:56:35 +1000 (TZ)",
      "soc": "N/A",
      "delta_soc": "N/A",
      "plc_comm": "N/A",
      "profile": "N/A",
      "last_report": "2026/03/09 16:56:26 +1100 (TZ)",
      "time_since_last_report": "6 minutes",
      "operation_mode": "N/A",
      "enc_serial_number": "BAT0000000001",
      "enc_serial_number_link": "https://enlighten.example/systems/<site_id>/ac_batteries/300001",
      "dmir_version": "549-00057-r00-v4.63.1-D63",
      "devimg_version": null,
      "essimg_version": null,
      "app_version": null,
      "ibl_fw_version": "N/A",
      "swift_asic_fw": "N/A"
    },
    {
      "device_type": "IQ System Controller E3 Control Board",
      "serial_number": "CTRLBOARD0001",
      "device_link": "https://enlighten.example/systems/<site_id>/ac_batteries/310000",
      "device_status": "Normal",
      "sw_version": "522-00003-01-v2.7.7054_rel/31.44",
      "hw_version": "880-01323-r04",
      "created_at": "2025/09/18 16:56:34 +1000 (TZ)",
      "soc": "N/A",
      "delta_soc": "N/A",
      "plc_comm": "N/A",
      "profile": null,
      "last_report": "-",
      "time_since_last_report": "-",
      "operation_mode": "N/A",
      "enc_serial_number": null,
      "enc_serial_number_link": null,
      "dmir_version": null,
      "devimg_version": null,
      "essimg_version": null,
      "app_version": null,
      "ibl_fw_version": "N/A",
      "swift_asic_fw": "N/A"
    }
  ],
  "csv_link": "https://enlighten.example/admin/sites/<site_id>/site_devices_csv?...",
  "show_feoc_dom": false
}
```

Observed structure:
- The payload is a single page, with `total_devices` describing the full filtered result count.
- `devices[]` is heterogeneous. The row schema varies by `device_type`; battery rows expose numeric `soc`/`delta_soc`, PCU and BMCC rows add `enc_serial_number`, and controller daughterboards can report `"-"` for `last_report` and `time_since_last_report`.
- Observed `device_type` values included `Gateway`, `Cellular Modem`, `Microinverter`, `Production Meter`, `Consumption Meter`, `IQ Battery`, `IQ Battery PCU`, `IQ Battery BMCC`, `IQ System Controller`, `IQ System Controller E3 Control Board`, and `IQ System Controller Startup PCBA`.
- `device_status`, `profile`, `operation_mode`, and `time_since_last_report` are display-oriented strings and may be localized.
- Missing values use a mix of `null`, `"N/A"`, and `"-"` depending on the field and device family.
- `device_link`, `enc_serial_number_link`, and `csv_link` are direct dashboard URLs. They should be treated as sensitive because they embed site-specific identifiers.
- `show_feoc_dom` was observed as a boolean feature flag (`false` in the capture); semantics are still unclear.

### 2.9.5 System Dashboard Status Overview
```
GET /service/system_dashboard/api_internal/dashboard/sites/<site_id>/status
```
Returns the compact site overview shown in the system dashboard header.

Example response (anonymized):
```json
{
  "name": "Example Account",
  "status": "normal",
  "statusText": "Normal",
  "battery_mode": "Self - Consumption",
  "soc": "97%",
  "storm_guard": "Disabled",
  "storage_setpoint": 22,
  "pv_setpoint": 100,
  "reserved_soc": 20,
  "backup_type": "Partial Home Backup",
  "timezone": "Region/City",
  "isIqcp": false,
  "items": [
    {
      "name": "Admin View",
      "link": "https://enlighten.example/admin/sites/<site_id>"
    },
    {
      "name": "MyEnlighten View",
      "link": "https://enlighten.example/web/<site_id>?v=3.4.0"
    },
    {
      "name": "Enlighten Mobile",
      "link": "https://enlighten.example/mobile/<site_id>?v=3.4.0"
    },
    {
      "name": "Enlighten Manager",
      "link": "https://enlighten.example/systems/<site_id>"
    }
  ]
}
```

Observed structure:
- The response is a flat object with no `meta`/`data` envelope.
- `name` contains account-holder text and should be treated as sensitive.
- `status` is a normalized lowercase state token, while `statusText` is the localized display label shown in the UI.
- `battery_mode`, `storm_guard`, and `backup_type` are localized display strings, not stable enums; preserve spacing and punctuation as returned.
- `soc` is a string percentage, not a numeric ratio.
- `storage_setpoint`, `pv_setpoint`, and `reserved_soc` are integer tuning values. `storage_setpoint` has been observed as both positive and negative; semantics are still unclear.
- `items[]` contains dashboard shortcut labels and fully qualified site URLs for other Enlighten surfaces.
- Observed request auth was session-cookie based from the dashboard web app; no explicit `e-auth-token` header was present in the captured request.
- The current implementation uses the same dashboard-read header builder for this family and may attach `Authorization: Bearer <token>` opportunistically.

### 2.9.5.a System Dashboard Range Testing
```
GET /service/system_dashboard/api_internal/dashboard/sites/<site_id>/range_testing
```
Returns the current dashboard-visible range-test state for the site.

Example response (anonymized):
```json
{
  "tested_on": null,
  "is_success": false,
  "range_test": []
}
```

Observed structure:
- No query parameters or request body were observed; the endpoint used the same authenticated dashboard session headers as other `api_internal/dashboard` calls.
- `tested_on` is nullable. The captured response returned `null`, so the exact timestamp format for completed tests is not yet confirmed.
- `is_success` is a boolean flag indicating the recorded outcome of the range test.
- `range_test` is an array. The captured response returned an empty array when no test result was available.

Inference:
- Based on the path and field names, this appears to expose a site-level range-test or commissioning validation result used by the system dashboard UI.
- The concrete schema of items inside `range_test` remains unknown because the observed capture contained no entries.

### 2.9.6 System Dashboard Device Tree
```
GET /service/system_dashboard/api_internal/dashboard/sites/<site_id>/devices-tree
Fallback: GET /pv/systems/<site_id>/system_dashboard/devices-tree
```
Returns a flattened parent/child topology for the site and attached devices.

Example response (anonymized):
```json
{
  "data": [
    {
      "id": 1234567,
      "name": "System",
      "serial_number": 1234567,
      "status": "normal",
      "type": "Site"
    },
    {
      "id": 210000001,
      "name": "Gateway",
      "serial_number": "GW0000000000",
      "status": "Normal",
      "type": "Envoy",
      "parent_id": 1234567
    },
    {
      "id": 210000002,
      "name": "Cellular Modem",
      "serial_number": "MD0000000000",
      "status": "activated",
      "type": "CellularModem",
      "parent_id": 210000001
    },
    {
      "id": 210000101,
      "name": "Microinverter",
      "serial_number": "MI0000000001",
      "status": "Normal",
      "sub_status": "",
      "type": "PcuDevice",
      "parent_id": 210000001
    },
    {
      "id": 210000102,
      "name": "Microinverter",
      "serial_number": "MI0000000002",
      "status": "Normal",
      "sub_status": "",
      "type": "PcuDevice",
      "parent_id": 210000001
    },
    {
      "id": 210000201,
      "name": "Production Meter",
      "serial_number": "GW0000000000EIM1",
      "status": "Normal",
      "sub_status": "",
      "type": "EimDevice",
      "parent_id": 210000001
    },
    {
      "id": 210000202,
      "name": "Consumption Meter",
      "serial_number": "GW0000000000EIM2",
      "status": "Normal",
      "sub_status": "",
      "type": "EimDevice",
      "parent_id": 210000001
    },
    {
      "id": 210000301,
      "name": "IQ System Controller",
      "serial_number": "SC0000000000",
      "status": "Normal",
      "sub_status": "",
      "type": "Enpower",
      "parent_id": 210000001
    },
    {
      "id": 210000302,
      "name": "E3 Control Board",
      "serial_number": "SCB000000001",
      "status": "Normal",
      "sub_status": "",
      "type": "EnpowerE3ControlBoard",
      "parent_id": 210000301
    },
    {
      "id": 210000303,
      "name": "IQ System Controller Startup PCBA",
      "serial_number": "SCP000000001",
      "status": "Normal",
      "sub_status": "",
      "type": "EnpowerStartupPcba",
      "parent_id": 210000301
    },
    {
      "id": 210000401,
      "name": "IQ Battery",
      "serial_number": "BAT000000001",
      "status": "Normal",
      "sub_status": "",
      "type": "Encharge",
      "parent_id": 210000001
    },
    {
      "id": 210000402,
      "name": "IQ Battery BMCC",
      "serial_number": "BATB00000001",
      "status": "Normal",
      "sub_status": "",
      "type": "EnchargeE3ControlBoard",
      "parent_id": 210000401
    },
    {
      "id": 210000403,
      "name": "IQ Battery PCU",
      "serial_number": "BATP00000001",
      "status": "Normal",
      "sub_status": "",
      "type": "EncPcuDevice",
      "parent_id": 210000401
    },
    {
      "id": 210000404,
      "name": "IQ Battery PCU",
      "serial_number": "BATP00000002",
      "status": "Normal",
      "sub_status": "",
      "type": "EncPcuDevice",
      "parent_id": 210000401
    }
  ]
}
```

Observed structure:
- The payload is a flat array; hierarchy is reconstructed via `parent_id`.
- The site root record has no `parent_id`; child records reference either the site root or another device node.
- `serial_number` is usually a string, but the site root was observed echoing the numeric site identifier.
- `status` and `sub_status` are human-readable strings. The same payload used both `normal` and `Normal`, so casing should not be treated as stable.
- Repeated hardware such as microinverters and battery PCUs appears as one record per physical unit, often with generic `name` values.

Observed fields:
- `id`: numeric device or site node identifier. This is the value referenced by `parent_id`.
- `name`: dashboard label for the node. It is often generic (`"Microinverter"`, `"IQ Battery PCU"`) rather than user-customized.
- `serial_number`: device serial or synthetic meter identifier. Meter entries may suffix the gateway serial with `EIM1` or `EIM2`.
- `status`: display-oriented device state. Preserve raw text rather than coercing it to an enum.
- `sub_status`: optional secondary status string; often empty for healthy devices.
- `type`: backend device-class key used by system-dashboard pages and the related `devices_details?type=<type>` endpoint.
- `parent_id`: foreign key to the containing node. Missing on the root `Site` record.

Observed `type` values from the capture:
- `Site`: root system node.
- `Envoy`: gateway / communications hub.
- `CellularModem`: optional modem attached to the gateway.
- `PcuDevice`: microinverter node.
- `EimDevice`: production or consumption meter.
- `Enpower`: IQ System Controller.
- `EnpowerE3ControlBoard`: system-controller control board child.
- `EnpowerStartupPcba`: system-controller startup board child.
- `Encharge`: IQ Battery node.
- `EnchargeE3ControlBoard`: battery BMCC/control-board child.
- `EncPcuDevice`: battery PCU child.

### 2.9.7 Standing Alarms
```
GET /service/system_dashboard/api_internal/dashboard/sites/<site_id>/alarms?range=today&filter_columns=<...>&type=table&page=<page>&per_page=<n>
```
Returns the table backing the "Standing Alarms" view in the system dashboard.

Example response (anonymized):
```json
{
  "total": 2,
  "alarms": [
    {
      "id": "<site_id>.440.1770000000000",
      "severity": 4,
      "type": "Gateway",
      "serial_num": "GW0000000000",
      "device_link": "https://enlighten.example/systems/<site_id>/envoys/200001",
      "description": "Aggregate battery low state of charge",
      "first_set": "2026/03/08 09:41:28 +0100 (CET)",
      "force_clearable": true
    }
  ],
  "page": "1",
  "per_page": "200",
  "csv_link": "https://enlighten.example/admin/sites/<site_id>/standing_alarms?...",
  "disable_force_clear": false,
  "availablePages": {
    "next_pointers": {},
    "last_offset": 0
  }
}
```

Observed structure:
- `first_set` is already formatted as a site-local string rather than epoch time.
- `serial_num` may contain a true serial number or aggregate text such as `"2 Devices"`.
- `force_clearable` and `disable_force_clear` appear to indicate whether manual clear actions are allowed.

Runtime use:
- Standing alarms are the authoritative source for alarm severity and Home
  Assistant Repairs.
- The Problem entity is active when at least one standing alarm exists or the
  event feed explicitly classifies an active event as high impact.
- Alarm IDs, serials, links, descriptions, and CSV links are discarded. Repair
  IDs use a truncated SHA-256 fingerprint, and missing alarms retain the existing
  bounded grace period before their Repairs are removed.

### 2.9.8 System Dashboard Device Details by Type
```
GET /service/system_dashboard/api_internal/dashboard/sites/<site_id>/devices_details?type=envoys
GET /service/system_dashboard/api_internal/dashboard/sites/<site_id>/devices_details?type=encharges
GET /service/system_dashboard/api_internal/dashboard/sites/<site_id>/devices_details?type=enpowers
GET /service/system_dashboard/api_internal/dashboard/sites/<site_id>/devices_details?type=meters
GET /service/system_dashboard/api_internal/dashboard/sites/<site_id>/devices_details?type=modems
GET /service/system_dashboard/api_internal/dashboard/sites/<site_id>/devices_details?type=inverters
Fallback: GET /pv/systems/<site_id>/system_dashboard/devices_details?type=<type>
```
Returns per-family detail cards for the system dashboard device modal.

Observed query parameter:
- `type` selects the device family. Observed values: `envoys`, `encharges`, `enpowers`, `meters`, `modems`, `inverters`.

Example response for `type=envoys` (anonymized):
```json
{
  "envoys": [
    {
      "id": 200001,
      "name": "IQ Gateway",
      "serial_number": "GW0000000000",
      "device_link": "https://enlighten.example/systems/<site_id>/envoys/200001",
      "sku_id": "SC100G-M000ROW",
      "connected": true,
      "status": "normal",
      "statusText": "Normal",
      "ip": "192.0.2.10",
      "ap_mode": true,
      "envoy_sw_version": "D8.3.5167.250527",
      "last_report": "2026/03/08 09:59:12 +0100 (CET)",
      "connection_details": {
        "cellular": false,
        "wifi": null,
        "ethernet": true,
        "interface_ip": {
          "wifi": null,
          "ethernet": "192.0.2.10"
        }
      }
    }
  ]
}
```

Example response for `type=encharges` (anonymized):
```json
{
  "encharges": [
    {
      "id": 300001,
      "name": "IQ Battery 5P",
      "serial_number": "BAT0000000001",
      "device_link": "https://enlighten.example/systems/<site_id>/ac_batteries/300001",
      "sku_id": "B05-T02-ROW00-1-2",
      "channel_type": "IQ Battery",
      "phase": "L1(A)",
      "status": "normal",
      "statusText": "Normal",
      "last_report": "2026/03/09 16:38:49 +1100 (AEDT)",
      "sw_version": "546-00002-01-v01",
      "total": 2,
      "not_reporting": 0,
      "rssi_subghz": 0,
      "rssi_24ghz": 5,
      "rssi_dbm": 0,
      "soc": "98%",
      "operation_mode": "Multi-mode On Grid, Discharging",
      "led_status": 13,
      "app_version": "3.0.8557_rel/31.44",
      "alarm_id": null
    }
  ]
}
```

Example response for `type=enpowers` (anonymized):
```json
{
  "enpowers": [
    {
      "id": 310001,
      "name": "IQ System Controller",
      "serial_number": "CTRL000000001",
      "device_link": "https://enlighten.example/systems/<site_id>/ac_batteries/310001",
      "sku_id": "SC100G-M230ROW",
      "channel_type": "IQ System Controller",
      "status": "normal",
      "statusText": "Normal",
      "last_report": "2026/03/09 16:42:43 +1100 (AEDT)",
      "sw_version": "546-00003-01-v01",
      "rssi_subghz": 0,
      "rssi_24ghz": 5,
      "rssi_dbm": 0,
      "operation_mode": "Grid Connected - IQ Batteries Connected",
      "app_version": "2.7.7054_rel/31.44",
      "earth": "TN-C-S"
    }
  ]
}
```

Example response for `type=meters` (anonymized):
```json
{
  "meters": [
    {
      "id": 320001,
      "name": "IQ Gateway",
      "serial_number": "GW0000000000EIM1",
      "device_link": "https://enlighten.example/systems/<site_id>/meters/320001",
      "sku_id": null,
      "channel_type": "Production Meter",
      "status": "normal",
      "statusText": "Normal",
      "last_report": "2026/03/09 16:40:00 +1100 (AEDT)",
      "meter_state": "Enabled",
      "config_type": "Production",
      "meter_type": "Production"
    },
    {
      "id": 320002,
      "name": "IQ Gateway",
      "serial_number": "GW0000000000EIM2",
      "device_link": "https://enlighten.example/systems/<site_id>/meters/320002",
      "sku_id": null,
      "channel_type": "Consumption Meter",
      "status": "normal",
      "statusText": "Normal",
      "last_report": "2026/03/09 16:40:00 +1100 (AEDT)",
      "meter_state": "Enabled",
      "config_type": "Net (Load with Solar)",
      "meter_type": "Consumption"
    }
  ]
}
```

Example response for `type=modems` (anonymized):
```json
{
  "modems": [
    {
      "id": 330001,
      "serial_number": "89010000000000000000",
      "device_status": "Normal",
      "status": "activated",
      "statusText": "Activated",
      "last_report": "2026/03/09 16:44:25 +1100 (AEDT)",
      "sw_version": "EG25GLGDR07A03M1G_01.200.01.200",
      "signal": 4,
      "rssi": 18,
      "bit_error_rate": "99 (Unknown)",
      "plan_end": "2030/09/17",
      "part_number_with_sku": "865-02038-r03 (CELLMODEM-07-INT-05-CM)",
      "device_link": "https://enlighten.example/systems/<site_id>/devices?status=active#cellular_modems"
    }
  ]
}
```

Example response for `type=inverters` (anonymized):
```json
{
  "inverters": {
    "total": 16,
    "not_reporting": 0,
    "plc_comm": 5,
    "items": [
      {
        "name": "IQ7A Microinverters",
        "count": 16
      }
    ],
    "device_link": "https://enlighten.example/systems/<site_id>/devices?status=active"
  }
}
```

Observed structure:
- The top-level key matches the requested `type`.
- `envoys`, `encharges`, `enpowers`, `meters`, and `modems` return arrays of device cards.
- `inverters` returns a summary object with `total`, `not_reporting`, `plc_comm`, `items[]`, and `device_link` rather than per-device rows.
- Fields are family-specific and often localized (`statusText`, `channel_type`, meter labels, modem plan text).
- `encharges` and `enpowers` both expose RF/link-health fields (`rssi_subghz`, `rssi_24ghz`, `rssi_dbm`) plus family-specific operating-state strings.
- `enpowers[].operation_mode` can describe System Controller operating state, but the live web grid-status chip is driven by the site live-stream `meters.gridRelay` value instead.
- `meters` expose configuration labels (`config_type`, `meter_type`) rather than firmware/network details.
- `modems` expose provisioning state (`status`, `plan_end`, `part_number_with_sku`) instead of the gateway-style network payload.
- These endpoints expose sensitive infrastructure details such as IP addresses, MAC-derived identifiers, and direct dashboard links; redact aggressively before sharing traces.

### 2.9.8.a Site Tariff and Billing Configuration
```
GET /service/tariff/tariff-ms/systems/<site_id>/tariff?include-site-details=true
PUT /service/tariff/tariff-ms/systems/<site_id>/tariff?user-id=<user_id>
GET /service/tariff/tariff-ms/systems/<site_id>/billing-details
POST /service/tariff/tariff-ms/systems/<site_id>/billing-details?date=<YYYY-MM-DD>
GET /pv/aws_sigv4/tariff_settings_response_stream?serial_number=<gateway_sn>
PUT /service/evse_scheduler/api/v1/siteConfig/<site_id>/tariff_change
Headers:
  Accept: application/json, text/javascript, */*; q=0.01
  Cookie: <authenticated Enlighten session cookies>
  e-auth-token: <session_id>
  Requestid: <uuid>
  Username: <user_id>
  X-Requested-With: XMLHttpRequest
  x-xsrf-token: <BP-XSRF-Token>  # observed on tariff GET/PUT, authorizer, and writes
```
Returns and updates the tariff profile used for site cost calculations and tariff-aware battery / EV charging UI flows.
The browser flow also reads and writes billing-cycle metadata, fetches a short-lived MQTT response-stream authorizer for tariff settings,
and notifies the EVSE scheduler service after billing or tariff changes.

Implementation notes:
- The active runtime reads a bundled billing/import/export snapshot and exposes billing-cycle plus current import/export tariff sensors.
- Rate writes update located `rate` or `offPeak` values in a copied full tariff payload, call `PUT /tariff` once per batch, then best-effort notify `/siteConfig/<site_id>/tariff_change`.
- Structural writes can replace the full tariff payload or replace the `purchase` and/or `buyback` branch on the latest payload before the single `PUT /tariff` call. This supports flat, time-of-use, and tiered structures, including seasons, weekday/weekend groups, periods, tiers, off-peak baselines, and export plans.
- Guided structural service fields can build common import/export branches from tariff type, variation, flat-rate, period-row, tier-row, off-peak baseline, and export-plan inputs. The advanced object fields remain available for exact payload replacement.
- Billing writes call `POST /billing-details` with `anyBillPeriodStartDate`, `billingFrequency`, and `billingIntervalValue`, then best-effort notify `/siteConfig/<site_id>/tariff_change`.
- The `update_tariff` service can combine billing, structural, and rate writes, but the Enphase endpoints are not transactional; a remote failure after one endpoint succeeds can leave a partial cloud-side update.
- The tariff-settings MQTT response stream is a documented browser flow, but it is not part of the active runtime write path.

#### Billing Details
```
GET /service/tariff/tariff-ms/systems/<site_id>/billing-details
POST /service/tariff/tariff-ms/systems/<site_id>/billing-details?date=<YYYY-MM-DD>
```

Example `GET` response:
```json
{
  "id": 1234567,
  "billingFrequency": "MONTH",
  "anyBillPeriodStartDate": "2025-08-18",
  "billingIntervalValue": 1
}
```

Example `POST` request body:
```json
{
  "anyBillPeriodStartDate": "2025-08-17",
  "billingFrequency": "MONTH",
  "billingIntervalValue": 1
}
```

Example `POST` response:
```json
{
  "id": 1234567,
  "billingFrequency": "MONTH",
  "anyBillPeriodStartDate": "2025-08-17",
  "billingIntervalValue": 1
}
```

Example day-based `POST` request body:
```json
{
  "anyBillPeriodStartDate": "2025-08-17",
  "billingFrequency": "DAY",
  "billingIntervalValue": 30
}
```

Example day-based `POST` response:
```json
{
  "id": 1234567,
  "billingFrequency": "DAY",
  "anyBillPeriodStartDate": "2025-08-17",
  "billingIntervalValue": 30
}
```

Observed billing fields:
- `anyBillPeriodStartDate`: ISO local date for any known bill-period start. The UI renders this as "Bill Start Date".
- `billingFrequency`: interval unit; observed payload values are `MONTH` and `DAY`.
- `billingIntervalValue`: interval count; observed values include `1` for month-based billing and `30` for day-based billing.
- The UI constrains Bill Start Date to no earlier than the year the site was commissioned and no later than 12 months in the future.
- The UI supports bill frequency units of months or days. Month intervals can be configured from `1` through `24`; day intervals can be configured from `1` through `100`.
- The `POST` query parameter `date` was the browser's current local date (`2026-04-26` in the capture), not the bill start date.

#### Tariff Read
```
GET /service/tariff/tariff-ms/systems/<site_id>/tariff?include-site-details=true
```

Example response (anonymized; representative values only):
```json
{
  "site_id": 1234567,
  "country": "XX",
  "currency": "$",
  "zipcode": "9999",
  "hasAcb": false,
  "chargeFromGrid": true,
  "chargeBeginTime": 120,
  "chargeEndTime": 300,
  "showBatteryConfig": true,
  "hideChargeFromGrid": true,
  "supports_mqtt": true,
  "calibrationProgress": false,
  "purchase": {
    "typeKind": "single",
    "typeId": "tou",
    "hasNetMetering": false,
    "source": "manual",
    "seasons": [
      {
        "id": "default",
        "startMonth": "1",
        "endMonth": "12",
        "days": [
          {
            "id": "week",
            "days": [1, 2, 3, 4, 5, 6, 7],
            "periods": [
              {
                "id": "off-peak",
                "startTime": "",
                "endTime": "",
                "rate": "0.18",
                "type": "off-peak",
                "rateComponents": []
              },
              {
                "id": "peak-1",
                "startTime": "840",
                "endTime": "1260",
                "rate": "0.31",
                "type": "peak",
                "rateComponents": []
              }
            ],
            "updatedValue": "",
            "must_charge_start": "0",
            "must_charge_duration": "0",
            "must_charge_mode": "CP"
          }
        ]
      }
    ]
  },
  "buyback": {
    "typeKind": "single",
    "typeId": "tou",
    "source": "netFit",
    "seasons": [
      {
        "id": "default",
        "startMonth": "1",
        "endMonth": "12",
        "days": [
          {
            "id": "week",
            "days": [1, 2, 3, 4, 5, 6, 7],
            "periods": [
              {
                "id": "off-peak",
                "startTime": "",
                "endTime": "",
                "rate": "0.02",
                "type": "off-peak",
                "rateComponents": []
              },
              {
                "id": "peak-1",
                "startTime": "960",
                "endTime": "1320",
                "rate": "0.06",
                "type": "peak",
                "rateComponents": []
              }
            ],
            "updatedValue": ""
          }
        ]
      }
    ],
    "exportPlan": "netFit"
  },
  "nemVersion": "",
  "installDate": "2024-01-15",
  "showDTQuestion": false,
  "dtCustomChargeEnabled": true
}
```

#### Dated Tariff Rates Read
```
GET /service/tariff/tariff-ms/systems/<site_id>/tariffs?rateType=BUYBACK&date=<YYYY-MM-DD>&includeUtility=
```

Returns date-specific tariff rate windows for sites where the main tariff configuration can include an empty `buyback.seasons[]` branch.
The active runtime uses this endpoint as a read-only fallback for current export price sensors; these rates are not exposed as editable tariff number entities.

Observed query parameters:
- `rateType`: observed as `BUYBACK` for export compensation rates.
- `date`: local date whose rates should be returned, formatted as `YYYY-MM-DD`.
- `includeUtility`: observed with an empty value.

Observed response shape:
```json
{
  "type": "tariff-rates",
  "timestamp": "2026-05-01T16:25:02.851080797Z",
  "data": {
    "tariff-version-id": "0123456789abcdef01234567",
    "tariff-type": "static",
    "currency": "$",
    "timezone": "US/Pacific",
    "siteDetails": {
      "currency": "$",
      "timezone": "US/Pacific",
      "importPlanType": "NEM3",
      "exportPlanType": "NEM3",
      "exportApplicabilityYear": 2026,
      "interconnectionApplicationDate": "2024-01-15T00:00:00Z",
      "installDate": "2024-01-15",
      "lastExportDate": "2033-09-11T07:00:00Z"
    },
    "buyback": [
      {
        "start": 0,
        "end": 59,
        "rate": 0.0789
      },
      {
        "start": 60,
        "end": 119,
        "rate": 0.0733
      },
      {
        "start": 1380,
        "end": 1439,
        "rate": 0.0764
      }
    ]
  }
}
```

Observed notes:
- `data.buyback[]` contains rate windows for the requested date. `start` and `end` are minute offsets from local midnight; observed rows used inclusive end minutes, for example `0..59` for 00:00-01:00 and `1380..1439` for 23:00-00:00.
- `data.timezone` and `data.siteDetails.timezone` identify the site timezone for interpreting those minute offsets.
- The endpoint can provide export rates even when `GET /tariff?include-site-details=true` returns `buyback.seasons: []`.
- The runtime currently uses this endpoint only for read-only `Current Export Rate` selection when the main tariff payload has no usable export rate specs.
- This endpoint is not expected to be available for every account or region. The runtime tracks it as its own optional endpoint family (`tariff_dated_rates`) so unsupported or degraded responses back off independently from the main tariff configuration endpoint.

#### Tariff Update
```
PUT /service/tariff/tariff-ms/systems/<site_id>/tariff?user-id=<user_id>
```
The web UI sends a full replacement tariff object rather than a patch. The request body preserves the top-level site metadata
from the latest read response and replaces the selected `purchase` and/or `buyback` tariff branch. The captured UI included
`"status": "success"` in the request body; preserve unknown top-level fields if round-tripping the payload.

Example response:
```json
{
  "message": "success"
}
```

Example update body for a single-season time-of-use import tariff and time-varying net feed-in export tariff:
```json
{
  "site_id": 1234567,
  "country": "AU",
  "currency": "$",
  "zipcode": "9999",
  "hasAcb": false,
  "chargeFromGrid": true,
  "chargeBeginTime": 120,
  "chargeEndTime": 300,
  "showBatteryConfig": true,
  "hideChargeFromGrid": true,
  "supports_mqtt": true,
  "calibrationProgress": false,
  "purchase": {
    "typeKind": "single",
    "typeId": "tou",
    "hasNetMetering": false,
    "source": "manual",
    "seasons": [
      {
        "id": "default",
        "startMonth": "1",
        "endMonth": "12",
        "days": [
          {
            "id": "week",
            "days": [1, 2, 3, 4, 5, 6, 7],
            "periods": [
              {
                "id": "off-peak",
                "startTime": "",
                "endTime": "",
                "rate": "0.1",
                "type": "off-peak",
                "rateComponents": []
              },
              {
                "id": "peak-1",
                "startTime": "900",
                "endTime": "1260",
                "rate": "0.3",
                "type": "peak",
                "rateComponents": []
              }
            ],
            "updatedValue": "",
            "must_charge_start": "0",
            "must_charge_duration": "0",
            "must_charge_mode": "CP"
          }
        ]
      }
    ]
  },
  "buyback": {
    "typeKind": "single",
    "typeId": "tou",
    "source": "netFit",
    "seasons": [
      {
        "id": "default",
        "startMonth": "1",
        "endMonth": "12",
        "days": [
          {
            "id": "week",
            "days": [1, 2, 3, 4, 5, 6, 7],
            "periods": [
              {
                "id": "off-peak",
                "startTime": "",
                "endTime": "",
                "rate": "0.002",
                "type": "off-peak",
                "rateComponents": []
              },
              {
                "id": "peak-1",
                "startTime": "1080",
                "endTime": "1380",
                "rate": "0.05",
                "type": "peak",
                "rateComponents": []
              }
            ],
            "updatedValue": ""
          }
        ]
      }
    ],
    "exportPlan": "netFit"
  },
  "nemVersion": "",
  "installDate": "2024-01-15",
  "showDTQuestion": false,
  "dtCustomChargeEnabled": true,
  "status": "success"
}
```

Example `purchase` branch for seasonal time-of-use import rates:
```json
{
  "typeKind": "seasonal",
  "typeId": "tou",
  "hasNetMetering": false,
  "source": "manual",
  "seasons": [
    {
      "id": "summer",
      "startMonth": "12",
      "endMonth": "5",
      "days": [
        {
          "id": "week",
          "days": [1, 2, 3, 4, 5, 6, 7],
          "periods": [
            {"id": "off-peak", "rate": "0.02", "type": "off-peak", "startTime": "", "endTime": ""},
            {"id": "peak-1", "rate": "0.03", "type": "peak", "startTime": 540, "endTime": 660}
          ]
        }
      ]
    },
    {
      "id": "winter",
      "startMonth": "6",
      "endMonth": "11",
      "days": [
        {
          "id": "week",
          "days": [1, 2, 3, 4, 5, 6, 7],
          "periods": [
            {"id": "off-peak", "rate": "0.03", "type": "off-peak", "startTime": "", "endTime": ""},
            {"id": "peak-1", "rate": "0.04", "type": "peak", "startTime": 600, "endTime": 1380}
          ]
        }
      ]
    }
  ]
}
```

Example `purchase` branches for flat import rates:
```json
{
  "typeKind": "single",
  "typeId": "flat",
  "hasNetMetering": false,
  "source": "manual",
  "seasons": [
    {
      "id": "default",
      "startMonth": "1",
      "endMonth": "12",
      "days": [
        {
          "id": "week",
          "days": [1, 2, 3, 4, 5, 6, 7],
          "periods": [{"id": "off-peak", "rate": "0.03", "type": "off-peak", "startTime": "", "endTime": ""}]
        }
      ]
    }
  ]
}
```

```json
{
  "typeKind": "seasonal",
  "typeId": "flat",
  "hasNetMetering": false,
  "source": "manual",
  "seasons": [
    {
      "id": "summer",
      "startMonth": "12",
      "endMonth": "5",
      "days": [
        {
          "id": "week",
          "days": [1, 2, 3, 4, 5, 6, 7],
          "periods": [{"id": "off-peak", "rate": "0.02", "type": "off-peak", "startTime": "", "endTime": ""}]
        }
      ]
    },
    {
      "id": "winter",
      "startMonth": "6",
      "endMonth": "11",
      "days": [
        {
          "id": "week",
          "days": [1, 2, 3, 4, 5, 6, 7],
          "periods": [{"id": "off-peak", "rate": "0.03", "type": "off-peak", "startTime": "", "endTime": ""}]
        }
      ]
    }
  ]
}
```

Example `purchase` branch for weekend-specific time-of-use import rates:
```json
{
  "typeKind": "weekends",
  "typeId": "tou",
  "hasNetMetering": false,
  "source": "manual",
  "seasons": [
    {
      "id": "default",
      "startMonth": "1",
      "endMonth": "12",
      "days": [
        {
          "id": "weekdays",
          "days": [1, 2, 3, 4, 5],
          "periods": [
            {"id": "off-peak", "rate": "0.03", "type": "off-peak", "startTime": "", "endTime": ""},
            {"id": "peak-1", "rate": "0.04", "type": "peak", "startTime": 600, "endTime": 660}
          ]
        },
        {
          "id": "weekend",
          "days": [6, 7],
          "periods": [
            {"id": "off-peak", "rate": "0.04", "type": "off-peak", "startTime": "", "endTime": ""},
            {"id": "peak-1", "rate": "0.05", "type": "peak", "startTime": 600, "endTime": 660}
          ]
        }
      ]
    }
  ]
}
```

Example `purchase` branch for seasonal and weekend-specific time-of-use import rates:
```json
{
  "typeKind": "seasonal-and-weekends",
  "typeId": "tou",
  "hasNetMetering": false,
  "source": "manual",
  "seasons": [
    {
      "id": "summer",
      "startMonth": "12",
      "endMonth": "5",
      "days": [
        {
          "id": "weekdays",
          "days": [1, 2, 3, 4, 5],
          "periods": [
            {"id": "off-peak", "rate": "0.02", "type": "off-peak", "startTime": "", "endTime": ""},
            {"id": "peak-1", "rate": "0.03", "type": "peak", "startTime": 600, "endTime": 660}
          ]
        },
        {
          "id": "weekend",
          "days": [6, 7],
          "periods": [
            {"id": "off-peak", "rate": "0.03", "type": "off-peak", "startTime": "", "endTime": ""},
            {"id": "peak-1", "rate": "0.04", "type": "peak", "startTime": 600, "endTime": 660}
          ]
        }
      ]
    },
    {
      "id": "winter",
      "startMonth": "6",
      "endMonth": "11",
      "days": [
        {
          "id": "weekdays",
          "days": [1, 2, 3, 4, 5],
          "periods": [
            {"id": "off-peak", "rate": "0.05", "type": "off-peak", "startTime": "", "endTime": ""},
            {"id": "peak-1", "rate": "0.06", "type": "peak", "startTime": 600, "endTime": 660}
          ]
        },
        {
          "id": "weekend",
          "days": [6, 7],
          "periods": [
            {"id": "off-peak", "rate": "0.07", "type": "off-peak", "startTime": "", "endTime": ""},
            {"id": "peak-1", "rate": "0.08", "type": "peak", "startTime": 600, "endTime": 660}
          ]
        }
      ]
    }
  ]
}
```

Example `purchase` branch for single-season tiered import rates:
```json
{
  "typeKind": "single",
  "typeId": "tiered",
  "hasNetMetering": false,
  "source": "manual",
  "seasons": [
    {
      "id": "default",
      "startMonth": "1",
      "endMonth": "12",
      "tiers": [
        {"id": "tier-1", "rate": "0.06", "startValue": "10", "endValue": "20"},
        {"id": "tier-2", "rate": "0.10", "startValue": "20", "endValue": -1}
      ],
      "offPeak": "0.04"
    }
  ]
}
```

Example `purchase` branch for seasonal tiered import rates:
```json
{
  "typeKind": "seasonal",
  "typeId": "tiered",
  "hasNetMetering": false,
  "source": "manual",
  "seasons": [
    {
      "id": "summer",
      "startMonth": "12",
      "endMonth": "5",
      "tiers": [
        {"id": "tier-1", "rate": "0.05", "startValue": "5", "endValue": "10"},
        {"id": "tier-2", "rate": "0.08", "startValue": "10", "endValue": -1}
      ],
      "offPeak": "0.03"
    },
    {
      "id": "winter",
      "startMonth": "6",
      "endMonth": "11",
      "tiers": [
        {"id": "tier-1", "rate": "0.09", "startValue": "5", "endValue": "10"},
        {"id": "tier-2", "rate": "0.10", "startValue": "10", "endValue": -1}
      ],
      "offPeak": "0.04"
    }
  ]
}
```

Example export tariff branches:
```json
{
  "buyback": {
    "typeKind": "single",
    "typeId": "flat",
    "hasNetMetering": false,
    "source": "netFit",
    "seasons": [
      {
        "id": "default",
        "startMonth": "1",
        "endMonth": "12",
        "days": [
          {
            "id": "week",
            "days": [1, 2, 3, 4, 5, 6, 7],
            "periods": [{"id": "off-peak", "rate": "0.01", "type": "off-peak", "startTime": "", "endTime": ""}]
          }
        ]
      }
    ],
    "exportPlan": "netFit"
  }
}
```

```json
{
  "buyback": {
    "typeKind": "single",
    "typeId": "tou",
    "hasNetMetering": false,
    "source": "netFit",
    "seasons": [
      {
        "id": "default",
        "startMonth": "1",
        "endMonth": "12",
        "days": [
          {
            "id": "week",
            "days": [1, 2, 3, 4, 5, 6, 7],
            "periods": [
              {"id": "off-peak", "rate": "0.01", "type": "off-peak", "startTime": "", "endTime": ""},
              {"id": "peak-1", "rate": "0.2", "type": "peak", "startTime": 1020, "endTime": 1260}
            ]
          }
        ]
      }
    ],
    "exportPlan": "netFit"
  }
}
```

```json
{
  "buyback": {
    "typeKind": "single",
    "typeId": "tou",
    "hasNetMetering": false,
    "source": "manual",
    "seasons": [
      {
        "id": "default",
        "startMonth": "1",
        "endMonth": "12",
        "days": [
          {
            "id": "week",
            "days": [1, 2, 3, 4, 5, 6, 7],
            "periods": [
              {"id": "off-peak", "rate": "0.1", "type": "off-peak", "startTime": "", "endTime": ""},
              {"id": "peak-1", "rate": "0.3", "type": "peak", "startTime": 900, "endTime": 1260}
            ]
          }
        ]
      }
    ],
    "exportPlan": "nem"
  }
}
```

```json
{
  "buyback": {
    "typeKind": "single",
    "typeId": "flat",
    "source": "grossFit",
    "seasons": [
      {
        "id": "default",
        "startMonth": "1",
        "endMonth": "12",
        "days": [
          {
            "id": "week",
            "days": [1, 2, 3, 4, 5, 6, 7],
            "periods": [{"id": "off-peak", "rate": "0.06", "type": "off-peak", "startTime": "", "endTime": ""}]
          }
        ]
      }
    ],
    "exportPlan": "grossFit"
  }
}
```

#### Tariff Settings Response Stream
```
GET /pv/aws_sigv4/tariff_settings_response_stream?serial_number=<gateway_sn>
```

Example response:
```json
{
  "aws_iot_endpoint": "a1b2c3d4e5f6g7-ats.iot.us-east-1.amazonaws.com",
  "aws_authorizer": "aws-lambda-authoriser-prod",
  "aws_token_key": "enph_token",
  "aws_token_value": "<session_id>",
  "aws_digest": "<base64_signature>",
  "duration": 900,
  "topic": "v1/response-stream/<stream_id>"
}
```

Observed notes:
- The query parameter is named `serial_number` and used the gateway/IQ Gateway serial number in the browser capture.
- The response shape is similar to the Live Status and BatteryConfig MQTT authorizers, but the topic prefix was `v1/response-stream/`.
- The UI did not expose a separate "start stream" action; the capture only confirms the authorizer request and response.

#### EVSE Scheduler Tariff-Change Notification
```
PUT /service/evse_scheduler/api/v1/siteConfig/<site_id>/tariff_change
```

Example request body:
```json
null
```

Example response:
```json
{
  "meta": {
    "serverTimeStamp": "2026-04-26T03:17:37.096+00:00"
  },
  "data": "Request Accepted",
  "error": {}
}
```

Observed notes:
- The browser called this endpoint after tariff and billing-detail changes.
- Success status was `202 Accepted`.
- This appears to notify the EVSE scheduler/site-config service that tariff-aware schedule calculations may need to refresh; it does not carry the tariff body itself.

Observed request fields:
- Path parameter `site_id`: numeric site identifier embedded in the `/systems/<site_id>/...` path.
- Query parameter `include-site-details`: observed as `true`; when set, the response includes top-level site metadata such as `country`, `currency`, `zipcode`, `installDate`, and battery-related capability flags.
- Query parameter `user-id`: observed on tariff `PUT`; value is the authenticated user id.
- Query parameter `serial_number`: observed on the tariff settings response stream; use the gateway serial number and redact it from shared traces.
- Write requests used `Content-Type: application/json`, `Origin: https://enlighten.enphaseenergy.com`, `Username`, `Requestid`, `e-auth-token`, and `x-xsrf-token`.

Observed tariff payload structure:
- The tariff read response is a flat JSON object with no `data` / `meta` wrapper; tariff writes respond with `{"message": "success"}`.
- `purchase` describes import tariffs and `buyback` describes export compensation. Time-of-use and flat tariffs use the nested `seasons[] -> days[] -> periods[]` shape; tiered tariffs use `seasons[] -> tiers[]` plus an `offPeak` baseline field.
- `typeKind` controls season/day variation. Observed values: `single`, `seasonal`, `weekends`, `seasonal-and-weekends`.
- `typeId` controls rate structure. Observed values: `flat`, `tou`, `tiered`.
- `purchase.source` was `manual` in all observed updates. `buyback.source` was observed as `manual`, `netFit`, and `grossFit`; `buyback.exportPlan` was observed as `nem`, `netFit`, and `grossFit`.
- `periods[].startTime` and `periods[].endTime` are minutes after midnight. The read payload returned strings such as `"900"`; some write payloads emitted numbers such as `540`. Empty strings were observed for all-day/default periods.
- The UI rejects overlapping peak/mid-peak time ranges within the same day or season block and displays the validation message "This period is overlapping with one of the other periods"; the `UPDATE` action remains disabled until the overlap is fixed.
- `periods[].rate` is returned as a string, not a numeric JSON value.
- `days[].id` labels include `week`, `weekdays`, and `weekend`; `days[].days` uses numeric weekday values. Observed mappings were `[1, 2, 3, 4, 5, 6, 7]` for every day, `[1, 2, 3, 4, 5]` for weekdays, and `[6, 7]` for weekends.
- `purchase.days[].must_charge_start`, `must_charge_duration`, and `must_charge_mode` appear to be EV/battery charge-policy hints attached to the import tariff definition.
- Tiered `tiers[].startValue` was observed as a string. `tiers[].endValue` may be a string for bounded tiers or `-1` for the final unbounded tier.
- Tiered `offPeak` appears to be the baseline import rate before the configured usage threshold.
- Top-level `chargeBeginTime` / `chargeEndTime` are integer minutes after midnight and align with the charge-from-grid window exposed by the BatteryConfig APIs.
- Top-level flags such as `showBatteryConfig`, `hideChargeFromGrid`, `supports_mqtt`, `calibrationProgress`, and `dtCustomChargeEnabled` appear to gate related UI behavior.

Notes:
- The observed browser request included live JWT-bearing cookies, XSRF tokens, session IDs, user IDs, site identifiers, gateway serial numbers, postcode data, account-linked metadata, and AWS IoT authorizer signatures. Those values are intentionally replaced here with placeholders or representative examples.
- `source` values under `purchase` / `buyback` are backend-origin labels (`manual`, `netFit` in the capture) and should be preserved verbatim until more variants are observed.

### 2.10 Homeowner Events History
```
GET /service/events-platform-service/v1.0/<site_id>/events/homeowner?next=<cursor>&page_size=<n>&locale=<locale>
Headers:
  Accept: application/json
  Content-Type: application/json
  Cookie: ...; XSRF-TOKEN=<token>; ...
  e-auth-token: <token>
  X-Requested-With: XMLHttpRequest
```
Returns event feed rows shown in "Events History" with cursor pagination.

Example response shape (anonymized):
```json
{
  "events": [
    {
      "id": 123456789,
      "status": "Info",
      "type": "IQ EV Charger",
      "description": "Charging started on IQ EV Charger (SNo. EV0000000000).",
      "standing": false,
      "event_start_date": 1770000100,
      "event_clear_date": 1770000100,
      "devices_impacted": ["IQ EV Charger (SNo. EV0000000000)"],
      "serial_num": "EV0000000000",
      "recommended_action": "No action is required.",
      "emu_event_id": -1,
      "event_key": "evse_start_charging",
      "event_date": 1770000100,
      "event_type_id": 795,
      "message_params": "Charge Level = --, Mode = Charge Now"
    }
  ],
  "site_timezone": "Region/City",
  "next": "1769999999:EV0000000000:795:-1",
  "page_size": 30,
  "csv_link": "https://enlighten.example/service/events-platform-service/v1.0/<site_id>/events/homeowner?export=true&next=start&page_size=5000"
}
```
Observed structure:
- `events[]` includes Info and Closed rows, with optional `cta`, `message_params`, and `description_key`.
- Date fields (`event_start_date`, `event_clear_date`, `event_date`) are epoch seconds and render in site-local timezone.
- Pagination uses the opaque `next` token returned by each page.
- `description`, `devices_impacted`, and `serial_num` embed serial identifiers; redact these before logging/sharing traces.
- Heat-pump / HEMS captures also showed stable `event_key` values for SG Ready mode transitions and connectivity problems, including:
  - `hems_sgready_mode_changed_to_2` for "normal mode"
  - `hems_sgready_mode_changed_to_3` for "recommended consumption"
  - `hems_sgready_relay_offline` when the heat pump / SG Ready relay stopped communicating
  - `hems_energy_meter_offline` when the HEMS energy meter stopped communicating
  - `hems_iqer_MQTT_offline` when the IQ Energy Router stopped forwarding connected-device data to Enphase Cloud

Inference:
- The event-history feed provides stable SG Ready transition keys even when the human-readable descriptions are localized, so it is useful for documenting `MODE_2` versus `MODE_3` semantics without relying on translated text.

Implementation note:
- The integration exposes this feed through the site-level `System Event History`
  calendar on the Enphase Cloud device. Calendar range requests paginate from the
  newest row until the requested start date or a 2,000-row safety bound is reached.
  Raw IDs, serials, impacted-device lists, message parameters, CSV links, and
  continuation cursors are discarded after descriptions and recommended actions
  are sanitized. The separate System Dashboard table below remains authoritative
  for Active System Events and standing-alarm monitoring.

### 2.10.1 System Dashboard Active Events

```http
GET /service/system_dashboard/api_internal/cs/sites/<site_id>/events
    ?range=today
    &cassandra_toggle=false
    &filter_columns=serial_number,device_type,event_date,cleared_date,event_type,event_state,details,updated_at,alarm_id
    &serial_numbers=
    &type=table
    &event_state=default
    &page=1
    &per_page=200
```

The runtime uses the authenticated System Dashboard read headers and treats the
route as optional. Successful responses are cached for five minutes; endpoint
failures retain the last valid event state and use bounded backoff.

Top-level response fields:

```text
total, page, per_page, events, event_types, event_states,
csv_link, event_severities, availablePages
```

Event rows may contain:

```text
id, serial_number, device_link, event_date, cleared_date,
updated_at, device_type, event_type, event_state, alarm_id, details
```

Runtime safety rules:

- Cleared/closed/resolved rows are not active.
- Ordinary stateful rows do not set the Problem entity. Only standing alarms or
  rows explicitly classified as `error`, `critical`, `fatal`, `emergency`, or
  `severe` set Problem.
- Up to 20 active rows are exposed as an identifier-free entity attribute with
  event type, device type, state, event date, and update time. The attribute is
  excluded from Recorder history.
- Event IDs, alarm IDs, device serials, device links, details, and CSV links are
  discarded.
- Repairs are synchronized from Standing Alarms rather than event-history rows. A
  transient endpoint failure never clears a last-known active repair.

### 2.11 Battery Backup History
```
GET /app-api/<site_id>/battery_backup_history.json
Headers:
  Accept: */*
  Cookie: ...; XSRF-TOKEN=<token>; ...
  e-auth-token: <token>
  X-Requested-With: XMLHttpRequest
```
Returns backup outage history consumed by the "Backup History" table.

Example response (anonymized):
```json
{
  "total_records": 4,
  "total_backup": 307,
  "histories": [
    {
      "start_time": "2025-10-17T14:38:30+11:00",
      "duration": 121
    },
    {
      "start_time": "2025-10-16T18:30:09+11:00",
      "duration": 74
    }
  ]
}
```
Observed structure:
- `total_records` is the count of backup events.
- `total_backup` is cumulative backup duration in seconds.
- `histories[]` entries contain ISO8601 `start_time` and `duration` (seconds); `end_time` can be derived as `start_time + duration`.

### 2.C Site Grid Control

### 2.12 Grid Control Check (Eligibility / Guardrails)
```
GET /app-api/<site_id>/grid_control_check.json
Headers:
  Accept: */*
  Cookie: ...; XSRF-TOKEN=<token>; ...
  e-auth-token: <token>
  X-Requested-With: XMLHttpRequest
```
Returns booleans describing off-grid control eligibility and in-progress state.

Example response (anonymized):
```json
{
  "disableGridControl": false,
  "activeDownload": false,
  "sunlightBackupSystemCheck": false,
  "gridOutageCheck": false,
  "userInitiatedGridToggle": false
}
```
Observed structure:
- `disableGridControl=true` indicates a grid-mode toggle is blocked.
- `activeDownload`/`sunlightBackupSystemCheck`/`gridOutageCheck` are guard conditions surfaced by the backend.
- `userInitiatedGridToggle` indicates whether a toggle workflow is already in progress.
- This endpoint does **not** provide the current steady-state grid mode (`On Grid`/`Off Grid`).

### 2.12.1 Grid Toggle OTP (Send / Resend)
```
GET /app-api/<site_id>/grid_toggle_otp.json
Headers:
  Accept: */*
  Cookie: ...; XSRF-TOKEN=<token>; ...
  e-auth-token: <token>
  X-Requested-With: XMLHttpRequest
```
Triggers delivery of the 4-digit OTP used to authorize a manual grid toggle.

Example response (anonymized):
```json
{
  "success": "email sent successfully"
}
```
Observed behavior:
- Called after the user confirms either `Go Off Grid` or `Go On Grid`.
- Also called again when the user taps `Resend` in the OTP modal.

### 2.12.2 Grid Toggle OTP Verification
```
POST /app-api/grid_toggle_otp.json
Headers:
  Accept: application/json, text/javascript, */*; q=0.01
  Content-Type: application/x-www-form-urlencoded; charset=UTF-8
  Cookie: ...; XSRF-TOKEN=<token>; ...
  e-auth-token: <token>
  Origin: https://enlighten.enphaseenergy.com
  X-Requested-With: XMLHttpRequest
Body (form):
  otp=<4_digit_code>
  site_id=<site_id>
```
Validates the OTP before the grid relay command is accepted.

Example response (anonymized):
```json
{
  "valid": true
}
```
Observed behavior:
- `valid=true` is required before `/pv/settings/grid_state.json` is invoked.

### 2.12.3 Grid State Change Command
```
POST /pv/settings/grid_state.json
Headers:
  Accept: application/json, text/javascript, */*; q=0.01
  Content-Type: application/x-www-form-urlencoded; charset=UTF-8
  Cookie: ...; XSRF-TOKEN=<token>; ...
  e-auth-token: <token>
  Origin: https://enlighten.enphaseenergy.com
  X-Requested-With: XMLHttpRequest
Body (form):
  envoy_serial_number=<envoy_serial>
  state=<state_code>
```
Queues the actual grid relay transition after OTP validation.

State mapping observed in captures:
- `state=1` requests transition to off-grid mode.
- `state=2` requests transition to on-grid mode.

Example response (anonymized):
```json
{
  "request_id": "req_xxxxxxxxxxxxxxxxxxxxxxxx",
  "context_ids": [
    1700000000000000
  ]
}
```

### 2.12.4 Grid Change Audit Log
```
POST /pv/settings/log_grid_change.json
Headers:
  Accept: application/json, text/javascript, */*; q=0.01
  Content-Type: application/x-www-form-urlencoded; charset=UTF-8
  Cookie: ...; XSRF-TOKEN=<token>; ...
  e-auth-token: <token>
  Origin: https://enlighten.enphaseenergy.com
  X-Requested-With: XMLHttpRequest
Body (form):
  envoy_serial_number=<envoy_serial>
  old_state=<relay_state_before>
  new_state=<relay_state_after>
```
Records relay-state transitions after the grid toggle request.

Example response (anonymized):
```json
{
  "status": "Grid Change Logged",
  "old_state": "OPER_RELAY_CLOSED",
  "new_state": "OPER_RELAY_OFFGRID_AC_GRID_PRESENT"
}
```
Observed state pairs:
- Off-grid transition: `OPER_RELAY_CLOSED` -> `OPER_RELAY_OFFGRID_AC_GRID_PRESENT`
- On-grid transition: `OPER_RELAY_OFFGRID_READY_FOR_RESYNC_CMD` -> `OPER_RELAY_CLOSED`

### 2.12.5 Off-Grid Status Context
```
GET /app-api/<site_id>/off_grid_due_to_grid_outage
Headers:
  Accept: */*
  Cookie: ...; XSRF-TOKEN=<token>; ...
  e-auth-token: <token>
  X-Requested-With: XMLHttpRequest
```
Returns grid-outage state and regional contact metadata.

Example response (anonymized):
```json
{
  "continent_code": "XX",
  "country_code": "YY",
  "isd_code": "+00",
  "phone": "<support_phone>",
  "is_sunlight_backup": false,
  "is_grid_outage": false,
  "show_grid_connect": true,
  "has_battery": true
}
```
`is_grid_outage=true` identifies an outage-driven off-grid state. When `show_grid_connect=true`, the Enphase UI is offering a reconnect action, which indicates the site is currently off grid even when `is_grid_outage=false`. This endpoint is useful fallback context; prefer the live-stream `meters.gridRelay` value when available.

### 2.12.6 Grid Toggle Request Sequence
Both directions use a confirmation + OTP gate before the relay command is sent.

Off-grid (`System is On Grid` -> `System is Off Grid`):
1. User taps `Go Off Grid` toggle and confirms warning dialog.
2. Client calls `GET /app-api/<site_id>/grid_toggle_otp.json`.
3. User enters OTP; client calls `POST /app-api/grid_toggle_otp.json`.
4. If `{"valid": true}`, client calls `POST /pv/settings/grid_state.json` with `state=1`.
5. Backend transition proceeds after the command is accepted.
6. Client logs transition via `POST /pv/settings/log_grid_change.json`.

On-grid (`System is Off Grid` -> `System is On Grid`):
1. User taps `Go On Grid` toggle and confirms reconnect dialog.
2. Client calls `GET /app-api/<site_id>/grid_toggle_otp.json`.
3. User enters OTP; client calls `POST /app-api/grid_toggle_otp.json`.
4. If `{"valid": true}`, client calls `POST /pv/settings/grid_state.json` with `state=2`.
5. Backend transition proceeds after the command is accepted.
6. Client may query `GET /app-api/<site_id>/off_grid_due_to_grid_outage` and logs transition via `POST /pv/settings/log_grid_change.json`.

### 2.D Microinverter APIs

### 2.13 Microinverter Inventory (Legacy Site View)
```
GET /app-api/<site_id>/inverters.json?limit=<n>&offset=<n>&search=<query>
Headers:
  Accept: */*
  Cookie: ...; XSRF-TOKEN=<token>; ...
  e-auth-token: <token>
  X-Requested-With: XMLHttpRequest
```
Returns the microinverter list for the site.

Example response shape (anonymized):
```json
{
  "total": 16,
  "not_reporting": 0,
  "inverters": [
    {
      "name": "IQ7A",
      "array_name": "North",
      "serial_number": "12XXXXXXXXXX",
      "sku_id": "IQ7A-72-E-ACM-INT",
      "status": "normal",
      "statusText": "Normal",
      "part_num": "800-01714-r02",
      "sku": 3733,
      "last_report": 1770623834,
      "fw1": "520-00082-r01-v04.30.32",
      "fw2": "540-00169-r01-v04.30.12",
      "warranty_end_date": "2032-08-10"
    },
    {
      "name": "IQ7A",
      "array_name": "West",
      "serial_number": "12XXXXXXXXYY",
      "sku_id": "IQ7A-72-E-ACM-INT",
      "status": "normal",
      "statusText": "Normal",
      "part_num": "800-01714-r02",
      "sku": 3733,
      "last_report": 1770624076,
      "fw1": "520-00082-r01-v04.30.32",
      "fw2": "540-00169-r01-v04.30.12",
      "warranty_end_date": "2032-08-10"
    }
  ],
  "error_count": 0,
  "warning_count": 0,
  "normal_count": 16,
  "panel_info": {
    "pv_module_manufacturer": null,
    "model_name": null,
    "stc_rating": null
  }
}
```
Observed structure:
- Pagination uses `limit` and `offset`; `search` filters by serial/model text and can be blank.
- `total` is the full match count, independent of current page size.
- `inverters[]` card fields include model (`name`), array grouping (`array_name`), serial, firmware (`fw1`/`fw2`), and warranty date.
- Status rollups are provided as counters (`error_count`, `warning_count`, `normal_count`) plus `not_reporting`.
- `last_report` is epoch seconds.
- The observed capture contained only `total`, `not_reporting`, and `inverters`; the aggregate `*_count` fields were absent, so clients should treat them as optional.

Observed property values from the capture:
- `name`: `IQ8AC`, `IQ8HC`
- `array_name`: localized free-form roof/array labels were observed; redact exact labels in published captures
- `status` / `statusText`: `normal` / `Normal`
- `sku_id`: `IQ8AC-72-M-INT`, `IQ8HC-72-M-INT`
- `part_num`: `800-01395-r03`, `800-01391-r03`
- `fw1`: `521-00005-r06-v08.13.01`
- `fw2`: `549-00071-r01-v08.13.01`, `549-00047-r01-v08.13.01`

### 2.14 Inverter Production by Date Range
```
GET /systems/<site_id>/inverter_data_x/energy.json?start_date=<YYYY-MM-DD>&end_date=<YYYY-MM-DD>
Headers:
  Accept: application/json, text/javascript, */*; q=0.01
  Cookie: ...; XSRF-TOKEN=<token>; ...
  e-auth-token: <token>
  X-Requested-With: XMLHttpRequest
```
Returns per-inverter production totals for the requested date window. The response is keyed by inverter ID.

Example response (single-day range, anonymized):
```json
{
  "production": {
    "67XXXXXXXX13": 556,
    "67XXXXXXXX14": 536,
    "67XXXXXXXX15": 543,
    "67XXXXXXXX16": 545
  },
  "start_date": "2026-02-09",
  "end_date": "2026-02-09"
}
```

Example response (lifetime range, anonymized):
```json
{
  "production": {
    "67XXXXXXXX13": 1884125,
    "67XXXXXXXX14": 1848279,
    "67XXXXXXXX15": 2092602,
    "67XXXXXXXX16": 2104793
  },
  "start_date": "2022-08-10",
  "end_date": "2026-02-09"
}
```
Observed structure:
- `start_date` and `end_date` are inclusive date bounds in `YYYY-MM-DD`.
- `production` is a dictionary of `<inverter_id> -> <energy_value>` for the selected window.
- Inverter keys are stable numeric IDs represented as strings in JSON.
- Values are energy totals in Wh for the requested period (single-day windows produce small daily totals; long windows return cumulative totals).

### 2.15 Inverter Status Map (ID to Serial/Device Mapping)
```
GET /systems/<site_id>/inverter_status_x.json
Headers:
  Accept: application/json, text/javascript, */*; q=0.01
  Cookie: ...; XSRF-TOKEN=<token>; ...
  e-auth-token: <token>
  X-Requested-With: XMLHttpRequest
```
Returns a device status map keyed by inverter ID. This endpoint provides the join between `inverter_data_x.production` keys and physical inverter serial numbers.

Example response shape (anonymized):
```json
{
  "67XXXXXXXX13": {
    "serialNum": "12XXXXXXXX62",
    "statusCode": "normal",
    "status": "Normal",
    "deviceId": 62XXXX42,
    "issi": { "sig_str": 0, "level": 0 },
    "rssi": { "sig_str": 0, "level": 0 },
    "emu_version": "8.3.5232",
    "show_sig_str": false,
    "type": "IQ7A"
  },
  "67XXXXXXXX14": {
    "serialNum": "12XXXXXXXX44",
    "statusCode": "normal",
    "status": "Normal",
    "deviceId": 62XXXX43,
    "issi": { "sig_str": 0, "level": 0 },
    "rssi": { "sig_str": 0, "level": 0 },
    "emu_version": "8.3.5232",
    "show_sig_str": false,
    "type": "IQ7A"
  }
}
```
Observed structure:
- Top-level object keys are inverter IDs and align with keys in `inverter_data_x.production`.
- Each entry includes `serialNum` and `deviceId`, allowing deterministic joins to `/app-api/<site_id>/inverters.json`.
- Payload may include non-microinverter device types on mixed systems (for example battery PCU entries); filter by serial/type when building microinverter entities.

Observed property values from the capture:
- `statusCode` / `status`: `normal` / `Normal`
- `emu_version`: `8.3.5167`
- `show_sig_str`: `true`
- `type`: `IQ8AC`, `IQ8HC`
- `issi.level`: `0`; observed `issi.sig_str` values included `52`, `54`, `64`
- `rssi.level`: `4`; observed `rssi.sig_str` values included `94`, `96`

### 2.15.1 Site Array Layout
```
GET /systems/<site_id>/site_array_layout_x
Headers:
  Accept: application/json, text/javascript, */*; q=0.01
  Cookie: <authenticated Enlighten session cookies>
  X-Requested-With: XMLHttpRequest
```
Returns the microinverter-layout geometry used by the Enlighten "Arrays" / Jellyfish view.

Example response shape:
```json
{
  "system_id": 1234567,
  "rotation": 0,
  "dimensions": {
    "x_min": 31,
    "x_max": 1031,
    "y_min": 1175,
    "y_max": 1776
  },
  "arrays": [
    {
      "array_id": 9000001,
      "label": "Array A",
      "x": 531,
      "y": 1475,
      "azimuth": 180,
      "modules": [
        {
          "module_id": 9100001,
          "rotation": 0,
          "x": 450,
          "y": 201,
          "inverter": {
            "inverter_id": 9200001,
            "serial_num": "12XXXXXXXXXX"
          }
        }
      ]
    }
  ],
  "haiku": "<display_text>",
  "has_iq8d": false
}
```

Observed structure:
- `dimensions` defines the canvas bounds for the site layout.
- `arrays[]` carries array-level position/orientation metadata plus `modules[]`.
- Each module record includes both layout coordinates and the joined inverter reference (`inverter_id`, `serial_num`).
- The observed capture exposed one array with 24 modules, `rotation=0`, `azimuth=180`, and `has_iq8d=false`.
- `haiku` is decorative display text rather than telemetry.

### 2.15.2 Jellyfish Initializer (HTML/JS Bootstrap)
```
GET /systems/<site_id>/jellyfish_initializer?range=<range>&view=<view>
Headers:
  Accept: text/html, */*; q=0.01
  Cookie: <authenticated Enlighten session cookies>
  X-Requested-With: XMLHttpRequest
```
Returns an HTML/JavaScript bootstrap snippet rather than JSON. The page initializes the client-side `JellyfishController` with endpoint paths, time ranges, localization strings, and view flags.

Observed bootstrap fragment:
```javascript
new e.JellyfishController({
  mode: "standard",
  ab_gating: true,
  dataMode: "inverter",
  seeModuleData: true,
  metered_view: true,
  view: "energy",
  alarms: "subStatusFlag",
  systemId: 1234567,
  energy: {
    sitePath: "/systems/1234567/energy",
    inverterPath: "/systems/1234567/inverter_data_x/energy.json",
    startDate: "2026-03-28",
    endDate: "2026-03-28"
  },
  power: {
    sitePath: "/systems/1234567/power_time_series?all_production_sources=true",
    inverterPath: "/systems/1234567/inverter_data_x/time_series.json",
    startDate: "2026-03-22",
    endDate: "2026-03-28"
  },
  acVoltage: {
    inverterPath: "/systems/1234567/inverter_data_x/time_series.json?stat=ACV"
  },
  acFrequency: {
    inverterPath: "/systems/1234567/inverter_data_x/time_series.json?stat=ACHZ"
  },
  dcVoltage: {
    inverterPath: "/systems/1234567/inverter_data_x/time_series.json?stat=DCV"
  },
  dcCurrent: {
    inverterPath: "/systems/1234567/inverter_data_x/time_series.json?stat=DCA"
  },
  temperature: {
    inverterPath: "/systems/1234567/inverter_data_x/time_series.json?stat=TMPI"
  },
  statusPath: "/systems/1234567/inverter_status_x.json",
  layout: "/systems/1234567/site_array_layout_x",
  timezone: "Region/City"
});
```

Observed behavior:
- The response is an executable HTML fragment intended for same-origin browser use, not a stable public JSON API.
- The initializer advertises the related microinverter endpoints and the stat query names `ACV`, `ACHZ`, `DCV`, `DCA`, and `TMPI`.
- The observed capture used `range=today` and `view=energy_production`; the exported browser text showed `amp;view` in the query-parameter dump, which is an HTML-escaping artifact rather than a separate API parameter.

### 2.E Site Battery Runtime Status

### 2.16 Battery Status (Site Battery Card)
```
GET /pv/settings/<site_id>/battery_status.json
Headers:
  Accept: */*
  Cookie: <authenticated Enlighten web session cookies>
  e-auth-token: <session token>
  X-Requested-With: XMLHttpRequest
```
Returns the battery card payload used in Enlighten web/app for site-level and per-battery SoC, power, and status details.
The raw web capture also included live cookies, XSRF tokens, a request ID, and browser metadata; those are omitted here because they are account-specific and not required to describe the schema.

Example response (anonymized):
```json
{
  "current_charge": "48%",
  "available_energy": 4.75,
  "max_capacity": 10,
  "available_power": 7.68,
  "max_power": 7.68,
  "total_micros": 12,
  "show_battery_banner": false,
  "active_micros": 12,
  "inactive_micros": 0,
  "inactive_micros_sn": [],
  "included_count": 2,
  "excluded_count": 0,
  "storages": [
    {
      "id": 100000001,
      "serial_number": "BT0000000001",
      "current_charge": "48%",
      "available_energy": 2.40,
      "max_capacity": 5,
      "led_status": 17,
      "excluded": false,
      "error_code": null,
      "error_text": null,
      "available_power": 3.84,
      "max_power": 3.84,
      "total_micros": 6,
      "active_micros": 6,
      "inactive_micros": 0,
      "inactive_micros_sn": [],
      "event_start_date": null,
      "event_description": null,
      "event_recommendation": null,
      "statusText": "Normal",
      "status": "normal",
      "last_report": 1770000001,
      "cycle_count": 115,
      "battery_mode": "Self-Consumption",
      "rated_power": 3840,
      "battery_phase_count": 3,
      "is_flex_phase": true,
      "battery_soh": "100%"
    },
    {
      "id": 100000002,
      "serial_number": "BT0000000002",
      "current_charge": "47%",
      "available_energy": 2.35,
      "max_capacity": 5,
      "led_status": 17,
      "excluded": false,
      "error_code": null,
      "error_text": null,
      "available_power": 3.84,
      "max_power": 3.84,
      "total_micros": 6,
      "active_micros": 6,
      "inactive_micros": 0,
      "inactive_micros_sn": [],
      "event_start_date": null,
      "event_description": null,
      "event_recommendation": null,
      "statusText": "Normal",
      "status": "normal",
      "last_report": 1770000011,
      "cycle_count": 115,
      "battery_mode": "Self-Consumption",
      "rated_power": 3840,
      "battery_phase_count": 3,
      "is_flex_phase": true,
      "battery_soh": "100%"
    }
  ]
}
```
Observed structure:
- Top-level metrics summarize combined battery behavior (`current_charge`, energy/power totals, microinverter counts).
- `show_battery_banner` is a UI hint flag; observed value so far: `false`.
- `storages[]` contains one object per battery with SoC, power, status, reporting timestamp, and event/error metadata.
- `excluded` has been observed as `false`; `true` is still the documented exclusion indicator when a battery is omitted from active fleet calculations.
- Percentage fields (`current_charge`, `battery_soh`) are string percentages in observed payloads.
- Status appears as normalized code (`status`) plus a display label (`statusText`); observed pair so far: `normal` / `Normal`.
- `battery_mode` is a human-readable profile label from the live battery controller. The integration normalizes observed labels such as `Self-Consumption`, `Self - Consumption`, `Full Backup`, `Savings`, and `AI Optimization` back to BatteryConfig profile keys. A confirmed BatteryConfig `profile` is the authoritative selected/effective profile; `battery_mode` is retained as live controller telemetry and fallback context when BatteryConfig profile data is unavailable or stale.
- `battery_phase_count` and `is_flex_phase` vary by hardware/site topology; observed combinations so far: `1` / `false` and `3` / `true`.
- `led_status` is the raw battery LED/runtime status code. The integration currently interprets `12` as charging, `13` as discharging, `14` as idle, `15` as idle, and `17` as idle; any other value is treated as unknown runtime state.
- `led_status` should be interpreted alongside `status`/`statusText`; observed values in captures so far: `12` and `17`.

Observed battery LED legend:
- Rapidly Flashing Yellow: Starting up / establishing communications
- Red Double Flash: Error. Refer to `Troubleshooting`
- Solid Yellow: Not operating due to high temperature
- Solid Blue or Green: Idle. Colour transitions between blue and green as battery charge changes. Check Live State for charge status.
- Soft Pulse Blue: Discharging
- Soft Pulse Green: Charging
- Soft Pulse Yellow: Sleep mode
- Red Triple Flashes: DC switch OFF
- Red One-Second Flash: Rapid Shutdown mode
- Off: Not operating. Refer `Troubleshooting`

### 2.F HEMS (IQ Energy Router / Heat Pump Monitoring)

The endpoints below are read-oriented HEMS/IQ Energy Router APIs observed in Enlighten web and mobile captures for sites with paired router + heat-pump accessories.

Implementation note:
- The current client treats HEMS as a bearer-preferred family. It builds requests with bearer auth first, then adds shared Enlighten headers/cookies plus `requestId` and `username` when a JWT-derived user id is available.
- Heat-pump runtime preflights `system_dashboard_summary`, caches `is_hems`, and suppresses HEMS runtime/daily/power calls when the site is known unsupported.

### 2.17 HEMS Device Inventory (Router + Heat Pump Stack)
```
GET https://hems-integration.enphaseenergy.com/api/v1/hems/<site_id>/hems-devices
GET https://hems-integration.enphaseenergy.com/api/v1/hems/<site_id>/hems-devices?include-retired=true
GET https://hems-integration.enphaseenergy.com/api/v1/hems/<site_id>/hems-devices?refreshData=false
Headers:
  Accept: application/json
  Authorization: <jwt> or Bearer <jwt>
  Cookie: ...; XSRF-TOKEN=<token>; ...    # optional in web capture
  Origin: https://enlighten.enphaseenergy.com
  username: <user_id>                     # observed in Bearer-token variant
  requestId: <uuid>                       # observed in Bearer-token variant
```
Returns grouped HEMS devices, including IQ Energy Router and connected heat-pump ecosystem devices.

Example response shape (anonymized):
```json
{
  "type": "hems-device-details",
  "timestamp": "2026-03-28T05:17:58.156910991Z",
  "data": {
    "hems-devices": {
      "gateway": [
        {
          "name": "IQ Energy Router_1",
          "device-type": "IQ_ENERGY_ROUTER",
          "make": "Hive",
          "model": "Nano Hub 2",
          "uid": "ROUTER-UID-001",
          "status": "normal",
          "statusText": "Normal",
          "created-at": "2025-08-11T08:11:08Z",
          "last-report": "2026-03-28T05:16:57Z",
          "hems-device-id": "hd_router_001",
          "hems-device-facet-id": "hf_router_001",
          "pairing-status": "PAIRED",
          "device-uid": "<site_id>_IQ_ENERGY_ROUTER_1",
          "device-state": "ACTIVE",
          "iqer-uid": "<site_id>_IQ_ENERGY_ROUTER_1",
          "ip-address": "192.0.2.11"
        },
        {
          "name": "IQ Gateway_1",
          "device-type": "IQ_GATEWAY",
          "make": "Enphase",
          "model": "Envoy-S Metered",
          "uid": "GW-UID-001",
          "status": "normal",
          "statusText": "Normal",
          "created-at": "2025-08-11T09:02:58Z",
          "last-report": "2025-08-11T09:02:58Z",
          "hems-device-id": "hd_gateway_001",
          "hems-device-facet-id": "hf_gateway_001",
          "pairing-status": "PAIRED",
          "device-uid": "<site_id>_IQ_GATEWAY_1",
          "iqer-uid": "<site_id>_IQ_ENERGY_ROUTER_1"
        }
      ],
      "heat-pump": [
        {
          "name": "SG Ready Gateway_1",
          "device-type": "SG_READY_GATEWAY",
          "make": "Gude",
          "model": "Expert Net Control 2302",
          "uid": "SGR-UID-001",
          "status": "normal",
          "statusText": "Normal",
          "created-at": "2025-08-11T09:33:28Z",
          "last-report": "2026-03-28T05:16:55Z",
          "hems-device-id": "hd_sgready_001",
          "hems-device-facet-id": "hf_sgready_001",
          "pairing-status": "PAIRED",
          "device-uid": "<site_id>_SG_READY_GATEWAY_1",
          "iqer-uid": "<site_id>_IQ_ENERGY_ROUTER_1"
        },
        {
          "name": "Energy Meter_1",
          "device-type": "ENERGY_METER",
          "make": "Bcontrol",
          "model": "Energy Manager 420",
          "firmware-version": "3.3",
          "uid": "METER-UID-001",
          "status": "normal",
          "statusText": "Normal",
          "created-at": "2025-10-21T10:37:57Z",
          "last-report": "2026-03-28T05:16:55Z",
          "hems-device-id": "hd_meter_001",
          "hems-device-facet-id": "hf_meter_001",
          "pairing-status": "PAIRED",
          "device-uid": "<site_id>_ENERGY_METER_1",
          "iqer-uid": "<site_id>_IQ_ENERGY_ROUTER_1"
        },
        {
          "name": "Waermepumpe",
          "device-type": "HEAT_PUMP",
          "make": "Ochsner",
          "model": "Europa Mini WP",
          "status": "normal",
          "statusText": "Normal",
          "created-at": "2025-10-21T10:38:35.641298069Z",
          "hems-device-facet-id": "hf_heatpump_001",
          "pairing-status": "PAIRED",
          "device-uid": "<site_id>_HEAT_PUMP_1",
          "iqer-uid": "<site_id>_IQ_ENERGY_ROUTER_1",
          "fvt-time": "2025-10-21T12:45:52.110Z"
        }
      ],
      "evse": [],
      "water-heater": []
    }
  }
}
```
Observed structure:
- The top-level inventory envelope in the supplied captures was `type` + `timestamp` + `data["hems-devices"]`; an older `status/result/devices` example should not be assumed.
- Device grouping is hierarchical (`gateway`, `heat-pump`, `evse`, `water-heater`) rather than the flat `type/devices` shape used by `/app-api/<site_id>/devices.json`.
- Query variants observed so far are `include-retired=true` and `refreshData=false`; both returned the same `hems-device-details` payload shape.
- Observed: authorization varied by caller; one web capture succeeded with a bare JWT in the `Authorization` header and no cookies, while another used `Authorization: Bearer <jwt>` together with cookies, `username`, and `requestId`.
- Implementation: the current client standardizes on bearer-preferred HEMS headers instead of reproducing each capture variant literally.
- Implementation accepts `data["hems-devices"]`, `data.hems_devices`, older `result.devices`, and legacy nested `hemsDevices` from `/app-api/<site_id>/devices.json`.
- `device-uid` is a stable HEMS identifier reused across timeseries filters and related detail requests.
- `created-at`, `last-report`, and `fvt-time` were observed as ISO-8601 strings in this capture, not epoch seconds.
- `status` / `statusText` were observed as `normal` / `Normal` for all listed devices in these captures; `pairing-status` was `PAIRED`.
- `device-state` was present on the IQ Energy Router and was observed as `ACTIVE`.
- Router/gateway members may expose network-local metadata such as `ip-address`; redact concrete LAN addresses when documenting captures.
- For the captured site, device types present were `IQ_ENERGY_ROUTER`, `IQ_GATEWAY`, `SG_READY_GATEWAY`, `ENERGY_METER`, and `HEAT_PUMP`.

### 2.17.1 HEMS Heat Pump Runtime State (Per Heat Pump UID)
```
GET https://hems-integration.enphaseenergy.com/api/v1/hems/<site_id>/heatpump/<device_uid>/state?timezone=<iana_tz>
Headers:
  Accept: application/json
  Authorization: Bearer <jwt>
  Cookie: ...; XSRF-TOKEN=<token>; ...
  Origin: https://enlighten.enphaseenergy.com
  username: <user_id>
  requestId: <uuid>
```
Returns app-facing runtime state for a single heat-pump device UID.

Example response from an "off / not running" mobile-app capture (anonymized only by site/device placeholders):
```json
{
  "type": "hems-heatpump-details",
  "timestamp": "2026-03-20T07:53:00.982568365Z",
  "data": {
    "device-uid": "<site_id>_HEAT_PUMP_1",
    "heatpump-status": "IDLE",
    "vpp-sgready-mode-override": "NONE",
    "sg-ready-mode": "MODE_2",
    "last-report-at": "2026-03-20T07:51:59.643Z"
  }
}
```

Example response from a "heating / SG Ready on" mobile-app capture (anonymized):
```json
{
  "type": "hems-heatpump-details",
  "timestamp": "2026-03-20T08:19:17.945447902Z",
  "data": {
    "device-uid": "<site_id>_HEAT_PUMP_1",
    "heatpump-status": "RUNNING",
    "vpp-sgready-mode-override": "NONE",
    "sg-ready-mode": "MODE_3",
    "last-report-at": "2026-03-20T08:18:59.604Z"
  }
}
```

Observed structure:
- `type` was `hems-heatpump-details` in all captured responses.
- `heatpump-status` represented runtime state and was observed as `IDLE` when the heat pump was off and `RUNNING` while it was actively heating.
- `sg-ready-mode` and `vpp-sgready-mode-override` appear alongside runtime state and may explain SG Ready behavior independently of health/status reporting.
- `sg-ready-mode` was observed as `MODE_2` when the app/event history described the heat pump as being in normal mode, and as `MODE_3` when it was heating with recommended-consumption / SG Ready on.
- `vpp-sgready-mode-override` remained `NONE` in both idle and running captures; other values are still undocumented.
- Mobile captures also included `username` and `requestId` headers; `timezone` was observed as an IANA name (`Europe/Berlin`).
- `last-report-at` is an ISO-8601 timestamp and is more precise than the sparse `fvt-time`/`last-report` fields seen on some inventory members.

Inference:
- This endpoint is a better candidate for the user-visible "running vs not running" state than `hems-devices.statusText`, which appears to describe device health/reporting (`Normal`, `Warning`, etc.) rather than runtime activity.

### 2.17.2 HEMS Daily Device Energy Consumption
```
GET https://hems-integration.enphaseenergy.com/api/v1/hems/<site_id>/energy-consumption?from=<iso8601>&to=<iso8601>&timezone=<iana_tz>&step=P1D
Headers:
  Accept: application/json or */*
  Authorization: Bearer <jwt>
  Cookie: ...; XSRF-TOKEN=<token>; BP-XSRF-Token=<token>; ...
  Origin: https://enlighten.enphaseenergy.com
  username: <user_id>
  requestId: <uuid>
```
Returns per-device daily energy-consumption buckets for HEMS-managed loads.

Example response from the same "off / not running" capture (anonymized):
```json
{
  "type": "hems-device-details",
  "timestamp": "2026-03-20T07:53:00.739143826Z",
  "data": {
    "heat-pump": [
      {
        "device-uid": "<site_id>_HEAT_PUMP_1",
        "device-name": "Waermepumpe",
        "consumption": [
          {
            "solar": 0.0,
            "battery": 0.0,
            "grid": 0.0,
            "details": [47.0]
          }
        ]
      }
    ],
    "evse": [],
    "water-heater": []
  }
}
```

Observed structure:
- `type` was `hems-device-details` in the captured responses.
- Results are grouped by HEMS family (`heat-pump`, `evse`, `water-heater`).
- The `consumption[]` item exposes source-split totals (`solar`, `battery`, `grid`) plus a `details[]` numeric array.
- The supplied March 28 capture returned `solar=0`, `battery=0`, `grid=0`, and `details=[987]`; earlier captures used decimal-looking values (`47.0`, `201.0`, `211.0`, `220.0`, `230.0`), and a later idle browser capture returned `details=[3]`, so clients should treat these as generic numeric values rather than fixed-format floats.
- In active-heating captures the `details[]` value increased across polls (`201.0`, `211.0`, `220.0`, `230.0`) while `/heatpump/<device_uid>/state` reported `heatpump-status: RUNNING`.
- Browser captures also used `Authorization: Bearer <jwt>` together with Enlighten cookies, `username`, and `requestId`; one Safari capture additionally included `BP-XSRF-Token` and `Accept: */*`.
- Captured browser requests used local-day bounds encoded as UTC strings, for example `from=2026-04-02T00:00:00.000Z`, `to=2026-04-02T23:59:59.999Z`, `timezone=Europe/Berlin`, `step=P1D`.

Inference:
- In the "not running" capture, `details: [47.0]` was still present even though the runtime endpoint reported `heatpump-status: IDLE`, so clients should not treat this endpoint as a strict on/off signal.
- A later idle browser capture returned `details: [3]` while runtime-state still reported `IDLE`, which suggests this payload may carry a small instantaneous or near-live device reading rather than a strict zeroed idle state.
- Running-state browser captures are still inconsistent: some observations align with current watts-style values, while others resemble larger day-accumulating totals. Consumers should therefore treat the semantics of `details[]` as observed-but-not-fully-stable and verify behavior against runtime-state and UI captures when changing parsing logic.

### 2.17.3 HEMS Supported Models Catalog
```
GET https://hems-integration.enphaseenergy.com/api/v1/hems/list-supported-models?deviceType=<device_type>
Headers:
  Accept: application/json, text/javascript, */*; q=0.01
  Authorization: Bearer <jwt>
  Cookie: <authenticated Enlighten session cookies>
  e-auth-token: <session_id>
  username: <user_id>
  requestId: <uuid>
  Origin: https://enlighten.enphaseenergy.com
```
Returns the supported-model catalog for a HEMS device family. The observed request used `deviceType=HEAT_PUMP`.

Example response:
```json
{
  "type": "hems-device-details",
  "timestamp": "2026-03-28T05:43:36.648255652Z",
  "data": {
    "Dimplex": [],
    "Vaillant": [],
    "tecalor": [],
    "iDM": [],
    "alpha innotec": [],
    "NIBE": [],
    "Wolf": [],
    "Viessmann": [],
    "NOVELAN": [],
    "Daikin": [],
    "Remeha": [],
    "Stiebel Eltron": []
  }
}
```

Observed structure:
- The response is a manufacturer-keyed object inside `data`, not an array.
- Empty arrays are meaningful and should be preserved; the capture showed known brands with no discovered models rather than omitting them.
- Manufacturer keys are case-sensitive and mixed-style (`iDM`, `alpha innotec`, `Stiebel Eltron`), so clients should not normalize them.

### 2.18 HEMS Power Timeseries (Heat Pump Consumption)
```
GET /systems/<site_id>/hems_power_timeseries
GET /systems/<site_id>/hems_power_timeseries?device-uid=<site_id>_HEAT_PUMP_1
Headers:
  Accept: application/json
  Cookie: ...; XSRF-TOKEN=<token>; ...
  e-auth-token: <token>
  X-Requested-With: XMLHttpRequest
```
Returns heat-pump consumption series as fixed-interval power buckets.

Runtime status: not implemented. This endpoint was observed during API research but
proved unreliable for heat-pump power in Home Assistant, so the integration does not
call it and does not expose helper code for it. Current heat-pump power sensors derive
watts from deltas in the site-today `heatpump` daily energy total instead.

Example response shape (anonymized):
```json
{
  "heat_pump_consumption": [4.0, 0.0, 0.0, 4.0, 0.0, 120.0, 564.0, 0.0],
  "start_date": 1771628400
}
```
Observed structure:
- `heat_pump_consumption` is a numeric array (captured samples length `672`).
- `start_date` is an epoch-seconds anchor for the first bucket.
- The variant with `device-uid` returned the same key shape as the unfiltered call.
- Do not use this endpoint for runtime heat-pump power unless new evidence shows the
  bucket series is reliable across sites and timezones.

### 2.19 HEMS Lifetime Consumption (Heat Pump / EV / Water Heater)
```
GET /systems/<site_id>/hems_consumption_lifetime
Headers:
  Accept: application/json
  Cookie: ...; XSRF-TOKEN=<token>; ...
  e-auth-token: <token>
  X-Requested-With: XMLHttpRequest
```
Returns long-window consumption flows; shape is compatible with site lifetime arrays and includes HEMS channels.

Example response shape (anonymized):
```json
{
  "system_id": 1234567,
  "start_date": "2025-01-01",
  "last_report_date": 1772108054,
  "update_pending": false,
  "production": [12000, 8300, 9000],
  "consumption": [7100, 13400, 15800],
  "evse": [0.0, 0.0, 1320.4],
  "heatpump": [0.0, 0.0, 412.0],
  "water_heater": [0.0, 0.0, 0.0]
}
```
Observed structure:
- `heatpump` can be populated even when generic `/pv/systems/<site_id>/lifetime_energy` previously returned it as empty.
- Values are numeric bucket totals; arrays align by index across flow types.

### 2.20 HEMS Device Event/Graph Views (Per Device UID)
```
GET /systems/<site_id>/iq_er/<device_uid>/events
GET /systems/<site_id>/iq_er/<device_uid>/events.json
GET /systems/<site_id>/heat_pump/<device_uid>/events
GET /systems/<site_id>/heat_pump/<device_uid>/events.json
GET /systems/<site_id>/heat_pump/<device_uid>/graphs
```
Observed usage:
- Observed when opening IQ Energy Router and heat-pump detail pages.
- Observed responses were HTTP `200` for router, SG Ready gateway, and energy meter device UIDs.
- In captured traces these were read-only page/data fetches; no corresponding control endpoint was observed for heat-pump actuation.
- The integration fetches the `.json` variants for runtime diagnostics only; payloads are redacted and summarized instead of exposed as entities.

### 2.21 HEMS Live Stream Status Toggle (Monitoring Transport)
```
PUT https://hems-integration.enphaseenergy.com/api/v1/hems/<site_id>/live-stream/status
Headers:
  Accept: application/json
  Content-Type: application/json
  Cookie: ...; XSRF-TOKEN=<token>; ...
  Origin: https://enlighten.enphaseenergy.com
Body:
  {"livestream-enabled": true}
```
Controls HEMS live data streaming state, used for monitoring refresh behavior.

Observed behavior:
- Captured write payload was `{"livestream-enabled": true}`.
- A later mobile-app capture also showed `{"livestream-enabled": false}` returning `"data": {"enable": false}`.
- No heat-pump mode/setpoint/relay control payloads were observed in the same session; this endpoint appears transport-oriented rather than device-actuation control.

### 2.21.1 HEMS Live Stream Vitals Toggle
```
PUT https://hems-integration.enphaseenergy.com/api/v1/hems/<site_id>/live-stream/vitals
Headers:
  Accept: application/json
  Content-Type: application/json
  Cookie: ...; XSRF-TOKEN=<token>; ...
  Origin: https://enlighten.enphaseenergy.com
Body:
  {"livestream-enabled": true}
```
Controls the live "vitals" transport.

Example response (anonymized):
```json
{
  "type": "hems-site-details",
  "timestamp": "2026-03-08T09:07:19.756777654Z",
  "data": {
    "enable": true
  }
}
```

Observed behavior:
- This endpoint mirrors the same write payload as `/live-stream/status` but returns a small acknowledgement envelope.
- A later capture also showed disable semantics: request body `{"livestream-enabled": false}` returned `"data": {"enable": false}`.
- Treat the endpoint as transport control, not device actuation.

### 2.G Mobile/Web Shared Constants

### 2.22 Shared Constants Payload
```
GET https://enlighten-mobile-38d22.firebaseio.com/enho_constants.json
```
Returns a global JSON document consumed by the Enphase mobile/web clients for feature constants, localized support/store links,
minimum supported app versions, SKU catalogs, and similar static configuration.

Auth observations:
- Captured request did not include cookies or a bearer token.
- The observed `e-auth-token` header value was literal `null`, suggesting the endpoint is public or at least not session-bound.
- Response was HTTP `200 OK` with `Content-Type: application/json; charset=utf-8`.

Example response excerpt (anonymized):
```json
{
  "AI_SAVINGS_DATA": {
    "AI_SAVINGS_METRICS_LOWER_LIMIT": 0.1,
    "AI_SAVINGS_METRICS_UPPER_LIMIT": 0.1
  },
  "CONNECTIVITY_DATA": {
    "ENV_SPECIAL_CHARACTERS": ["#", "$", "&"],
    "MIN_ESW_FOR_ENCODING": "D8.3.5314"
  },
  "ENPHASE_STORE": {
    "US": { "en": "https://store.enphase.com/storefront/en-us" }
  },
  "IQCP_DATA": {
    "APP_VERSION": "4.1.0",
    "FW_VERSION": "2.0.0"
  }
}
```

Notable field groups observed:
- `AI_SAVINGS_DATA`: thresholds plus country/language-specific support article links.
- `CONNECTIVITY_DATA`: app-side validation constants for special characters and minimum software support.
- `ENPHASE_STORE` and `ENSTORE_CONSTANTS`: localized storefront links, SKU lists, titles, media base URLs, and feature toggles.
- `IQCP_DATA`: balcony solar / IQCP app and firmware compatibility values plus device naming strings.

Integration relevance:
- No charger telemetry, per-site configuration, or account-specific state was present in the captured payload.
- The document appears useful as a reference for feature-gating and catalog discovery, but not for live EV charger control/state.
- Because the payload is shared/global and not site-scoped, any future use in the integration should treat it as cacheable static metadata.

### 2.22.1 App Shell and Feature-Gating Endpoints
These Browser-capture-only endpoints were observed alongside normal mobile/web site loads. They help explain app-shell behavior and feature gates, but they are not part of the current runtime surface.

#### Customer support metadata
```
GET /app-api/customer_support.json
```

#### Homeowner notices
```
GET /app-api/homeowner_notices.json
```

#### Net promoter score prompt state
```
GET /app-api/nps.json
```

#### Release notes feed
```
GET /app-api/release_notes.json
```

#### Feature-level applicability
```
POST /decision_rule/feature_level_applicability.json
Content-Type: application/x-www-form-urlencoded; charset=UTF-8
```

Observed request body fields:
- `decision_reason=A_B testing`
- `input[email]=<redacted>`
- `input[app_version]=3.4.0`
- `input[site_id]=<site_id>`

Observed response notes:
- The response contains a large `output` object of feature flags keyed by internal identifiers.
- Observed non-sensitive examples included `ENLM_UI_V2=true`, `power_interval_change=true`, and `ITK_Allow_Entrez_Support=true`.
- Real captures include personal data and site identifiers in both request and response context; redact those fields before sharing.

#### PES in-app status/banner endpoint
```
GET /service/pes_management/systems/<site_id>/inapp?type=RMA
```

Observed behavior:
- The HAR captured `404 Not Found` with plain-text body `Site with PES count not found with given id <site_id>`.
- The `type` query parameter was observed as `RMA`.

#### Health check
```
GET /healthcheck
```

Example response:
```json
{
  "OPS_TICKET": "NA",
  "DEPLOY_TIME": "2026-04-07 08:55:55 UTC",
  "SHA": "N/A",
  "VERSION": "26.3.2",
  "STATUS": "UP",
  "ENVIRONMENT": "production"
}
```

### 2.23 Runtime Firmware Catalog
```
GET https://raw.githubusercontent.com/barneyonline/ha-enphase-energy/firmware-catalog/catalog/v1/runtime_catalog.json
```
The integration uses this repository-managed catalog to populate advisory firmware update entities for gateways and EV chargers. It is not an Enphase cloud endpoint.

Implementation notes:
- `ENPHASE_EV_FIRMWARE_CATALOG_URL` can override the default URL.
- The catalog must be a JSON object with `schema_version: 1` and a `devices` object.
- The manager caches successful fetches for 12 hours and uses a 30-minute stale-on-error backoff.
- Catalog selection prefers locale and country matches, then base language, then global entries.
- EV charger installed/target versions still come from `/service/evse_management/fwDetails/<site_id>` when available; the catalog provides latest-version and release metadata.

---

### 2.H Remaining Enlighten Manager and Installer Read Endpoints

The contracts in `2.24`-`2.29` were captured on 2026-07-11 while navigating the authenticated installer System Dashboard and the legacy Enlighten Manager View, Graph, Reports, Devices, Events, Live Status, Summary, and Settings surfaces. They are documentation-only unless another section explicitly marks them as runtime endpoints.

Privacy rules for this capture:

- Site, account, company, user, gateway, device, channel, meter, task, and event identifiers are represented by placeholders.
- Email addresses, phone numbers, site names, addresses, serial numbers, IP addresses, modem identifiers, tokens, cookies, MQTT topics, firmware paths, and authenticated download URLs are omitted or redacted.
- Dynamic JSON keys can themselves be serial numbers or other identifiers and must be redacted before logging.
- Account-profile and MQTT-bootstrap payloads must never be stored verbatim in diagnostics.

Common System Dashboard request shape:

```http
Accept: application/json
Content-Type: application/json
Referer: https://enlighten.enphaseenergy.com/app/system_dashboard/sites/<site_id>/...
Cookie: <authenticated_session_cookies>
Authorization: Bearer <token>  # endpoint/session dependent
X-XSRF-Token: <xsrf_token>    # endpoint/session dependent
```

Common legacy Manager XHR shape:

```http
Accept: application/json, text/javascript, */*; q=0.01
X-Requested-With: XMLHttpRequest
Referer: https://enlighten.enphaseenergy.com/systems/<site_id>/<manager_screen>
Cookie: <authenticated_session_cookies>
```

#### 2.24 Dashboard account, configuration, and feature metadata

##### 2.24.1 System configuration

```http
GET /service/enlm_ms/api_internal/<site_id>/system_config
```

Confirmed top-level groups:

```json
{
  "battery_rating_info": {},
  "site_pel_settings_config": {},
  "pcs_settings_config": {},
  "battery_pcs_settings_config": {},
  "sspl_settings_config": {},
  "phase_imbalance_info": {}
}
```

Observed data covered grid voltage/country, production export limiting, PEL timing, storage power-control settings, battery current and fuse limits, import/generation limiting, per-phase breaker ratings, and phase-imbalance information. The exact groups are capability dependent.

##### 2.24.2 Dashboard A/B feature catalog

```http
GET /service/system_dashboard/api_internal/dashboard/ab_features
    ?feature[email]=<email>
    &feature[company_id]=<company_id_or_null>
    &feature[site_id]=<site_id>
```

```json
[
  {
    "feature_id": "<feature_key>",
    "feature_type": "<feature_type>",
    "is_enabled": true
  }
]
```

The request itself contains private account identifiers. Do not log its query string without replacing the email, company ID, and site ID.

##### 2.24.3 Dashboard account profile

```http
GET /service/system_dashboard/api_internal/ms/account
```

Sanitized shape excerpt:

```json
{
  "user_id": "<user_id>",
  "company_id": "<company_id>",
  "company_name": "<company_name>",
  "first_name": "<first_name>",
  "last_name": "<last_name>",
  "email": "<email>",
  "phone": "<phone>",
  "roles": ["<role>"],
  "locale": "<locale>",
  "country": "<country_code>",
  "token": "<credential_redacted>"
}
```

Other observed fields included account status, preferred metric, impersonation state, community URL, pending email, admin/consumer/self-installer flags, and company role metadata. The `token` field is a credential and must never enter diagnostics or logs.

##### 2.24.4 Enlighten shell account metadata

```http
GET /service/enlm_ms/api_internal/account/data
```

```json
{
  "banner_notices": [],
  "company_master_data": {},
  "available_locales": [],
  "footer_links": {},
  "phone_country_codes": []
}
```

This is account-shell metadata rather than site telemetry. Nested company and link records can contain private identifiers and should be summarized rather than retained verbatim.

#### 2.25 Dashboard logs, diagnostic history, and MQTT bootstraps

The two log endpoints below use `POST` only to submit read filters; no state change was observed.

##### 2.25.1 Activation logs

```http
POST /service/system_dashboard/api_internal/dashboard/sites/<site_id>/activation_logs
    ?range=<range>
    &type=table
    &page=<page>
    &per_page=<n>
```

```json
{
  "serial_numbers": ["<site_id>", "<device_sn>"],
  "filter_columns": [
    "local_timestamp",
    "created_at",
    "severity",
    "activity_name",
    "source",
    "user"
  ]
}
```

```json
{
  "activation_logs": [],
  "page": "1",
  "per_page": "200",
  "total": 0,
  "csv_link": "<authenticated_url_redacted>"
}
```

The captured date contained no rows. Any returned user identities, device serials, and authenticated `csv_link` must be redacted.

##### 2.25.2 ITK raw logs

```http
POST /service/system_dashboard/api_internal/dashboard/sites/<site_id>/itk_raw_logs
```

```json
{
  "range": "today",
  "version": "2.0",
  "type": "table",
  "onChangeSerialNumber": false,
  "filter_columns": ["user", "timestamp", "download_link"],
  "serial_numbers": ["<site_id>", "<device_sn>"]
}
```

```json
{
  "itk_raw_logs": [],
  "page": 1,
  "per_page": 200,
  "total": 0
}
```

Download links are authenticated and must be removed from shared captures.

##### 2.25.3 Enlighten logs

```http
GET /service/system_dashboard/api_internal/dashboard/sites/<site_id>/enho_logs
    ?range=<range>
    &type=table
    &page=<page>
    &per_page=<n>
```

```json
{
  "enho_logs": [],
  "page": "1",
  "per_page": "200",
  "total": 0
}
```

The production bundle omits `serial_numbers` from this request even when the UI has a device selection. The captured date contained no rows, so the row schema remains unconfirmed.

##### 2.25.4 Diagnostic task definitions

```http
GET /service/system_dashboard/api_internal/cs/sites/<site_id>/task_types
```

```json
{
  "all_tasks": [
    {
      "task_type_id": 0,
      "task_name": "<display_name>",
      "task_key": "<task_key>",
      "task_description": "<description>",
      "input_required": false
    }
  ],
  "envoys": [
    {
      "id": "<device_id>",
      "serial_number": "<gateway_sn>"
    }
  ]
}
```

This endpoint is read-only. It must not be confused with the separate task-submission route.

##### 2.25.5 Diagnostic task history

```http
GET /service/system_dashboard/api_internal/cs/sites/<site_id>/tasks
    ?page=<page>
    &per_page=<n>
    &sort_by_created_date=<optional_sort>
    &task_name=<optional_name>
    &task_id=<optional_id>
```

Sanitized response shape:

```json
{
  "tasks": [
    {
      "task_id": "<task_id>",
      "created_date": "<timestamp>",
      "task_name": "<task_name>",
      "envoy": "<gateway_sn>",
      "task_params": "<redacted>",
      "created_by": "<user_redacted>",
      "completed_date": "<timestamp_or_null>",
      "result": "<result>",
      "cancel": false,
      "response": "<redacted>",
      "request": "<redacted>",
      "env_log_url": "<authenticated_url_redacted>"
    }
  ],
  "page": 1,
  "per_page": 10,
  "iTotalRecords": 0
}
```

Task request/response data, creator identity, gateway serial, parameters, and log URLs are sensitive.

##### 2.25.6 PLC FFT MQTT bootstrap

```http
GET /service/system_dashboard/api_internal/dashboard/fft_scan
    ?serial_number=<gateway_sn>
    &mode=<mode>
```

```json
{
  "fft_scan_duration": 0,
  "fft_scan_topic": "<mqtt_topic_redacted>",
  "site_id": "<site_id>",
  "env": "<environment>",
  "aws_iot_endpoint": "<endpoint_redacted>",
  "aws_authorizer": "<credential_redacted>",
  "aws_token_key": "<credential_redacted>",
  "aws_token_value": "<credential_redacted>",
  "aws_digest": "<credential_redacted>"
}
```

This `GET` only bootstraps the MQTT connection. The separate UI Generate action initiates a PLC scan and was not invoked.

##### 2.25.7 Generator/live MQTT bootstrap

```http
GET /admin/aws_sigv4/livestream?serial_num=<gateway_sn>
```

Sanitized response fields:

```text
live_stream_duration
live_stream_topic
dr_event_mode
has_load_controls
timeout
site_id
env
aws_iot_endpoint
aws_authorizer
aws_token_key
aws_token_value
aws_digest
```

The topic, endpoint, authorizer, token, and digest values are credentials or connection secrets and must never be persisted or logged verbatim.

#### 2.26 Enlighten Manager site metadata and energy series

##### 2.26.1 EV supported-country catalog

```http
GET /service/evse_management/api/v1/config/supported_countries
```

```json
{
  "meta": {
    "serverTimeStamp": "<timestamp>",
    "rowCount": 0
  },
  "data": [
    {
      "name": "<country_name>",
      "code": "<country_code>",
      "region": "<region>",
      "zipCodePattern": "<regex>",
      "learnMoreUrl": "<public_url>",
      "faqUrl": "<public_url>"
    }
  ],
  "error": {}
}
```

This is global EV configuration metadata, not site-specific state.

##### 2.26.2 Site weather and statistics fragments

```http
GET /systems/<site_id>/weather.json?locale=<locale>
```

```json
{
  "string": "<localized_condition>",
  "code": "<condition_code>",
  "temperature": {
    "value": 0,
    "min": 0,
    "max": 0,
    "display": "<localized_temperature>"
  }
}
```

```http
GET /systems/<site_id>/show_stat_data
```

`show_stat_data` returns `text/html`. The fragment contains localized today, seven-day, month-to-date, and lifetime production/consumption values, peak/latest production, and Microinverter AC voltage. Treat it as presentation HTML rather than a stable JSON schema.

##### 2.26.3 Date-range and lifetime production

```http
GET /systems/<site_id>/energy
    ?start_date=<YYYY-MM-DD>
    &end_date=<YYYY-MM-DD>
```

```json
{
  "production": 0,
  "start_date": "<YYYY-MM-DD>",
  "end_date": "<YYYY-MM-DD>",
  "inverter_count": 0
}
```

```http
GET /systems/<site_id>/energy_lifetime?all_production_sources=true
```

```json
{
  "system_id": "<site_id>",
  "start_date": "<YYYY-MM-DD>",
  "production": {
    "micros": [0],
    "meter": [0]
  },
  "pending": null,
  "deleted": [0]
}
```

The production arrays are daily, positionally aligned series.

```http
GET /systems/<site_id>/energy_lifetime_est
```

The estimate route returned HTTP `200` with `{}` on the captured site. The Graph page still identifies it as the lifetime estimate source.

##### 2.26.4 Lifetime site and EV consumption

```http
GET /systems/<site_id>/consumption_lifetime
```

```json
{
  "start_date": "<YYYY-MM-DD>",
  "consumption": [0]
}
```

```http
GET /systems/<site_id>/evse_consumption_lifetime
```

```json
{
  "evse_start_date": "<YYYY-MM-DD>",
  "evse_consumption": [0],
  "evse_consumption_phase": {
    "L1(A)": {"phase_name": "L1(A)", "timeseries": null},
    "L2(B)": {"phase_name": "L2(B)", "timeseries": null},
    "L3(C)": {"phase_name": "L3(C)", "timeseries": null}
  }
}
```

The aggregate EV array was populated in the capture; the per-phase lifetime series were `null`.

##### 2.26.5 EV charging power timeseries

```http
GET /systems/<site_id>/evse_power_timeseries
    ?days=<integer>
    &date=<YYYY-MM-DD>
```

```json
{
  "evse_consumption": [0],
  "evse_consumption_phase": {
    "L1(A)": {"phase_name": "L1(A)", "timeseries": [0]},
    "L2(B)": {"phase_name": "L2(B)", "timeseries": [0]},
    "L3(C)": {"phase_name": "L3(C)", "timeseries": [0]}
  },
  "start_date": 0
}
```

The Manager Graph page requests this alongside site `power_time_series` data.

#### 2.27 Enlighten Manager device and event feeds

##### 2.27.1 Microinverter DataTables inventory

```http
GET /systems/<site_id>/pcu_datatable.js?<datatable_query>
```

Observed DataTables 1.x parameters included:

```text
sEcho, iColumns, sColumns, iDisplayStart, iDisplayLength,
mDataProp_<n>, sSearch, sSearch_<n>, bRegex, bRegex_<n>,
bSearchable_<n>, iSortCol_0, sSortDir_0, iSortingCols,
bSortable_<n>, in_mobile_view, visible_columns, sCompanies, eDeviceID
```

```json
{
  "iTotalRecords": 0,
  "iTotalDisplayRecords": 0,
  "sEcho": 1,
  "aaData": [
    ["<cell_html_or_value>"]
  ]
}
```

The observed column order covered serial/part identifiers, phase, AC/DC electrical values, temperature, power, lifetime energy, SKU/assembly data, last report, status, reporting gateway, device group, and warranty. Cells can contain HTML with identifiers and authenticated links; sanitize markup before reuse.

##### 2.27.2 Site and device event DataTables feeds

```http
GET /systems/<site_id>/events.json?<datatable_query>
GET /systems/<site_id>/envoys/<gateway_id>/events.json?<datatable_query>&eDeviceID=<gateway_id>
GET /systems/<site_id>/envoys/<gateway_id>/modem_events.json?<datatable_query>&eDeviceID=<gateway_id>
GET /systems/<site_id>/inverters/<inverter_id>/events.json?<datatable_query>&eDeviceID=<inverter_id>
```

The fixed columns are `status,severity,device,event_type,first_set`.

```json
{
  "iTotalRecords": 0,
  "iTotalDisplayRecords": 0,
  "sEcho": 1,
  "aaData": [],
  "availablePages": {
    "next_pointers": {},
    "last_offset": 0
  }
}
```

The capture returned no matching rows, so the populated `aaData` cell schema remains unconfirmed.

##### 2.27.3 Microinverter statistics and multistat telemetry

```http
GET /systems/<site_id>/inverters/<inverter_id>/show_stat_data_x
```

This route returns localized `text/html` containing today, seven-day, month-to-date, and lifetime energy plus AC voltage.

```http
GET /systems/<site_id>/inverters/<inverter_id>/time_series_x
    ?date=<YYYY-MM-DD>
    &stat=POWR,DCV,DCA,ACV,ACHZ,TMPI
```

```json
{
  "date": "<YYYY-MM-DD>",
  "ch_id": "<channel_id>",
  "POWR": [[0, 0, 0]],
  "DCV": [[0, 0]],
  "DCA": [[0, 0]],
  "ACV": [[0, 0]],
  "ACHZ": [[0, 0]],
  "TMPI": [[0, 0]],
  "stat_info": {
    "POWR": {"name": "<label>", "format": {}, "range": {}},
    "TMPI": {
      "name": "<label>",
      "format": {},
      "range": {},
      "preferred_temperature_unit": "<unit>"
    }
  }
}
```

`stat_info` also supplies metadata for DC voltage/current, AC voltage/frequency, and temperature. The Graph screen made one request per day for a seven-day view.

#### 2.28 Enlighten Manager Settings and server-rendered documents

##### 2.28.1 Settings fragments

```http
GET /service/array-builder/api/<site_id>/v1-block-status
```

```json
{
  "blocked": false
}
```

```http
GET /enlm_internal/site_photos?site_id=<site_id>
GET /systems/<site_id>/third_party_reporting_details
GET /systems/<site_id>/async_tariff_details
```

- `site_photos` returns an HTML fragment; the captured response was effectively empty.
- `third_party_reporting_details` returned HTTP `204 No Content` on the captured site.
- `async_tariff_details` returns an HTML fragment linking to tariff, battery, system-profile, and optional optimization applications. It can embed user/site identifiers and short-lived session material, so only its structural presence should be logged.

##### 2.28.2 Confirmed server-rendered Manager documents

The legacy Manager transports substantial read-only state in authenticated HTML rather than JSON. These routes returned HTTP `200` during normal navigation:

| Route | Data carried by the HTML response |
| --- | --- |
| `/systems/<site_id>/arrays` | View metadata, graph paths, date ranges, and playback options |
| `/systems/<site_id>/graphs` | Graph metadata and source URLs |
| `/systems/<site_id>/reports` | Site report types and generation form |
| `/systems/<site_id>/devices` | Gateway, meter, Microinverter, battery, controller, EVSE, modem, firmware, status, energy, and warranty fields |
| `/systems/<site_id>/events` | Event filters and DataTables setup |
| `/systems/<site_id>/overview` | Summary tiles and chart source metadata |
| `/systems/<site_id>/details` | Settings form state and embedded fragment URLs |
| `/systems/<site_id>/envoys/<gateway_id>` | Gateway firmware/status, IEEE 2030.5 state, communications, PLC, device counts, report settings, and limiting state |
| `/systems/<site_id>/envoys/<gateway_id>/events` | Gateway event table shell |
| `/systems/<site_id>/envoys/<gateway_id>/modem` | Carrier/plan, ICCID/IMEI, SIM/APN, signal, firmware, heartbeat, and SINR metadata |
| `/systems/<site_id>/envoys/<gateway_id>/modem_events` | Modem event table shell |
| `/systems/<site_id>/meters/<meter_id>` | Meter type, phase, state, serial, polarity, lifetime offset, and start date |
| `/systems/<site_id>/inverters/<inverter_id>/arrays` | Per-Microinverter View and energy sidebar |
| `/systems/<site_id>/inverters/<inverter_id>/graphs` | Per-Microinverter Graph metadata |
| `/systems/<site_id>/inverters/<inverter_id>/reports` | Per-Microinverter report form |
| `/systems/<site_id>/inverters/<inverter_id>/events` | Per-Microinverter event table shell |

Modem identifiers, site addresses, access lists, user details, device serials, firmware values, and embedded authenticity tokens are sensitive and are intentionally absent from this specification.

##### 2.28.3 Embedded application shells

```http
GET /app/installer_evse/evse/sites/<site_id>/devices?locale=<locale>
GET /app/system_dashboard/external/<site_id>/compliance?locale=<locale>
```

Both returned HTML application shells. A `200` shell response does not prove that the embedded application's subsequent data APIs loaded successfully.

#### 2.29 Source-backed read candidates not invoked

The following contracts were present in authenticated HTML or the production JavaScript bundle but were deliberately not invoked. They are not confirmed response contracts and must not be treated as runtime requirements.

##### 2.29.1 Manager reports and export forms

```http
POST /systems/<site_id>/generate_rpt
Content-Type: application/x-www-form-urlencoded
```

Common fields:

```text
authenticity_token=<csrf_token>
report[title]=<localized_report_title>
report[type]=<integer_code>
commit=<submit_label>
```

Conditional fields cover date/week/month/year ranges, device selection, interval size, metadata inclusion, production/consumption/battery ranges, gateway/array reports, and EV daily/session reports. Observed report type codes included site production/power, per-module production, meter, grid profile, site consumption, battery, gateway/array production, and EV charging reports. The form was inspected but not submitted, so no response body was captured.

```http
POST /systems/<site_id>/inverters/<inverter_id>/generate_report
Content-Type: application/x-www-form-urlencoded
```

The Microinverter form advertises energy-production and recent-power report types and requires a live CSRF token. It was not submitted.

```http
GET /systems/<site_id>/pcu_datatable.csv?<datatable_query>
```

The CSV link mirrors the Microinverter DataTables filters, sort, visible columns, company selector, and optional device ID. The download was not invoked.

Captured CSRF tokens and generated download URLs must never be committed or replayed.

##### 2.29.2 Manager conditional data sources

```http
GET /systems/<site_id>/energy_lifetime_filtered?<unverified_query>
GET /systems/<site_id>/consumption_energy_lifetime_filtered?<unverified_query>
GET /systems/<site_id>/power_time_series_filtered?<unverified_query>
GET /systems/<site_id>/iq_air_data_timeseries?<unverified_query>
GET /systems/<site_id>/inverters/<inverter_id>/energy_lifetime?<unverified_query>
GET /systems/<site_id>/module_status/<channel_id>.json
```

These paths were advertised by Graph/View source metadata but were not requested by the captured site's default state.

##### 2.29.3 System Dashboard conditional reads

```http
GET /service/system_dashboard/api_internal/{cs|dashboard}/sites/<site_id>/data/intervals?<normalized_query>
GET /service/system_dashboard/api_internal/{cs|dashboard}/sites/<site_id>/data/meters-graph-data?<normalized_query>
GET /service/system_dashboard/api_internal/cs/sites/<site_id>/events/get_event_change_details?event_date=<date>&event_key=<key>
GET /service/system_dashboard/api_internal/cs/sites/<site_id>/events/get_event_change_details?event_id=<event_id>
GET /service/system_dashboard/api_internal/cs/sites/<site_id>/alarms/details?alarm_id=<alarm_id>
GET /service/device-state-ms/api/v1/systems/<site_id>/site-config-audit/active-profiles?from=<YYYY-MM-DD>&to=<YYYY-MM-DD>&timezone=<iana_timezone>
GET /service/site_search/sites/search/autocomplete?term=<site_name>&type=ENSEMBLE&limit=10
```

Evidence limits:

- A focused `data/intervals` request returned HTTP `200` with application status `422` because a device serial was required; no query grid was attempted.
- `meters-graph-data` was bundle-backed but not invoked because no valid chart selection was available.
- Event/alarm detail routes lacked a valid event key, event ID, or alarm ID on the captured site.
- The active-profile route rejected ISO timestamps and disclosed that `from`/`to` require local dates; the corrected form was not retried.
- Site autocomplete was not invoked because that would transmit the private site name solely for exploration.

Potentially stateful diagnostic `GET` routes (such as meter CT verification) and all task, alarm-clear, tunnel, device-configuration, Settings, report-generation, download, and control actions were excluded from live probing.

---

## 3. EV Charger Control Operations

Observed request variants differ across regions. All payloads shown below are the canonical request.

### 3.1 Start Charging / Set Amps
```
POST /service/evse_controller/<site_id>/ev_chargers/<sn>/start_charging
Body: { "chargingLevel": 32, "connectorId": 1 }
```
Fallback variants observed:
- `PUT` instead of `POST`
- Path `/ev_charger/` (singular)
- Payload keys `charging_level` / `connector_id`
- Connector-only body `{ "connectorId": 1 }`
- Level-only body `{ "chargingLevel": 32 }`
- No body (uses last stored level)
- The implementation caches working variants, with separate preferences for requests that should include or omit the charging level.
- Backend `409`/`422` or parsed "not plugged" errors are normalized to `{ "status": "not_ready" }`; "already in charging state" is normalized to `{ "status": "already_charging" }`.
- Transient backend `5xx` responses are raised immediately instead of falling through to method, path, or payload fallbacks. The EVSE runtime still retries without a charge-level payload when a `500` response explicitly reports an invalid charge level and no explicit setpoint was requested.

Typical response:
```json
{ "status": "accepted", "chargingLevel": 32 }
```

### 3.2 Stop Charging
```
PUT /service/evse_controller/<site_id>/ev_chargers/<sn>/stop_charging
```
Fallbacks: `POST`, singular path `/ev_charger/`.
```json
{ "status": "accepted" }
```
Implementation notes:
- Successful responses cache the working variant.
- Backend `400`, semantic `404`, `409`, or `422` responses are treated as benign no-op `{ "status": "not_active" }` but do not cache the variant.
- Routing `404` responses with `No static resource` in the response text or JSON problem details are treated as route failures so fallback variants can still be tried.
- Transient backend `5xx` responses are raised immediately instead of falling through to method or path fallbacks.

### 3.3 Trigger OCPP Message
```
POST /service/evse_controller/<site_id>/ev_charger/<sn>/trigger_message
Body: { "requestedMessage": "MeterValues" }
```
Replies vary by backend. Common shape:
```json
{
  "status": "accepted",
  "message": "MeterValues",
  "details": {
    "initiatedAt": "2025-01-15T12:34:56.000Z",
    "trackingId": "TICKET-XYZ123"
  }
}
```

---

## 4. EV Scheduler (Charge Mode) API

Separate Enlighten service requiring bearer tokens in addition to the cookie headers.

### 4.1 Read Preferred Charge Mode
```
GET /service/evse_scheduler/api/v1/iqevc/charging-mode/<site_id>/<sn>/preference
Headers: Authorization: Bearer <token>
```
Response:
```json
{
  "data": {
    "modes": {
      "manualCharging": { "enabled": true, "chargingMode": "MANUAL_CHARGING" },
      "scheduledCharging": { "enabled": false },
      "greenCharging": { "enabled": false }
    }
  }
}
```

Observed AI Optimization variant:
```json
{
  "meta": {
    "serverTimeStamp": "<timestamp>"
  },
  "data": {
    "config": {
      "profile": "ai_optimisation",
      "modeSyncStatus": "synced",
      "stormActive": false,
      "isModeCancellable": true,
      "pendingModesOffGrid": false,
      "pendingSchedulesOffGrid": false
    },
    "modes": {
      "smartCharging": {
        "chargingMode": "SMART_CHARGING",
        "enabled": true,
        "showSettings": false,
        "isHidden": false,
        "isDisabledByStorm": false,
        "goalsEnabled": 0,
        "defaultGoalAdded": false
      },
      "scheduledCharging": {
        "chargingMode": "SCHEDULED_CHARGING",
        "enabled": false,
        "showSettings": false,
        "isHidden": false,
        "isDisabledByStorm": false,
        "schedulesEnabled": 0
      },
      "manualCharging": {
        "chargingMode": "MANUAL_CHARGING",
        "enabled": false,
        "showSettings": false,
        "isHidden": false,
        "isDisabledByStorm": false
      }
    }
  },
  "error": {}
}
```
Notes:
- The current implementation resolves this bearer from the `enlighten_manager_token_production` cookie first, then falls back to the stored access token.
- Scheduler calls are sent on top of the normal session-cookie/base EV headers; the bearer is the additional requirement that makes this family distinct from simple EV status reads.

### 4.2 Set Charge Mode
```
PUT /service/evse_scheduler/api/v1/iqevc/charging-mode/<site_id>/<sn>/preference
Body: { "mode": "MANUAL_CHARGING" }
Headers: Authorization: Bearer <token>
```
Success response mirrors the GET payload.

Observed mode values include `MANUAL_CHARGING`, `SCHEDULED_CHARGING`,
`GREEN_CHARGING`, and `SMART_CHARGING`.

Notes:
- The bearer source matches `4.1`: manager-token cookie first, stored access token second.

### 4.3 Green Charging Settings (Battery Support)
```
GET /service/evse_scheduler/api/v1/iqevc/charging-mode/GREEN_CHARGING/<site_id>/<sn>/settings
Headers: Authorization: Bearer <token>
```
Response:
```json
{
  "meta": {
    "serverTimeStamp": "2025-01-01T00:00:00.000+00:00",
    "rowCount": 1
  },
  "data": [
    {
      "chargerSettingName": "USE_BATTERY_FOR_SELF_CONSUMPTION",
      "enabled": true,
      "value": null
    }
  ],
  "error": {}
}
```

```
PUT /service/evse_scheduler/api/v1/iqevc/charging-mode/GREEN_CHARGING/<site_id>/<sn>/settings
Headers: Authorization: Bearer <token>
Body: {
  "chargerSettingList": [
    {
      "chargerSettingName": "USE_BATTERY_FOR_SELF_CONSUMPTION",
      "enabled": true,
      "value": null,
      "loader": false
    }
  ]
}
```
Response:
```json
{
  "meta": { "serverTimeStamp": "2025-01-01T00:00:00.000+00:00" },
  "data": {
    "meta": { "serverTimeStamp": "2025-01-01T00:00:00.000+00:00" },
    "data": null,
    "error": {}
  },
  "error": {}
}
```
Notes:
- `USE_BATTERY_FOR_SELF_CONSUMPTION` was the observed setting name for battery supplementation in Green mode.
- Setting `enabled=false` disables battery supplementation; `value` remains `null`.
- Captured requests sent `loader=false`; the API accepts payloads without the `loader` key.
- As with other scheduler endpoints, the current implementation derives bearer auth from the manager-token cookie first and otherwise falls back to the stored access token.

### 4.4 List Schedules
```
GET /service/evse_scheduler/api/v1/iqevc/charging-mode/SCHEDULED_CHARGING/<site_id>/<sn>/schedules
Headers: Authorization: Bearer <token>
```
Response:
```json
{
  "meta": { "serverTimeStamp": "2025-01-01T00:00:00.000+00:00" },
  "data": {
    "config": {
      "isOffPeakEligible": true,
      "scheduleSyncStatus": "synced",
      "isModeCancellable": true,
      "pendingModesOffGrid": false,
      "pendingSchedulesOffGrid": false
    },
    "slots": [
      {
        "id": "<site_id>:<sn>:<uuid>",
        "startTime": "23:00",
        "endTime": "06:00",
        "chargingLevel": 32,
        "chargingLevelAmp": 32,
        "scheduleType": "CUSTOM",
        "days": [1, 2, 3, 4, 5, 6, 7],
        "remindTime": 10,
        "remindFlag": false,
        "enabled": true,
        "recurringKind": "Recurring",
        "chargeLevelType": "Weekly",
        "sourceType": "SYSTEM",
        "reminderTimeUtc": null,
        "serializedDays": null
      },
      {
        "id": "<site_id>:<sn>:<uuid>",
        "startTime": null,
        "endTime": null,
        "chargingLevel": null,
        "chargingLevelAmp": null,
        "scheduleType": "OFF_PEAK",
        "days": [1, 2, 3, 4, 5, 6, 7],
        "remindTime": 10,
        "remindFlag": false,
        "enabled": false,
        "recurringKind": null,
        "chargeLevelType": null,
        "sourceType": "SYSTEM",
        "reminderTimeUtc": null,
        "serializedDays": null
      }
    ]
  },
  "error": {}
}
```
Notes:
- `scheduleType=OFF_PEAK` typically has null `startTime`/`endTime`.
- `days` uses 1=Monday through 7=Sunday.
- `remindFlag` toggles reminders and `remindTime` is minutes before `startTime`.
- Observed: `recurringKind` and `chargeLevelType` may be `null` even for `CUSTOM` slots.
- Observed: `chargingLevel`/`chargingLevelAmp` can be populated for `OFF_PEAK` schedules even when `startTime`/`endTime` are null.
- Observed: `remindTime` may be present even when `remindFlag` is `false`.
- Observed: `reminderTimeUtc` is `HH:MM` when `remindFlag=true`, otherwise null.
- Observed: editing a schedule time in Enlighten auto-enables the slot and populates `reminderTimeUtc`.
- Auth matches the rest of the scheduler family: `Authorization: Bearer <token>` from manager-token cookie first, stored access token second.

### 4.5 Update Schedules
```
PATCH /service/evse_scheduler/api/v1/iqevc/charging-mode/SCHEDULED_CHARGING/<site_id>/<sn>/schedules
Headers: Authorization: Bearer <token>
Body: {
  "meta": { "serverTimeStamp": "2025-01-01T00:00:00.000+00:00", "rowCount": 2 },
  "data": [ <slot>, <slot> ]
}
```
Notes:
- Send the full list of slots; omitted slots may be deleted server-side.
- Preserve unchanged fields like `sourceType`, `recurringKind`, `chargeLevelType`.
- The implementation also supports state-only `PATCH /schedules` with `{ "<slot_id>": "ENABLED" | "DISABLED" }`, single-slot `PATCH /schedule/<slot_id>`, single-slot `POST /schedule`, and `DELETE /schedule/<slot_id>`.
- Slot write payloads are normalized before sending: supported fields are preserved, `scheduleType` defaults to `CUSTOM`, `OFF_PEAK` without days becomes `[1,2,3,4,5,6,7]`, booleans and integer-like fields are coerced, and `datetime.time` values are serialized as `HH:MM`.
- Observed: captured PATCH requests may include `chargingLevel=100` and `chargingLevelAmp=null` for `CUSTOM` schedules; subsequent GETs may normalize back to `32/32`.
- Observed: captured PATCH requests include a top-level `"error": {}` field; the API accepts PATCH payloads without it.
- Auth matches the rest of the scheduler family: `Authorization: Bearer <token>` from manager-token cookie first, stored access token second.
---

## 5. BatteryConfig APIs (System Profile and Battery Controls)

The BatteryConfig service exposes system profile and EV charging mode endpoints.

Observed shared requirements:
- Reads and writes follow the official homeowner web app request shape rather than the bearer/e-auth/cookie overlay used by other endpoint families.
- Shared BatteryConfig baseline headers are:
  - `Accept: application/json, text/plain, */*`
  - `Origin: https://battery-profile-ui.enphaseenergy.com`
  - `Referer: https://battery-profile-ui.enphaseenergy.com/`
  - `User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36`
  - `Username: <user_id>` when the active auth context exposes a usable user id
- Observed successful first-party variants are:
  - primary variant: baseline headers plus `e-auth-token` and `requestid`
  - lean fallback variant: baseline headers without `e-auth-token` and `requestid`
- The current implementation explicitly suppresses inherited `Authorization`, `Cookie`, `X-CSRF-Token`, and `X-Requested-With` on BatteryConfig requests so those headers do not leak in from the general Enlighten client state.
- Write flows acquire a fresh `BP-XSRF-Token` first, then send `X-XSRF-Token` on both the `isValid` preflight and the follow-up write.
- `GET /batterySettings/<site_id>` uses `source=enlm`; writes still use `source=enho`.

### 5.0 AC Battery cloud UI routes

Legacy AC Battery systems expose runtime discovery, telemetry, and sleep-mode control through the Enlighten web UI rather than the JSON BatteryConfig service.

Observed integration rules:
- `BatteryConfig siteSettings.hasAcb=true` is the canonical capability signal.
- Runtime enumeration and control state still come from the HTML Devices page because BatteryConfig does not expose per-battery identifiers or current sleep state.
- These are optional browser-style endpoints and should be handled with tolerant support-state / backoff logic.
- CSS/control classes are normalized as `sleep -> off`, `cancel -> pending`, and `wake -> on`.
- Enabling sleep sends the request to every parsed AC Battery row with a `battery_id`, defaulting `sleep_min_soc` to `20` when no selected value is known.
- Changing target state of charge while sleep is `on` or `pending` immediately reissues the sleep command with the new lower-bound value.

#### Devices page

```http
GET /systems/<site_id>/devices?status=active
```

Observed usage:
- Returns the Devices page HTML.
- AC Battery rows expose serial, part number, phase, state of charge, charge cycles, status text, sleep/wake control state, and links of the form `/systems/<site_id>/ac_batteries/<battery_id>`.
- The current integration uses this page to enumerate batteries, resolve `battery_id`, read sleep state, and read the currently selected `sleep_min_soc` band.

#### Detail page

```http
GET /systems/<site_id>/ac_batteries/<battery_id>
```

Observed usage:
- Returns the per-battery HTML page.
- Useful as the referer/anchor page for telemetry and diagnostics.

#### Telemetry fragment

```http
GET /systems/<site_id>/ac_batteries/<battery_id>/show_stat_data
```

Observed usage:
- Returns an HTML fragment, typically fetched with `X-Requested-With: XMLHttpRequest`.
- Observed fields include instantaneous power, operating mode, state of charge, charge cycles, and the last reported timestamp.

#### Sleep control

```http
GET /systems/<site_id>/ac_batteries/<battery_id>/sleep?sleep_min_soc=<value>
```

Observed usage:
- Requests sleep mode for the target AC Battery.
- `sleep_min_soc` is the lower bound of the selected target state-of-charge band.
- Observed accepted values are discrete 5% steps from `0` through `95`.
- Success is observed as HTTP `302` redirecting back to the Devices page.

#### Wake control

```http
GET /systems/<site_id>/ac_batteries/<battery_id>/wake
```

Observed usage:
- Requests wake mode or cancels a pending sleep transition.
- Success is observed as HTTP `302` redirecting back to the Devices page.

#### Events page

```http
GET /systems/<site_id>/ac_batteries/<battery_id>/events
```

Observed usage:
- Returns HTML event/history content for the target battery.
- The current integration only uses this route for diagnostics; it does not expose event/history entities.

### 5.1 MQTT Signed URL / Authorizer Bootstrap
```
GET /service/batteryConfig/api/v1/mqttSignedUrl/<site_id>
```
Returns an AWS IoT custom authorizer payload used to open a short-lived MQTT response stream for BatteryConfig write flows.

Example response (anonymized):
```json
{
  "topic": "v1/server/response-stream/<stream_id>",
  "stream_duration": 900,
  "aws_iot_endpoint": "a1b2c3d4e5f6g7-ats.iot.us-east-1.amazonaws.com",
  "aws_authorizer": "aws-lambda-authoriser-prod",
  "aws_token_key": "enph_token",
  "aws_token_value": "<session_id>",
  "aws_digest": "<base64_signature>"
}
```

Observed usage:
- MQTT WebSocket URL is `wss://<aws_iot_endpoint>/mqtt`.
- MQTT authentication is carried in the MQTT `username` field rather than the WebSocket query string:
  `?x-amz-customauthorizer-name=<aws_authorizer>&<aws_token_key>=<aws_token_value>&site-id=<site_id>&x-amz-customauthorizer-signature=<urlencoded aws_digest>&env=production`
- The integration uses MQTT 3.1.1 (`protocolVersion: 4`), no password, and `Origin: https://battery-profile-ui.enphaseenergy.com`.
- The returned `topic` is treated as the response stream subscription topic before issuing the matching battery-settings `PUT`.
- A 60-second local probe subscribed to the returned `v1/server/response-stream/<stream_id>` topic without issuing a paired write and received no passive telemetry. Use `2.9.2.a` for the separate web-app Live Status telemetry stream.

### 5.2 Site Settings
```
GET /service/batteryConfig/api/v1/siteSettings/<site_id>?userId=<user_id>
```
Provides feature and availability flags for the battery profile service.

Example response (anonymized):
```json
{
  "type": "site-settings",
  "timestamp": "<timestamp>",
  "data": {
    "showProduction": true,
    "showConsumption": true,
    "hasEncharge": true,
    "hasAcb": false,
    "hasGenerator": false,
    "hasEnpower": true,
    "countryCode": "AU",
    "region": "AU",
    "locale": "en-AU",
    "ownerOrHostMaskedEmail": "u*******r@example.com",
    "timezone": "Australia/Melbourne",
    "showChargeFromGrid": false,
    "isEnsemble": true,
    "hasOjasDevice": false,
    "showSavingsMode": true,
    "showAiOptiSavingsMode": true,
    "siteStage": 5,
    "isEmea": false,
    "isDTSupported": false,
    "isIQGWScheduleSupported": true,
    "isDTEnabled": false,
    "isDTSite": false,
    "restrictCfg": false,
    "isHemsActivationPending": true,
    "isTariffNEM3": false,
    "tariffTypeId": {
      "importType": "tou",
      "exportType": "tou"
    },
    "isHemsAuthPending": false,
    "isHemsSite": false,
    "isNEM3Supported": false,
    "isNEM3Site": false,
    "calibrationProgress": false,
    "acceptedGICDisclaimer": false,
    "featureDetails": {
      "98daf27c08a25a0a": false,
      "3951a76025fc7d6c": false,
      "HEMS_EV_Custom_Schedule": true,
      "a3f9c1e7b82d4a6f": false,
      "554ed34b3f489c08": true,
      "a56e30d62ecc443f": true,
      "f3a9b7c4d681e25f": true,
      "Disable_Storm_Guard_Grid_Charging": false,
      "714ed62b9f489c98": true,
      "2e6W81086bd123F8": false,
      "ae9fc99447fa52a0": true,
      "734ed62b3f489c98": true,
      "b7def45a12d67e3f": false,
      "af356d446af0e3b4": false,
      "dtpfu43qwqxr5327": false,
      "b707fae2750a4965": true,
      "2e6W81086cd123F9": false,
      "671b7d2591d1adc1": true,
      "pm221c316d32ad3e": false,
      "00eb0092160e4279": false,
      "61df86665efcb677": false,
      "a8cbe12f09b34c1d": true,
      "534ed33b3f489c98": true,
      "66f7a67f71be52f7": true
    },
    "showStormGuard": true,
    "showCriticalAlertsOverride": false,
    "showVLS": true,
    "isIQ8site": false,
    "showFullBackup": true,
    "showBatteryBackupPercentage": true,
    "isCollarPresent": false,
    "isCollarInstalled": false,
    "userDetails": {
      "isHost": false,
      "isOwner": true,
      "isInstaller": false,
      "isMaintainer": false,
      "email": "u******r@example.com",
      "isDemoUser": false
    },
    "siteStatus": {
      "code": "normal",
      "text": "Normal",
      "severity": "warning"
    },
    "isChargingModesEnabled": true,
    "isUseBatteryForEVSESupported": false,
    "batteryGridMode": "ImportExport",
    "batteryLimitSupport": false,
    "isHemsOptScheduleSupported": true,
    "isChangePending": false,
    "isThermostatSmartModeAllowed": false
  }
}
```

Observed field behavior:
- `countryCode="DE"`, `region="DE"`, `locale="en-AU"`, and `timezone="Europe/Berlin"` can legitimately coexist when the account language differs from the site locale.
- `isEmea=true`, `isDTSite=true`, and `isDTSupported=true` were observed together while `isDTEnabled=false`.
- `showChargeFromGrid=false` can coexist with `isChargingModesEnabled=true`, `batteryGridMode="ImportExport"`, `isDTSupported=true`, `isDTSite=true`, `isIQGWScheduleSupported=true`, and `isHemsOptScheduleSupported=true`; do not infer DTG/RBD availability from the CFG visibility flag.
- `isHemsSite=true`, `isHemsActivationPending=false`, `isHemsAuthPending=false`, and `isHemsOptScheduleSupported=true` were present on the same site.
- `siteStatus.code="normal"` and `siteStatus.text="Normal"` coexisted with `siteStatus.severity="warning"`.
- `batteryGridMode` was observed as `ImportExport`.
- `acceptedGICDisclaimer=true` and `isChangePending=false` were observed together on a site with active DTG/RBD schedules.
- `featureDetails` mixes opaque rollout keys with readable flags such as `HEMS_EV_Custom_Schedule` and `Disable_Storm_Guard_Grid_Charging`; preserve unknown keys verbatim and record new values instead of filtering them out.

### 5.3 Profile Details (System + EVSE)
```
GET /service/batteryConfig/api/v1/profile/<site_id>?source=enho&userId=<user_id>&locale=<locale>
GET /service/batteryConfig/api/v1/profile/<site_id>?userId=<user_id>
```
Returns the active system profile plus embedded EVSE configuration used to render the EV charging card.
Both URL variants were observed; one request included `source=enho&locale=en-AU`, while another omitted them and still returned the same schema.

Example response:
```json
{
  "type": "profile-details",
  "timestamp": "<timestamp>",
  "data": {
    "supportsMqtt": true,
    "pollingInterval": 60,
    "drEventActive": false,
    "drEventMode": "",
    "profile": "self-consumption",
    "requestedConfig": {},
    "requestedConfigMqtt": {},
    "isTariffTou": false,
    "isBuybackTariffTou": false,
    "buybackExportPlan": "netFit",
    "batteryBackupPercentage": 5,
    "stormGuardState": "enabled",
    "acceptedStormGuardDisclaimer": false,
    "showStormGuardAlert": false,
    "devices": {
      "thirdPartyEvse": [],
      "iqEvse": [
        {
          "uuid": "<evse_uuid>",
          "deviceName": "IQ EV Charger_XXXX",
          "profile": "self-consumption",
          "profileConfig": "full",
          "enable": false,
          "status": 1,
          "chargeMode": "GREEN",
          "activeSchedules": [],
          "updatedAt": 1772529741
        }
      ],
      "thirdPartyWaterHeater": []
    },
    "systemTask": false,
    "batteryBackupPercentageMax": 100,
    "batteryBackupPercentageMin": 5,
    "veryLowSoc": 5,
    "previousBatteryBackupPercentage": {
      "self-consumption": 30,
      "cost_savings": 30,
      "backup_only": 100,
      "expert": 30,
      "ai_optimisation": 5
    },
    "dtgControl": {
      "show": true,
      "showDaySchedule": true,
      "enabled": false,
      "locked": false,
      "scheduleSupported": true,
      "startTime": 960,
      "endTime": 1140
    },
    "cfgControl": {
      "show": true,
      "showDaySchedule": true,
      "enabled": false,
      "locked": false,
      "scheduleSupported": true,
      "forceScheduleSupported": true,
      "forceScheduleOpted": true
    },
    "rbdControl": {
      "show": true,
      "showDaySchedule": true,
      "enabled": false,
      "locked": false,
      "scheduleSupported": true
    },
    "evseStormEnabled": false,
    "isIQGWScheduleSupported": true,
    "appTutorialUrl": "<tutorial_id>",
    "isBatteryChangePending": false
  }
}
```

Observed field behavior:
- `profile` and `devices.iqEvse[].profile` were both `self-consumption`.
- `devices.iqEvse[].chargeMode` was `GREEN`; preserve new charge-mode strings verbatim.
- `devices.iqEvse[].status` was `1`, `profileConfig` was `full`, `enable` was `false`, and `activeSchedules` was an empty array.
- `buybackExportPlan="netFit"` was present even while `isBuybackTariffTou=false`.
- `stormGuardState="enabled"` coexisted with `acceptedStormGuardDisclaimer=false` and `showStormGuardAlert=false`.
- `previousBatteryBackupPercentage` included `self-consumption=30`, `cost_savings=30`, `backup_only=100`, `expert=30`, and `ai_optimisation=5`.
- `dtgControl.startTime=960` and `dtgControl.endTime=1140` indicate minute-of-day scheduling windows.
- `cfgControl.forceScheduleSupported=true` and `cfgControl.forceScheduleOpted=true` were both present in the same payload.

### 5.4 System Profile Updates (Site Profile)
```
PUT /service/batteryConfig/api/v1/profile/<site_id>?userId=<user_id>
Headers: X-XSRF-Token: <token>
```
Updates the system profile and reserve percentage. Observed profile keys include
`self-consumption`, `cost_savings`, `backup_only`, and `ai_optimisation`.

Implementation auth notes:
- The current client first attempts `GET /service/batteryConfig/api/v1/siteSettings/<site_id>?userId=<user_id>` and reads the `x-csrf-token` response header.
- If that GET does not yield a token, it falls back to `POST /service/batteryConfig/api/v1/battery/sites/<site_id>/schedules/isValid`, where it accepts `x-csrf-token`, `Set-Cookie`, response cookies, or the session cookie jar.
- If the `isValid` bootstrap returns `4xx`, the client still checks error-response cookies and the session cookie jar for `BP-XSRF-Token`, then tries a final cookie-based `GET /service/batteryConfig/api/v1/siteSettings/<site_id>` bootstrap before keeping any existing token.
- On affected issue `#460` sites, the verified working write uses the raw stored BatteryConfig cookie (`Cookie`), `e-auth-token` from `enlighten_manager_token_production`, `Username`, `X-XSRF-Token` from the raw cookie header, and `X-Requested-With: XMLHttpRequest`.
- That cookie-backed write must be sent from a stateless request session so aiohttp does not merge cookie-jar state into the raw `Cookie` header.
- Verified write planner order:
  - preferred compatibility request on affected sites: `PUT /profile/<site_id>?userId=<user_id>&source=enho` with the raw-cookie browser shape
  - first compatibility retry: same request without `devices` when the payload included an EVSE `devices` list
  - second compatibility retry: same stripped payload without `source`
  - later retries: official-web primary, official-web lean, then mixed-auth browser shape when auth material is available
- Direct live verification also succeeded without `source`.

Example payloads observed:
```json
{ "profile": "self-consumption", "batteryBackupPercentage": 10 }
```

```json
{
  "profile": "self-consumption",
  "batteryBackupPercentage": 20,
  "devices": [
    {
      "uuid": "<evse_uuid>",
      "profileConfig": "full",
      "chargeMode": "SMART",
      "deviceType": "iqEvse",
      "enable": false
    }
  ]
}
```

```json
{
  "profile": "cost_savings",
  "operationModeSubType": "prioritize-energy",
  "batteryBackupPercentage": 20,
  "devices": [
    {
      "uuid": "<evse_uuid>",
      "chargeMode": "MANUAL",
      "deviceType": "iqEvse",
      "enable": false
    }
  ]
}
```

```json
{
  "profile": "backup_only",
  "batteryBackupPercentage": 100,
  "devices": [
    {
      "uuid": "<evse_uuid>",
      "chargeMode": "MANUAL",
      "deviceType": "iqEvse",
      "enable": false
    }
  ]
}
```

```json
{
  "profile": "ai_optimisation",
  "batteryBackupPercentage": 10,
  "operationModeSubType": "prioritize-energy",
  "devices": [
    {
      "uuid": "<evse_uuid>",
      "chargeMode": "MANUAL",
      "deviceType": "iqEvse",
      "enable": false
    }
  ]
}
```

Response:
```json
{ "message": "success" }
```

Notes:
- Verified working profile response body remains the compact success envelope:
  - `{"message":"success"}`
- The reserve slider enforces a 10% minimum (Self-Consumption) and 100% for Full Backup. The Savings profile uses a reserve slider plus a "Use battery after peak hours" toggle; `operationModeSubType` appears to track this state (only `prioritize-energy` observed so far).
- The AI Optimization flow uses the profile key `ai_optimisation` and also sends `operationModeSubType: "prioritize-energy"` with the EVSE device payload.
- Switching away from AI Optimization may still include an EVSE `devices` payload. In the observed AI -> Self-Consumption transition, the EVSE payload included `profileConfig: "full"` and `chargeMode: "SMART"`.
- After saving a mode change, the profile may remain in a pending state until the change takes effect. During this window, a separate cancel endpoint can be used.

```
PUT /service/batteryConfig/api/v1/cancel/profile/<site_id>?userId=<user_id>
Headers: X-XSRF-Token: <token>
Body: {}
```
Cancels a pending profile change. The request body is an empty JSON object.

Implementation auth notes:
- The current client treats this as another official-web-style BatteryConfig write: acquire fresh XSRF first, then send the BatteryConfig `Accept`/`Username`/`Origin`/`Referer`/Chrome-`User-Agent` shape plus `X-XSRF-Token`.
- Inherited `Authorization`, `e-auth-token`, `Cookie`, `X-CSRF-Token`, and `X-Requested-With` are explicitly suppressed here as well.

Example response:
```json
{ "message": "success" }
```

### 5.5 Battery Settings (Battery Details)
```
GET /service/batteryConfig/api/v1/batterySettings/<site_id>?source=enlm&userId=<user_id>
```
Returns battery configuration details for the Battery page (battery mode, charge-from-grid settings, shutdown level).

Example response (anonymized):
```json
{
  "type": "battery-details",
  "timestamp": "<timestamp>",
  "data": {
    "supportsMqtt": true,
    "pollingInterval": 60,
    "drEventActive": false,
    "drEventMode": "",
    "profile": "self-consumption",
    "batteryBackupPercentage": 5,
    "requestedConfigMqtt": {},
    "requestedConfig": {},
    "stormGuardState": "enabled",
    "showStormGuardAlert": false,
    "acceptedItcDisclaimer": "<timestamp>",
    "hideChargeFromGrid": true,
    "envoySupportsVls": true,
    "chargeBeginTime": 120,
    "chargeEndTime": 300,
    "batteryGridMode": "ImportExport",
    "veryLowSoc": 5,
    "veryLowSocMin": 5,
    "veryLowSocMax": 25,
    "chargeFromGrid": false,
    "chargeFromGridScheduleEnabled": true,
    "batteryBackupPercentageMax": 100,
    "batteryBackupPercentageMin": 5,
    "previousBatteryBackupPercentage": {
      "self-consumption": 30,
      "cost_savings": 30,
      "backup_only": 100,
      "expert": 30,
      "ai_optimisation": 5
    },
    "systemTask": false,
    "dtgControl": {
      "show": true,
      "showDaySchedule": true,
      "enabled": false,
      "locked": false,
      "scheduleSupported": true,
      "startTime": 960,
      "endTime": 1140
    },
    "cfgControl": {
      "show": true,
      "showDaySchedule": true,
      "enabled": false,
      "locked": false,
      "scheduleSupported": true,
      "forceScheduleSupported": true,
      "forceScheduleOpted": true
    },
    "rbdControl": {
      "show": true,
      "showDaySchedule": true,
      "enabled": false,
      "locked": false,
      "scheduleSupported": true
    },
    "devices": {
      "iqEvse": { "useBatteryFrSelfConsumption": true }
    }
  }
}
```

```
PUT /service/batteryConfig/api/v1/batterySettings/<site_id>?userId=<user_id>&source=enho
Headers:
  Username: <user_id>
  X-XSRF-Token: <token>
  Origin: https://battery-profile-ui.enphaseenergy.com
  Referer: https://battery-profile-ui.enphaseenergy.com/
  User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36
```
Updates battery settings. Captured requests used partial payloads to change individual controls.

Implementation auth notes:
- The current client first attempts `GET /service/batteryConfig/api/v1/siteSettings/<site_id>?userId=<user_id>` and reads the `x-csrf-token` response header.
- If that GET does not yield a token, it falls back to `POST /battery/sites/<site_id>/schedules/isValid`, where it accepts `x-csrf-token`, `Set-Cookie`, response cookies, or the session cookie jar.
- If the `isValid` bootstrap returns `4xx`, the client still checks error-response cookies and the session cookie jar for `BP-XSRF-Token`, then tries a final cookie-based `GET /service/batteryConfig/api/v1/siteSettings/<site_id>` bootstrap before keeping any existing token.
- On affected issue `#460` sites, the verified working write uses the raw stored BatteryConfig cookie (`Cookie`), `e-auth-token` from `enlighten_manager_token_production`, `Username`, `X-XSRF-Token` from the raw cookie header, and `X-Requested-With: XMLHttpRequest`.
- That cookie-backed write must be sent from a stateless request session so aiohttp does not merge cookie-jar state into the raw `Cookie` header.
- Verified write planner order:
  - preferred compatibility request on affected sites: `PUT /batterySettings/<site_id>?userId=<user_id>&source=enho` with the raw-cookie browser shape
  - on `supportsMqtt=true` sites, next retries drop `source`
  - later retries: official-web primary, official-web lean, then mixed-auth browser shape when auth material is available
  - if the payload includes `acceptedItcDisclaimer` and prior attempts still fail, the final compatibility retry sends `acceptedItcDisclaimer: true` instead of the timestamp string; on `supportsMqtt=true` sites this final retry follows the no-`source` path

Example payloads observed:
```json
{ "chargeFromGrid": false }
```

```json
{
  "chargeFromGrid": true,
  "acceptedItcDisclaimer": "<timestamp>",
  "chargeBeginTime": 120,
  "chargeEndTime": 300,
  "chargeFromGridScheduleEnabled": true
}
```

```json
{ "veryLowSoc": 15 }
```

```json
{ "rbdControl": { "enabled": false } }
```

```json
{
  "rbdControl": {
    "show": true,
    "showDaySchedule": true,
    "enabled": true,
    "locked": false,
    "scheduleSupported": true
  }
}
```

```json
{
  "profile": "self-consumption",
  "batteryBackupPercentage": 21,
  "requestedConfig": {},
  "requestedConfigMqtt": {},
  "stormGuardState": "disabled",
  "showStormGuardAlert": false,
  "acceptedItcDisclaimer": "2026-04-17T10:41:56.440Z",
  "hideChargeFromGrid": true,
  "envoySupportsVls": true,
  "chargeBeginTime": 120,
  "chargeEndTime": 300,
  "batteryGridMode": "ImportExport",
  "veryLowSoc": 10,
  "veryLowSocMin": 10,
  "veryLowSocMax": 25,
  "chargeFromGrid": true,
  "chargeFromGridScheduleEnabled": false,
  "batteryBackupPercentageMax": 100,
  "batteryBackupPercentageMin": 10,
  "systemTask": false,
  "dtgControl": {
    "show": true,
    "showDaySchedule": true,
    "enabled": false,
    "locked": false,
    "scheduleSupported": true,
    "startTime": 0,
    "endTime": 1439
  },
  "cfgControl": {
    "show": true,
    "showDaySchedule": true,
    "enabled": true,
    "locked": false,
    "scheduleSupported": true,
    "forceScheduleSupported": true,
    "forceScheduleOpted": false
  },
  "rbdControl": {
    "show": true,
    "showDaySchedule": true,
    "enabled": false,
    "locked": false,
    "scheduleSupported": true
  }
}
```

Response:
```json
{ "message": "success" }
```

Notes:
- Verified working battery-settings response body remains the compact success envelope:
  - `{"message":"success"}`
- Direct live verification on the affected site also accepted each of these write bodies with the raw-cookie request shape:
  - `{"chargeFromGrid":false}`
  - `{"chargeFromGrid":true}`
  - `{"chargeFromGrid":true,"acceptedItcDisclaimer":"<timestamp>"}`
  - `{"chargeFromGrid":true,"acceptedItcDisclaimer":true}`
- The raw capture contained live cookies, JWTs, XSRF tokens, site IDs, and user IDs. Only the endpoint shape and sanitized field values are recorded here.
- `supportsMqtt` and `pollingInterval` indicate whether the page can use the MQTT-backed battery settings flow and how often the UI expects state refreshes. Observed values so far: `supportsMqtt=true`, `pollingInterval=60`.
- `requestedConfig` and `requestedConfigMqtt` were empty objects in the capture; they appear to be placeholders for pending configuration writes or async acknowledgements. Observed values so far: `{}` for both fields.
- `drEventActive` / `drEventMode` look like demand-response state flags; observed values so far: `false` / `""`.
- `profile` is the backend battery profile code. Observed values in this endpoint so far: `self-consumption`.
- `stormGuardState` is the backend Storm Guard state. Observed values in captures so far: `enabled`, `disabled`.
- `showStormGuardAlert` is a UI flag. Observed value so far: `false`.
- `batteryGridMode` matches the Battery Mode card ("ImportExport" renders as "Import and Export") and is controlled by interconnection settings. Observed value so far: `ImportExport`.
- `batteryBackupPercentage` is the active reserve percentage. Observed values in captures so far: `5`, `20`.
- `batteryBackupPercentageMin` / `batteryBackupPercentageMax` expose the allowed reserve slider bounds, while `previousBatteryBackupPercentage` preserves the last reserve value used for each profile. Observed bounds so far: `5` and `100`.
- `chargeFromGrid` backs the "Charge battery from the grid" toggle. Enabling it shows a disclaimer dialog; the confirmation sets `acceptedItcDisclaimer` and unlocks the schedule controls.
- Observed `chargeFromGrid` values so far: `true`, `false`.
- The schedule checkbox ("Also up to 100% during this schedule") is represented by `chargeFromGridScheduleEnabled`; `chargeBeginTime`/`chargeEndTime` are minutes after midnight (local). Observed values so far: `chargeFromGridScheduleEnabled=true`, `chargeBeginTime=120`, `chargeEndTime=300`.
- When the schedule is enabled, the status payload reports `chargeFromGridScheduleEnabled: true` and `cfgControl.forceScheduleOpted: true`.
- Live local verification for issue `#634` on 2026-05-02 showed the final-CFG-delete path needs a follow-up `PUT /batterySettings/<site_id>` disable write even when no replacement CFG window remains. The backend accepted a payload with `chargeFromGridScheduleEnabled:false`, the current `chargeFromGrid` value, `acceptedItcDisclaimer`, and `cfgControl.forceScheduleOpted:false` while omitting `chargeBeginTime` and `chargeEndTime`; the follow-up read returned zero CFG schedules with `chargeFromGridScheduleEnabled:false`.
- First-party browser captures documented for this repository used `acceptedItcDisclaimer: true`, while subsequent reads returned a timestamp string; the backend normalizes the acknowledgement state internally.
- The current integration uses both shapes: direct charge-from-grid enablement sends boolean `true` after calling `acceptDisclaimer`, while CFG schedule/settings payloads send the stored timestamp or a current UTC timestamp.
- `veryLowSoc` drives the "Battery shutdown level" slider, clamped between `veryLowSocMin` and `veryLowSocMax`. Observed values so far: `veryLowSoc=5` and `15`, `veryLowSocMin=5` and `10`, `veryLowSocMax=25`.
- `dtgControl`, `cfgControl`, and `rbdControl` are per-feature UI capability blocks. In the homeowner capture they each exposed `show`, `enabled`, `locked`, and schedule-support fields even though the corresponding toggles were off. Observed booleans so far: `show=true`, `showDaySchedule=true`, `enabled=false`, `locked=false`, `scheduleSupported=true`, plus `cfgControl.forceScheduleSupported=true` and `cfgControl.forceScheduleOpted=true`.
- Later captures showed `dtgControl.enabled=true` and `rbdControl.enabled=true` while `dtgControl.forceScheduleSupported` / `rbdControl.forceScheduleSupported` remained absent or `null`; schedule-family toggles should not assume CFG-style `forceScheduleSupported` metadata is present for DTG/RBD.
- `hideChargeFromGrid` may be `true` even when charge-from-grid schedule fields are still present in the payload, so clients should not infer field absence from UI visibility. Observed values so far: `true`, `false`.
- `systemTask` remained `false` in the capture and likely flags backend-owned operations that temporarily lock manual changes. Observed value so far: `false`.
- `devices.iqEvse.useBatteryFrSelfConsumption` exposes whether an IQ EV charger can draw from battery during self-consumption mode. Observed value so far: `true`.
- Two equivalent write variants were observed:
  - REST-only flows use `PUT /batterySettings/<site_id>?source=enho&userId=<user_id>`.
  - MQTT-backed RBD flows on `supportsMqtt=true` systems use `PUT /batterySettings/<site_id>?userId=<user_id>` after opening the MQTT response stream.
- First-party homeowner captures used a lean official-web request with no `Authorization`, `Cookie`, `X-CSRF-Token`, or `X-Requested-With`.
- Direct live verification for issue `#460` showed those first-party shapes still returning `403` on the affected site, while the raw-cookie browser request succeeded.
- In the current implementation, BatteryConfig variant selection is cached per `(site_id, battery_config_user_id, endpoint_family)` after a successful write so later calls reuse the proven request shape instead of retrying both variants each time.
- Additional partial payloads were observed on the same endpoint for DTG/RBD enablement toggles:
  - `{"dtgControl":{"enabled":true}}`
  - `{"dtgControl":{"enabled":false}}`
  - `{"rbdControl":{"enabled":true}}`
  - `{"rbdControl":{"enabled":false}}`
- The current integration builds DTG/RBD toggle payloads with `enabled`, copies available capability fields (`show`, `locked`, `showDaySchedule`, `scheduleSupported`, `forceScheduleSupported`, `forceScheduleOpted`), and includes minute-of-day `startTime`/`endTime` when a current control window is known.
- RBD enable requires an exposed `rbdControl` plus an existing schedule id/start/end. If a fresh read has `rbdControl` absent/null and no RBD schedule record, the integration treats RBD as off and blocks blind enablement until a real schedule is created.
- Live local verification on 2026-04-18 showed the RBD control path is asymmetric on the affected `supportsMqtt=true` site:
  - RBD enable worked with the richer control payload that echoed the current capability fields:
    - `{"rbdControl":{"show":true,"showDaySchedule":true,"enabled":true,"locked":false,"scheduleSupported":true}}`
  - RBD disable did **not** apply when sent as the partial payload `{"rbdControl":{"enabled":false}}`, even though the endpoint returned `200 OK`.
  - RBD disable did apply when the request used the merged battery-settings payload, omitted `source`, and stripped `devices` from the body.
- Later live local verification on 2026-04-18 also showed a stricter read-side edge case on the same site state:
  - `GET /batterySettings/<site_id>` returned `rbdControl: null`
  - `GET /battery/sites/<site_id>/schedules` returned `rbd.count=0` and `rbd.scheduleStatus="active"`
  - in that state the backend should be treated as effectively **off**, not as an unknown or partially-on RBD state
- Because of that read shape, clients should not rely on stale local `rbdControl.enabled` values once a fresh `batterySettings` read omits `rbdControl`. A safe reconciliation rule is:
  - if `rbdControl` is absent/null and there is no RBD schedule record (`rbd.count=0`, no `details[]` entry), treat RBD as off
  - if a user tries to enable RBD in that state, require them to create an RBD schedule first rather than issuing a blind toggle write
- Because of that live result, clients should not assume the minimal partial `rbdControl.enabled=false` payload is authoritative on every site. On affected sites, the safe non-MQTT fallback is:
  - `PUT /batterySettings/<site_id>?userId=<user_id>`
  - body = merged current battery-settings payload
  - remove `devices`
  - set `rbdControl.enabled=false`
- External client source and the current integration use a richer DTG enable payload when no DTG schedule record exists yet:
  - `{"dtgControl":{"enabled":true,"scheduleSupported":true,"startTime":<minutes>,"endTime":<minutes>}}`
  - `startTime` / `endTime` are minute-of-day integers derived from the current DTG control window
- When a DTG schedule already exists, the integration still uses the same battery-settings toggle family for on/off writes and leaves schedule create/update/delete to the `/battery/sites/<site_id>/schedules` endpoints.

### 5.5.1 Dry Contact Settings
```
GET /pv/settings/<site_id>/dry_contacts
```
Returns dry-contact load settings for gateway-managed auxiliary loads.

Implementation notes:
- The integration treats this as an optional history-family JSON endpoint using normal authenticated Enlighten headers.
- Payloads are normalized from nested objects/lists into dry-contact entries and matched back to inventory dry-contact members when possible.
- Recognized fields include identity, override state, control mode, polling interval, SOC threshold bounds, and schedule windows; unmatched entries are retained for diagnostics.

### 5.6 Storm Guard Alert Status, Opt-Out, and Toggle
```
GET /service/batteryConfig/api/v1/stormGuard/<site_id>/stormAlert
```
Returns Storm Guard alert state and critical alert override status.

Example response with no active alerts (anonymized):
```json
{
  "criticalAlertsOverride": true,
  "stormAlerts": [],
  "criticalAlertActive": false
}
```

Example response with an active non-critical alert (anonymized):
```json
{
  "criticalAlertsOverride": true,
  "stormAlerts": [
    {
      "id": "<alert_id>",
      "name": "Severe Weather",
      "source": "<weather_provider>",
      "status": "active",
      "startTime": 1771895761000,
      "endTime": 1771920000000,
      "critical": false
    }
  ],
  "criticalAlertActive": false
}
```

```
PUT /service/batteryConfig/api/v1/stormGuard/<site_id>/stormAlert
Headers: X-XSRF-Token: <token>
Body: {
  "stormAlerts": [
    { "id": "<alert_id>", "name": "Severe Weather", "status": "opted-out" }
  ]
}
```
Opts out of a specific active Storm Guard alert.

Implementation auth notes:
- The client routes this through the same compatibility pipeline as other
  BatteryConfig writes. It acquires a fresh XSRF token before official-web
  attempts and retries compatible authentication shapes after HTTP `403`.
- When the stored browser-session cookie contains its matching XSRF token, the
  cookie plus `e-auth-token` variant is preferred and reuses that pair without
  replacing the token first.
- Fallback attempts include the primary first-party `e-auth-token` + `requestid`
  shape, the lean first-party shape, and mixed cookie/bearer compatibility where
  the available credentials support them. Successful shapes are cached separately
  for the Storm Alert endpoint family.

Example response:
```json
{ "message": "success" }
```

```
PUT /service/batteryConfig/api/v1/stormGuard/toggle/<site_id>?userId=<user_id>
Headers: X-XSRF-Token: <token>
Body: {
  "stormGuardState": "enabled",
  "evseStormEnabled": true
}
```
Updates the Storm Guard toggle and the EV charging checkbox shown in the Storm Guard modal.

Example responses:
```json
{ "message": "success" }
```

Notes:
- `stormGuardState` accepts `enabled` or `disabled`.
- `evseStormEnabled` controls EV charging behavior during Storm Guard alerts.
- Alert opt-out uses `PUT /stormGuard/<site_id>/stormAlert` with `status: "opted-out"` per alert ID.
- Observed behavior: if that opt-out removes the last active Storm Alert and Storm Guard remains enabled, the system profile exits storm-driven Full Backup and returns to the normal configured profile.
- Once enabled, the profile automatically switches to Full Backup during severe weather alerts and reserves full battery capacity.
- The current client sends this write with the same BatteryConfig write flow as other mutations: acquire XSRF first, then send the official-web BatteryConfig shape plus `X-XSRF-Token`, using the primary `e-auth-token` + `requestid` variant first and the lean fallback after `403`.

### 5.7 Third-Party Control Settings
```
GET /service/batteryConfig/api/v1/<site_id>/thirdPartyControlSettings
```
Returns additional battery-profile control settings.

Example response (anonymized):
```json
{
  "type": "third-party-control-settings",
  "timestamp": "2026-03-08T09:40:03.684055096Z",
  "data": {}
}
```

Observed structure:
- The captured site returned an empty `data` object, so field semantics remain unknown.

### 5.8 Battery Schedules
```
GET /service/batteryConfig/api/v1/battery/sites/<site_id>/schedules
```
Returns the charge-from-grid / day-schedule configuration backing the Battery page.

Example response (anonymized):
```json
{
  "type": "BATTERY_SCHEDULES_CONFIG",
  "cfg": {
    "scheduleStatus": "active",
    "count": 1,
    "details": [
      {
        "createdBy": "<user_id>",
        "updatedBy": "<user_id>",
        "createdAt": "1769455003182",
        "updatedAt": "1769455103104",
        "scheduleId": "<schedule_uuid>",
        "timezone": "Region/City",
        "startTime": "20:00",
        "endTime": "05:00",
        "limit": 100,
        "scheduleType": "CFG",
        "scheduleStatus": "active",
        "days": [1, 2, 3, 4, 5, 6, 7],
        "isDeleted": false,
        "isEnabled": false
      }
    ]
  },
  "dtg": {
    "scheduleStatus": "active",
    "count": 1,
    "details": [
      {
        "scheduleId": "<schedule_uuid>",
        "timezone": "Region/City",
        "startTime": "18:00",
        "endTime": "23:59",
        "limit": 5,
        "scheduleType": "DTG",
        "scheduleStatus": "active",
        "days": [1, 2, 3, 4, 5, 6, 7],
        "isDeleted": false,
        "isEnabled": true
      }
    ]
  },
  "rbd": {
    "scheduleStatus": "active",
    "count": 1,
    "details": [
      {
        "scheduleId": "<schedule_uuid>",
        "timezone": "Region/City",
        "startTime": "01:00",
        "endTime": "16:00",
        "limit": 100,
        "scheduleType": "RBD",
        "scheduleStatus": "active",
        "days": [1, 2, 3, 4, 5, 6, 7],
        "isDeleted": false,
        "isEnabled": true
      }
    ]
  },
  "anySchedulePending": false
}
```

Observed structure:
- `cfg`, `dtg`, and `rbd` are separate schedule families; later captures showed all three families carrying populated `details[]` entries at the same time.
- The captured `days` field used numeric weekday values, but the exact weekday-to-number mapping was not explicit in the trace.
- `scheduleStatus` and per-entry `isEnabled` are separate flags; preserve both rather than collapsing them.
- Live verification on the affected site showed that `/schedules` entry `isEnabled` is not authoritative for disabled families: temporary CFG/DTG/RBD schedules created with `isEnabled: false` were still echoed back as `isEnabled: true` while the corresponding family control in `batterySettings` stayed disabled.

### 5.8.1 Create Battery Schedule
```
POST /service/batteryConfig/api/v1/battery/sites/<site_id>/schedules
Headers: X-XSRF-Token: <token>
Body: {
  "timezone": "Region/City",
  "startTime": "20:00",
  "endTime": "05:00",
  "scheduleType": "CFG",
  "days": [1, 2, 3, 4, 5, 6, 7],
  "limit": 100,
  "isEnabled": true
}
```
Creates a new battery schedule entry. The same endpoint is used for CFG, DTG, and RBD schedule creation and schedule restore flows.

Implementation auth notes:
- The current client acquires fresh XSRF first by preferring `GET /service/batteryConfig/api/v1/siteSettings/<site_id>?userId=<user_id>` and its `x-csrf-token` response header, then falling back to `/battery/sites/<site_id>/schedules/isValid` when needed before sending the write plus `X-XSRF-Token`.
- On affected issue `#460` sites, the verified working write instead reuses the raw stored BatteryConfig cookie (`Cookie`), `e-auth-token`, `Username`, `X-XSRF-Token`, and `X-Requested-With: XMLHttpRequest`.
- That cookie-backed create/update/delete flow is sent from a stateless request session so aiohttp does not merge cookie-jar state into the raw `Cookie` header.
- The current client now tries the cookie-backed compatibility write first when the raw cookie/XSRF pair is available, then falls back through official-web primary, official-web lean, and mixed-auth variants.

Observed behavior:
- `scheduleType` is sent uppercase (`CFG`, `DTG`, `RBD`).
- `limit` is included for charge-oriented schedules and omitted for pure RBD recreate flows.
- Live verification on the affected site showed that `isEnabled` should be sent explicitly on create; omitting it caused a CFG create attempt to fail with `409 CONFLICTING_SCHEDULE_CFG`.
- Disabled temp creates (`isEnabled: false`) for CFG/DTG/RBD were still echoed back by `/schedules` as `isEnabled: true`, so create responses and follow-up `/schedules` entry flags are not sufficient to infer the effective enabled state.
- `startTime` and `endTime` are `HH:MM` strings, while `days` uses the same numeric weekday array returned by `GET /schedules`.
- The current integration treats the create response body as opaque JSON and reconciles authoritative state from the subsequent `GET /schedules` refresh.

### 5.8.2 Delete / Soft-Delete Variants
Two delete patterns were observed:

```
PUT /service/batteryConfig/api/v1/battery/sites/<site_id>/schedules/<schedule_id>
Headers: X-XSRF-Token: <token>
Body: {
  "scheduleType": "CFG",
  "startTime": "20:00",
  "endTime": "05:00",
  "days": [1, 2, 3, 4, 5, 6, 7],
  "timezone": "Region/City",
  "isEnabled": true,
  "isDeleted": true
}
```

```
POST /service/batteryConfig/api/v1/battery/sites/<site_id>/schedules/<schedule_id>/delete
Headers: X-XSRF-Token: <token>
Body: {}
```

Observed behavior:
- Soft-delete is supported through the canonical `PUT /schedules/<schedule_id>` resource, sometimes with the full schedule echoed back and sometimes with only `{ "isDeleted": true }`.
- A `/delete` alias also exists. This path is useful as a compatibility note, but it was not present in the newer browser traces captured for this repository.
- The current client implements the legacy `/delete` alias and sends it through the same compatibility write planner used by schedule create/update, including the raw-cookie browser request on affected sites.
- The current integration treats the `/delete` alias response as opaque JSON and confirms the deletion via the follow-up `GET /schedules` refresh.
- For CFG specifically, deleting the final saved schedule does not by itself prove the schedule-family toggle is off. Live local verification for issue `#634` confirmed clients should send the paired `PUT /batterySettings/<site_id>` disable write and should allow that settings payload to omit `chargeBeginTime` / `chargeEndTime` when no CFG schedule window remains.

### 5.9 Battery Schedule Validation
```
POST /service/batteryConfig/api/v1/battery/sites/<site_id>/schedules/isValid
Body: { "scheduleType": "cfg", "forceScheduleOpted": true }
```
Performs server-side validation before enabling a battery schedule.

The current codebase uses this endpoint in two distinct ways:
- Explicit validation requests (`validate_battery_schedule`) first acquire a fresh token from `GET /service/batteryConfig/api/v1/siteSettings/<site_id>?userId=<user_id>`, then send the lowercase family payload below using the official-web primary request shape with `X-XSRF-Token`. The client retries with the lean variant on `403`.
- Internal XSRF bootstrap requests (`_acquire_xsrf_token`) now first try `GET /service/batteryConfig/api/v1/siteSettings/<site_id>?userId=<user_id>` and read the `x-csrf-token` response header, matching the homeowner web UI flow on affected EMEA sites.
- If the GET bootstrap does not yield a token, the helper still hits `/schedules/isValid` immediately before BatteryConfig writes. That fallback sends the same JSON payload, includes the currently held `X-XSRF-Token` when present, and harvests a fresh token from the response header, `Set-Cookie`, response cookies, or the session cookie jar.

Additional request shapes observed:

```json
{ "scheduleType": "dtg" }
```

```json
{ "scheduleType": "rbd" }
```

Example response (anonymized):
```json
{
  "isValid": true
}
```

The current integration normalizes this success shape to `valid: true` for the standalone Home Assistant service response. Service wrappers treat `403` and `404` validation responses as non-fatal and continue with `{}`, but still raise when the endpoint returns an explicit invalid result.

Observed `403` response shape from the live affected site when the explicit validation request omitted `X-XSRF-Token`:
```json
{
  "timestamp": "2026-04-18T08:45:45.931+00:00",
  "status": 403,
  "error": "Forbidden",
  "path": "/api/v1/battery/sites/<site_id>/schedules/isValid"
}
```

Observed behavior:
- The validation call appeared immediately before enabling charge-from-grid scheduling.
- The request used lowercase schedule-family values (`cfg`, `dtg`, `rbd`) even though stored schedule objects used uppercase `scheduleType` values.
- `forceScheduleOpted: true` was only observed for CFG validation; DTG/RBD validation calls omitted that field.
- In the current client, this route serves both as an explicit validation endpoint and as the XSRF bootstrap mechanism for later BatteryConfig writes. Those two call sites do not use exactly the same header shape.
- Live verification on 2026-04-24 showed that explicit validation succeeds when it includes the `X-XSRF-Token` acquired from `GET /siteSettings`; the same primary request without `X-XSRF-Token` returned `403` for CFG/DTG/RBD across lowercase and uppercase payload variants.
- CFG validation requires `forceScheduleOpted` to be present. Omitting it returned `400` with `"Force schedule opted is null or empty for schedule type."`; both `true` and `false` values were accepted on the affected site.
- The internal XSRF bootstrap helper reuses the same route, includes `X-XSRF-Token` when an existing token is available, and still learns fresh `BP-XSRF-Token` values from `Set-Cookie` / the updated cookie jar.
- If this bootstrap route returns `4xx`, the current client still checks error response cookies and the session cookie jar for a `BP-XSRF-Token`. If none is present there, it performs a final `GET /service/batteryConfig/api/v1/siteSettings/<site_id>` bootstrap that accepts `BP-XSRF-Token` from response cookies or the session cookie jar before giving up and keeping the existing token.
- External client source matches the CFG-only `forceScheduleOpted` rule: it documents `forceScheduleOpted` only for CFG validation and omits it for DTG validation.
- Compatibility note: the current integration should not treat `forceScheduleOpted` as a universal BatteryConfig validation field.

### 5.10 Update Battery Schedule (In-Place PUT)
```
PUT /service/batteryConfig/api/v1/battery/sites/<site_id>/schedules/<schedule_id>
Body: {
  "timezone": "Europe/Lisbon",
  "startTime": "02:00",
  "endTime": "08:00",
  "limit": 61,
  "scheduleType": "CFG",
  "days": [1, 2, 3, 4, 5, 6, 7]
}
```
Updates an existing battery schedule in place.  This is the endpoint used by
the Enlighten battery profile UI when the user modifies a CFG schedule.

Alternate toggle-style request shape used by explicit schedule-entry helper paths:
```json
{
  "timezone": "Europe/Lisbon",
  "startTime": "02:00",
  "endTime": "08:00",
  "limit": 61,
  "scheduleType": "CFG",
  "days": [1, 2, 3, 4, 5, 6, 7],
  "isEnabled": false
}
```

**Headers** (same as other BatteryConfig calls in the current implementation):
- `Username`: Enphase user ID when decodable from the active auth context
- `X-XSRF-Token`: freshly acquired XSRF token echoed in request header
- `Origin: https://battery-profile-ui.enphaseenergy.com`
- `Referer: https://battery-profile-ui.enphaseenergy.com/`
- Chrome-style browser `User-Agent`

Implementation auth notes:
- The current client acquires fresh XSRF immediately before issuing this `PUT`, preferring `GET /siteSettings/<site_id>?userId=<user_id>` and its `x-csrf-token` response header before falling back to `/schedules/isValid`.
- On affected issue `#460` sites, the verified working request is the raw-cookie browser shape:
  - `Cookie: <serialized stored cookie>`
  - `e-auth-token: <enlighten_manager_token_production>`
  - `Username: <user_id>`
  - `X-XSRF-Token: <BP-XSRF-Token from raw cookie header>`
  - `X-Requested-With: XMLHttpRequest`
- That cookie-backed write must be sent from a stateless request session so aiohttp does not merge cookie-jar state into the raw `Cookie` header.
- The current client now tries the cookie-backed compatibility write first when that raw cookie/XSRF pair is available, then falls back through official-web primary, official-web lean, and mixed-auth variants.
- The current integration treats the update response body as opaque JSON for control flow and refreshes `GET /schedules` to confirm the persisted schedule state.

Example response (anonymized):
```json
{
  "createdBy": "<user_id>",
  "updatedBy": "<user_id>",
  "createdAt": "1773435236162",
  "updatedAt": "1774227351728",
  "scheduleId": "<uuid>",
  "timezone": "Europe/Lisbon",
  "startTime": "02:00",
  "endTime": "08:00",
  "limit": 61,
  "scheduleType": "CFG",
  "scheduleStatus": "pending",
  "days": [1, 2, 3, 4, 5, 6, 7],
  "isDeleted": false,
  "isEnabled": true
}
```

Verified live DTG update response on the affected site:
```json
{
  "createdBy": "<user_id>",
  "updatedBy": "<user_id>",
  "createdAt": "<epoch_ms>",
  "updatedAt": "<epoch_ms>",
  "scheduleId": "<uuid>",
  "timezone": "Australia/Melbourne",
  "startTime": "00:00",
  "endTime": "23:59",
  "limit": 22,
  "scheduleType": "DTG",
  "scheduleStatus": "pending",
  "days": [1, 2, 3, 4, 5, 6, 7],
  "isDeleted": false,
  "isEnabled": false
}
```

Observed behavior:
- The response includes a `scheduleStatus` field that transitions from `"pending"` to `"active"` once the Envoy acknowledges the change.
- While a schedule is pending, schedule time/limit edits should still be treated as conflicting, but DTG/RBD on/off toggles can succeed because those writes use `PUT /batterySettings/<site_id>` rather than the schedule CRUD route.
- `startTime` and `endTime` use `HH:MM` format (24-hour, no seconds).
- `limit` is the maximum SoC percentage (5-100).
- `days` is a 1-indexed day-of-week array (1=Monday through 7=Sunday).
- For RBD specifically, `count=0` with `scheduleStatus="active"` is a meaningful observed state. It indicates that the schedule family is available, but there is no saved RBD schedule entry yet.
- In that `count=0` RBD state, `PUT /batterySettings/<site_id>` enable/disable toggles should not be treated as fully symmetric:
  - disable/off reconciliation may need to treat the family as already off when `rbdControl` is also absent on the paired `batterySettings` read
  - enable/on should be blocked by the client until a real RBD schedule is created
- This replaces the delete+create pattern previously used for schedule modifications.
- Ordinary time/limit edits on the affected site preserved `isEnabled: false` when updating the existing real disabled CFG/DTG/RBD schedules without sending `isEnabled`.
- Temporary disabled schedules created with `isEnabled: false` were still echoed back from later update/readback calls as `isEnabled: true` even when the follow-up `PUT` omitted `isEnabled`.
- Explicit CFG schedule-entry toggle writes that included `isEnabled: true` or `isEnabled: false` still read back as `false` for the existing real schedule on the affected site, indicating that effective CFG enablement is controlled by `batterySettings.chargeFromGridScheduleEnabled`, not reliably by the `/schedules` entry flag.
- Live local verification for issue `#634` also reproduced a disabled CFG recreate quirk: creating a replacement CFG schedule with `isEnabled:false` could echo the new entry as `isEnabled:true` while `batterySettings.chargeFromGridScheduleEnabled` stayed `false`. An explicit schedule update with `isEnabled:false` plus the BatterySettings disable write restored both the entry flag and the effective settings flag.
- Captured DTG/RBD enablement toggles were also observed as partial `PUT /batterySettings/<site_id>` payloads (`dtgControl.enabled` / `rbdControl.enabled`), so clients should not assume all schedule-family toggles go through `PUT /battery/sites/<site_id>/schedules/<schedule_id>`.
- The current DTG runtime now follows that split explicitly:
  - toggle enable/disable uses `PUT /batterySettings/<site_id>`
  - schedule time/limit creation and edits use `/battery/sites/<site_id>/schedules`
- The current RBD runtime follows the same split, with one extra guard derived from live verification:
  - toggle disable uses `PUT /batterySettings/<site_id>` and may reconcile to off when the backend exposes no `rbdControl` and no saved RBD schedule
  - toggle enable should require an existing RBD schedule entry
  - schedule creation, time edits, and deletes use `/battery/sites/<site_id>/schedules`
- Live verification on 2026-04-19 showed that overlapping temporary schedule creates can be rejected with misleading cross-family `409` statuses such as `CONFLICTING_SCHEDULE_CFG` even when the attempted write was DTG. The current integration now performs local overlap validation against the cached battery schedule inventory before calling create/update so users see a deterministic conflict message.

### 5.11 ITC Disclaimer Acknowledgement
```
POST /service/batteryConfig/api/v1/batterySettings/acceptDisclaimer/<site_id>
Body: { "disclaimer-type": "itc" }
```
Records the regulatory disclaimer acknowledgement required before enabling charge-from-grid on eligible sites.

Example response (anonymized):
```json
{
  "message": "success"
}
```

Observed behavior:
- This call preceded `PUT /batterySettings/<site_id>` in the captured sequence.
- A subsequent battery-settings update used `acceptedItcDisclaimer: true`; later `GET /batterySettings/<site_id>` returned `acceptedItcDisclaimer` as a timestamp string, so the backend normalizes the acknowledgement internally.
- Repeating the disclaimer-accept call on an already-accepted site refreshed the returned `acceptedItcDisclaimer` timestamp on later `GET /batterySettings/<site_id>` reads.
- The current runtime calls this route before enabling charge-from-grid, then sends `{"chargeFromGrid": true, "acceptedItcDisclaimer": true}` to `PUT /batterySettings/<site_id>`.
- It uses the same compatibility planner as the other BatteryConfig mutation endpoints, including the raw-cookie browser request on affected sites.

---

## 5A. Activation Grid Profile APIs (Installer-Level)

The Activation UI exposes grid-profile discovery and assignment endpoints that are separate from BatteryConfig and the homeowner grid on/off controls. These calls were captured from the Enlighten Activation web UI on 2026-07-08 while inspecting Australian grid profiles.

Operational constraints:
- These endpoints require an account/session with installer-level Activation access. Homeowner/owner sessions should not be assumed to have access.
- A successful apply response means the cloud accepted or queued the grid-profile update; it does not prove the Gateway has finished applying the profile.
- Grid profile changes are safety- and compliance-sensitive. Any runtime control should require explicit opt-in, strong confirmation text, and should expose the pending/in-progress state rather than treating the write as instant.
- Do not log raw Authorization headers, cookies, Activation URLs with user identifiers, site IDs, gateway serials, part numbers, JWTs, email addresses, or local Gateway IP addresses.

### 5A.1 Reference Data
```
GET /service/activation_service/api/details/reference_data
```
Returns country and region/state reference data used by the Activation UI grid-profile filter modal. The `countryCode` and `regionCode` values from this endpoint are the valid `country` and `state` request values for `POST /grid_profiles_filtered`.

Observed request headers:
```http
Accept: application/json, text/plain, */*
Accept-Language: en-AU,en;q=0.9
Cookie: <authenticated Enlighten session cookies>
enlm-token: <manager_or_activation_token>
Referer: https://enlighten.enphaseenergy.com/app/activation_ui/?...
```

Example response excerpt:
```json
{
  "countries": [
    {
      "name": "Australia",
      "code": "AU"
    }
  ],
  "country_regions": {
    "AU": [
      {
        "id": 14,
        "countryCode": "AU",
        "regionCode": "VIC",
        "regionName": "Victoria"
      }
    ]
  }
}
```

Captured `country_regions.AU` values:
```json
[
  {
    "id": 2,
    "countryCode": "AU",
    "regionCode": "ACT",
    "regionName": "Australian Capital Territory"
  },
  {
    "id": 4,
    "countryCode": "AU",
    "regionCode": "NSW",
    "regionName": "New South Wales"
  },
  {
    "id": 6,
    "countryCode": "AU",
    "regionCode": "NT",
    "regionName": "Northern Territory"
  },
  {
    "id": 8,
    "countryCode": "AU",
    "regionCode": "QLD",
    "regionName": "Queensland"
  },
  {
    "id": 10,
    "countryCode": "AU",
    "regionCode": "SA",
    "regionName": "South Australia"
  },
  {
    "id": 12,
    "countryCode": "AU",
    "regionCode": "TAS",
    "regionName": "Tasmania"
  },
  {
    "id": 14,
    "countryCode": "AU",
    "regionCode": "VIC",
    "regionName": "Victoria"
  },
  {
    "id": 16,
    "countryCode": "AU",
    "regionCode": "WA",
    "regionName": "Western Australia"
  }
]
```

Observed behavior:
- The top-level `countries[]` list provides display names and country codes.
- The `country_regions` object is keyed by country code. Earlier browser captures used the same shape under `regions`; clients should accept both keys.
- The grid-profile filter request uses `countryCode` as `country` and `regionCode` as `state`. It does not use the numeric `id`.
- The operational grid-profile list still comes from `POST /grid_profiles_filtered`; this reference endpoint supplies valid picker/filter values.
- Preserve country and region/state codes as returned. Do not synthesize region mappings unless a later implementation needs a fallback.
- Runtime UI should first derive the site/user country from Activation data or existing site metadata, then show only regions for that country. It should not show countries or profiles outside the user's country unless the user explicitly enters an advanced override.
- Search should run within the country-scoped data set. Region search should match `regionCode`, `regionName`, and display labels such as `"VIC, AU"`; profile search should match profile `name` and `profile_id` after region filtering.

### 5A.2 Grid Profiles Filtered
```
POST /service/activation_backend/api/gateway/v4/systems/<site_id>/grid_profiles_filtered
```
Returns grid profiles filtered by country, state, and whether the Activation UI is showing only commonly used profiles.

Observed request headers:
```http
Accept: application/json, text/plain, */*
Content-Type: application/json
Authorization: Bearer <activation_jwt>
Origin: https://enlighten.enphaseenergy.com
Referer: https://enlighten.enphaseenergy.com/app/activation_ui/?...
```

Observed request body:
```json
{
  "commonly_used": true,
  "country": "AU",
  "state": "VIC"
}
```

Example response excerpt:
```json
{
  "title": {
    "country": "AU",
    "state": "VIC",
    "commonly_used": true
  },
  "grid_profiles": {
    "VIC, AU": [
      {
        "name": "AS/NZS 4777.2: 2020 Australia A Region (1.3.12)",
        "profile_id": "agf:68e72cd69638927c4f4beaa0",
        "pel_enabled": false,
        "is_277v_compatible": false
      }
    ]
  },
  "recommended_profile": {
    "name": "AS/NZS 4777.2: 2020 Australia A Region (1.3.12)",
    "profile_id": "agf:68e72cd69638927c4f4beaa0",
    "pel_enabled": false,
    "is_277v_compatible": false
  }
}
```

Observed behavior:
- The 2026-07-08 browser capture showed `commonly_used: true` returning the common non-export-limited Australian Region A profile for ACT, NSW, QLD, SA, VIC, and NT.
- A 2026-07-09 live response for `{"commonly_used": true, "country": "AU", "state": "VIC"}` returned `recommended_profile` as `AS/NZS 4777.2: 2020 Australia A Region (1.3.12)`, while the grouped `grid_profiles["VIC, AU"]` list contained `AS/NZS 4777.2: 2020 Australia A Region 0 kW Export (1.3.9)`.
- For the common-profile UI, prefer the complete `recommended_profile` object when present. Fall back to the region-scoped `grid_profiles` list only when `recommended_profile` is absent or incomplete.
- To discover export-limit profiles, request all profiles with `commonly_used: false`.
- `grid_profiles` is grouped by a display key such as `"VIC, AU"`. Clients should flatten entries for selection but preserve the original grouping for diagnostics.
- `recommended_profile` can duplicate one of the grouped entries, but can also be distinct from the grouped common response; use the API field rather than inferring from the profile name.
- The Activation UI exposes a `Commonly used Grid Profiles` / `All Grid Profiles` filter and country/state dropdowns. It also includes a free-text search field above the grouped profile list.
- Runtime profile discovery should use the user's country to filter available regions first, then call this endpoint only for selected or searched regions in that country.
- Search should not cross country boundaries. For country-scoped search, match region code/name/group labels and profile names, then display grouped results such as `ACT, AU` and `NSW, AU`.

Likely all-profiles request body:
```json
{
  "commonly_used": false,
  "country": "AU",
  "state": "VIC"
}
```

### 5A.3 Current Profile Device List
```
GET /service/activation_backend/api/gateway/v4/systems/<site_id>/devices/list
```
Returns the cloud Gateway inventory together with the selected and requested grid
profile. This is the preferred cloud-only endpoint for checking whether an
accepted profile change has taken effect.

Sanitized response shape:
```json
[
  {
    "envoyCombiner": {
      "IQ Gateway": ["GW0000000000"]
    },
    "envoyGridProfile": {
      "selected_profile_id": "agf:6643fae616246153786f318b",
      "requested_profile_id": null,
      "selected_grid_profile_name": "AS/NZS 4777.2: 2020 Australia A Region 0 kW Export (1.3.9)"
    },
    "ensembleEnvoy": true
  }
]
```

Implementation notes:
- Read the Gateway serial from `envoyCombiner` and the profile state from `envoyGridProfile`.
- A null `requested_profile_id` indicates no cloud profile change is pending.
- Use this `GET` endpoint for status polling. `/systems/<site_id>/envoys` is the observed `PUT` apply endpoint.

### 5A.4 Profile ID Format

Activation profile identifiers include an `agf:` prefix:
```text
agf:68e72cd69638927c4f4beaa0
```

Implementation notes:
- Store and submit the cloud `profile_id` exactly as returned by Activation, including the `agf:` prefix.
- Do not construct profile IDs from names or versions. Use the returned `profile_id`.
- The v1 Home Assistant implementation is cloud-only and does not compare these IDs with local Gateway AGF endpoints.

### 5A.5 Apply Grid Profile
```
PUT /service/activation_backend/api/gateway/v4/systems/<site_id>/envoys
```
Queues or initiates a grid-profile update for one or more Gateway/Envoy devices.

Observed request headers match `5A.2`:
```http
Accept: application/json, text/plain, */*
Content-Type: application/json
Authorization: Bearer <activation_jwt>
Origin: https://enlighten.enphaseenergy.com
Referer: https://enlighten.enphaseenergy.com/app/activation_ui/?...
```

Observed request body for an Australian zero-export profile:
```json
[
  {
    "grid_profile_id": "agf:6643fae616246153786f318b",
    "serial_num": "GW0000000000",
    "part_num": "800-00555-r01",
    "ensemble_envoy": true
  }
]
```

Observed selected profile:
```text
AS/NZS 4777.2: 2020 Australia A Region 0 kW Export (1.3.9)
profile_id: agf:6643fae616246153786f318b
pel_enabled: true
```

Observed response:
```http
HTTP/1.1 200 OK
Content-Type: application/json; charset=utf-8
```

Observed behavior:
- No useful response body was captured.
- The web UI showed the profile update as `in progress` after this request, so HTTP success should be interpreted as accepted/queued.
- `grid_profile_id` is the cloud/Activation profile ID, including `agf:`.
- `serial_num` is the Gateway/Envoy serial number.
- `part_num` is the Gateway/Envoy part number reported to Activation.
- A 2026-07-09 live PUT using the current already-requested profile omitted `part_num` and still returned `HTTP 200`. The response body included `serial_num`, `part_num`, `device_type`, `category`, `grid_profile.selected_profile_id`, and `grid_profile.requested_profile_id`. Runtime apply may omit `part_num` when Activation has not provided it.
- `ensemble_envoy: true` indicates an Ensemble/IQ System Controller capable site in the observed capture. Preserve the boolean returned or inferred from Activation/Gateway data rather than assuming all sites use `true`.
- A runtime implementation should send a single selected Gateway first unless a later capture confirms multi-Gateway semantics.

### 5A.6 Activation Record and Pending Status
```
GET /service/activation_backend/api/gateway/v4/activations/<site_id>?expand=owner,host
```
Reads the cloud-side activation record, including currently recorded and requested grid-profile data. The detailed response shape still needs a redacted fixture before runtime parsing is designed.

Implementation notes:
- The v1 Home Assistant implementation is cloud-only. It must not call local Gateway `/installer/agf/*` endpoints, add local installer auth options, or expose local verification status.
- A successful `PUT /envoys` means Activation accepted or queued the request. Runtime state should remain accepted/pending until Activation record fields show the requested profile has settled.
- Apply must use Gateway serial and `ensemble_envoy` from the Activation record. Include the Gateway part number when Activation provides it, but the cloud endpoint has been observed accepting an omitted `part_num`. If Gateway serial/ensemble metadata are unavailable, expose browsing/status only.
- Redact serials, part numbers, site IDs, profile IDs, raw URLs, cookies, JWTs, and Authorization headers in diagnostics.

---

## 6. Authentication Flow (Shared Across Services)

### 6.1 Login (Enlighten Web)
```
POST https://enlighten.enphaseenergy.com/login/login.json
```
This endpoint authenticates credentials and either completes login immediately or initiates an MFA challenge. The current implementation treats login as a cookie bootstrap step first, then derives site access and tokens afterward.

Observed implementation behavior:
- Credentials are posted as form fields `user[email]` and `user[password]`.
- No pre-existing CSRF/session cookie is required by the client before the first login request.
- MFA may be indicated explicitly by `requires_mfa: true` or inferred from `success: true` together with a `login_otp_nonce` cookie.
- A successful login can still return an incomplete body; the implementation will continue with cookie-based token/site discovery if the response body is empty.
- If `POST /login/login.json` returns `406`, the client falls back to `POST /login/login` using browser form headers and the same `user[email]` / `user[password]` fields, then derives auth state from cookies.
- Authenticated session cookie candidates are `_enlighten_4_session`, `enlighten_session`, and `_enlighten_session`; `enlighten_manager_token_production` is the manager JWT, and the session id can be decoded from that JWT if absent in the response body.

MFA required response (credentials accepted, OTP pending):
```json
{
  "requires_mfa": true
}
```
Indicators:
- `session_id` and `manager_token` are absent from the JSON.
- `Set-Cookie` refreshes `login_otp_nonce` (short expiry).
- `_enlighten_4_session` is not replaced with an authenticated session yet.

Alternate MFA-required shape observed by the implementation:
```json
{
  "success": true,
  "isBlocked": false
}
```

MFA not required response (fully authenticated):
```json
{
  "message": "success",
  "session_id": "<session_id>",
  "manager_token": "<jwt>",
  "is_consumer": true,
  "system_id": "<system_id>",
  "redirect_url": ""
}
```
Indicators:
- `session_id` and `manager_token` are present.
- `Set-Cookie` issues a new authenticated `_enlighten_4_session`.

Other observed response shapes include `success: false` and `isBlocked: true`.

### 6.2 MFA OTP Validation
```
POST https://enlighten.enphaseenergy.com/app-api/validate_login_otp
Content-Type: application/x-www-form-urlencoded
```
Requires the pre-MFA session cookies from the login step (`_enlighten_4_session`, `login_otp_nonce`, XSRF cookies, `email`). Body parameters are base64-encoded:

```
email=<base64_email>
otp=<base64_otp>
xhrFields[withCredentials]=true
```

Success response (authenticated):
```json
{
  "message": "success",
  "session_id": "<session_id>",
  "manager_token": "<jwt>",
  "is_consumer": true,
  "system_id": "<system_id>",
  "redirect_url": "",
  "isValidMobileNumber": true
}
```
Indicators:
- `Set-Cookie` replaces `_enlighten_4_session` with the authenticated session.
- `session_id` and `manager_token` are now available for API access.
- The implementation also sends `X-CSRF-Token` when an XSRF cookie is present and re-seeds the session cookie jar before the request.
- If MFA validation returns a success-like payload or an empty body without `session_id`, the client attempts token/site recovery from the current cookies. A `manager_token` without a recoverable `session_id` is treated as invalid.

Invalid OTP response:
```json
{
  "isValid": false,
  "isBlocked": false
}
```

Blocked (defensive case):
```json
{
  "isValid": false,
  "isBlocked": true
}
```

### 6.3 MFA OTP Resend
```
POST https://enlighten.enphaseenergy.com/app-api/generate_mfa_login_otp
Content-Type: application/x-www-form-urlencoded
```
Body:
```
locale=en
```

Success response (OTP queued):
```json
{
  "success": true,
  "isBlocked": false
}
```
The server rotates `login_otp_nonce` via `Set-Cookie` but does not return `session_id` or `manager_token`.
The config flow sends one OTP automatically when entering the MFA step and locally throttles user-triggered resends for 30 seconds. An empty resend response is accepted as success, and existing cookies are reused if no updated cookie map is returned.

### 6.4 Token Retrieval Used by the Current Client
Current first-party implementation path:
```
GET https://enlighten.enphaseenergy.com/users/self/token
Accept: application/json, text/plain, */*
```

Observed response:
```json
{
  "token": "<jwt>",
  "expires_at": 1770000000
}
```

Observed behavior:
- This is the token bootstrap path used by the current Enlighten web application and the integration.
- The endpoint uses the authenticated Enlighten session cookie. A `client_id` query parameter is optional and is omitted by the integration.
- Enlighten can rotate `_enlighten_4_session` during this request, so the client serializes and persists cookies after token minting.
- The token is stored as both the access token and `e-auth-token` value for cookie-backed site discovery and many EV endpoints.
- If `expires_at` is absent, the client decodes the JWT `exp` claim locally.
- `401` and `403` are treated as invalid credentials. Other failures, including `404`, `422`, `429`, auth unavailability, and generic client errors, are logged/debugged and cookie-only site discovery continues where possible.

Retired path:
```
POST https://entrez.enphaseenergy.com/tokens
```
Enphase stopped honoring this token mint in July 2026 and returned HTTP 400 for otherwise valid authenticated sessions.

Legacy/documented JWT retrieval paths from earlier captures:

Primary path:
```
GET https://enlighten.enphaseenergy.com/app-api/jwt_token.json
```

Observed response:
```json
{
  "token": "<jwt>"
}
```

Fallback path:
```
GET https://enlighten.enphaseenergy.com/service/auth_ms_enho/api/v1/session/token
Headers:
  e-auth-token: <_enlighten_4_session cookie value>
  X-Requested-With: XMLHttpRequest
```

Observed response:
```json
{
  "token": "<jwt>"
}
```

Observed behavior:
- In older captures, `jwt_token.json` appeared to be the simpler retrieval path when available.
- `auth_ms_enho/api/v1/session/token` is a useful fallback when the primary endpoint fails but the authenticated `_enlighten_4_session` cookie is present.
- Clients can decode the JWT locally and refresh it roughly one hour before expiry.

### 6.5 Access Token
The current implementation does not use `https://entrez.enphaseenergy.com/access_token`.

Instead:
- the Enlighten `GET /users/self/token` response is treated as the access/bearer token source;
- scheduler/control requests often prefer the JWT found in the `enlighten_manager_token_production` cookie when present;
- timeseries requests derive `e-auth-token` from the JWT `session_id` claim rather than reusing the raw bearer token.

### 6.5.1 Runtime Auth Refresh
The integration can refresh stored credentials after setup.

Implementation behavior:
- Automatic refresh only runs when email, `remember_password`, and a stored password are available.
- Concurrent refreshes coalesce into a single task, and a recent success is reused for five seconds.
- Rejected credentials/MFA failures cool down for 300 seconds and suspend automatic retries for 86400 seconds after three consecutive rejections.
- Manual refresh has a 60-second failed-attempt cooldown.
- Too-many-active-sessions responses set a 86400-second auth block.
- Successful refresh updates the client cookie/`e-auth-token` state and persists new tokens.

### 6.6 Headers Required by API Client
There is no single universal header set; the implementation varies headers by endpoint family:

| Endpoint family | Auth/header strategy in current client |
| --- | --- |
| Login + MFA | form POSTs plus cookie jar management; `X-CSRF-Token` added for MFA when an XSRF cookie is present |
| Site discovery | authenticated cookies; `X-CSRF-Token`; add `Authorization` + `e-auth-token` when Enlighten session-token retrieval succeeded |
| Basic Enlighten reads (`/app-api`, `/pv`, many EV reads) | authenticated cookies plus `e-auth-token` when available |
| Scheduler + EV control overlays | base headers plus `Authorization: Bearer <jwt>` from manager-token cookie first, stored access token second |
| Session history + EVSE timeseries | `Authorization: Bearer <jwt>`; `e-auth-token` set to JWT `session_id`; `username` set to JWT `user_id`; `requestid` UUID |
| System dashboard reads | authenticated cookies; may also include bearer auth opportunistically |
| HEMS | bearer-preferred auth plus cookies/base headers; `username` and `requestId` when available |
| BatteryConfig reads | current web shape: fresh session `Cookie`, `Username`, `requestid`, battery-profile `Origin`/`Referer`, Chrome-style `User-Agent`, and no `Authorization` or `e-auth-token`; token-backed primary and lean variants remain as fallbacks |
| BatteryConfig writes | acquire fresh XSRF by preferring `GET /service/batteryConfig/api/v1/siteSettings/<site_id>?userId=<user_id>` and its `x-csrf-token` response header, then falling back to `/battery/sites/<site_id>/schedules/isValid`; if that fallback returns `4xx`, still harvest `BP-XSRF-Token` from error response cookies or the session cookie jar, then try a final cookie-based `GET /siteSettings/<site_id>` bootstrap before giving up and keeping the existing token; writes then use an endpoint-specific compatibility planner; on affected sites the verified working shape is a stateless raw-cookie browser request (`Cookie`, `e-auth-token`, `Username`, `X-XSRF-Token`, `X-Requested-With`), with official-web primary, official-web lean, and mixed-auth fallbacks retained |
| Activation reference data and grid profile reads/writes | Activation UI same-origin browser shape. Reference data was observed with authenticated Enlighten cookies plus `enlm-token`; profile discovery/apply were observed with authenticated cookies plus `Authorization: Bearer <activation_jwt>`, `Accept: application/json, text/plain, */*`, `Content-Type: application/json` for writes, `Origin: https://enlighten.enphaseenergy.com`, and an `/app/activation_ui/` referer; grid profile apply requires installer-level Activation permissions |

- Base Enlighten reads:
  - `Cookie: <serialized cookie jar>`
  - `e-auth-token: <token>` when available
  - `X-Requested-With: XMLHttpRequest`
  - `Referer: https://enlighten.enphaseenergy.com/pv/systems/<site_id>/summary`
- Site discovery after login:
  - authenticated cookies
  - `X-CSRF-Token` when an XSRF cookie is present
  - `Authorization: Bearer <token>` and `e-auth-token: <token>` when Enlighten session-token retrieval succeeds
- Scheduler / EV control overlays:
  - `Authorization: Bearer <jwt>` from `enlighten_manager_token_production` cookie when present, otherwise the stored access token
- Session history / EVSE timeseries:
  - `Authorization: Bearer <jwt>`
  - `e-auth-token: <jwt session_id claim>` when present
  - `username: <jwt user_id claim>` when present
  - `requestid: <uuid>`
- BatteryConfig:
  - `Accept: application/json, text/plain, */*`
  - `Username: <user_id>` when decodable from the active auth context
  - `Origin` / `Referer` for the battery-profile UI
  - Chrome-style `User-Agent`
  - `X-XSRF-Token` for `isValid` preflight and writes after token acquisition
  - affected-site compatibility write adds raw `Cookie`, `e-auth-token`, and `X-Requested-With`
  - official-web primary fallback adds `e-auth-token` and `requestid`
  - official-web lean fallback omits `e-auth-token` and `requestid`
  - mixed-auth compatibility fallback restores `Authorization`, `Cookie`, and `X-CSRF-Token`
  - cookie-backed compatibility writes must use a stateless request session so aiohttp does not merge cookie-jar state into the raw `Cookie` header
- Activation grid profile:
  - `Accept: application/json, text/plain, */*`
  - `enlm-token: <manager_or_activation_token>` for `GET /activation_service/api/details/reference_data` in the observed browser capture
  - `Authorization: Bearer <activation_jwt>` for grid profile discovery/apply in the observed browser capture
  - `Content-Type: application/json` for `POST`/`PUT`
  - authenticated Enlighten session cookies
  - `Origin: https://enlighten.enphaseenergy.com`
  - `Referer: https://enlighten.enphaseenergy.com/app/activation_ui/?...`
  - requires installer-level Activation access in the observed capture

---

## 7. Response Field Reference

| Field | Description |
| --- | --- |
| `connected` | Charger cloud connection status |
| `pluggedIn` | Vehicle plugged state |
| `charging` | Active charging session |
| `faulted` | Fault present |
| `offGrid` | Charger grid-mode label from `/ev_chargers/status`; observed value so far: `ON_GRID` |
| `mode` | Charger operating-mode integer from `/ev_chargers/status`; observed values so far: `0` for manual charging and `1` for green charging |
| `commissioned` | Status payload commissioning flag; observed value so far: `1` |
| `smartEV.hasToken` | Smart-EV token availability flag; observed value so far: `false` |
| `smartEV.hasEVDetails` | Smart-EV vehicle-details availability flag; observed value so far: `false` |
| `isEVDetailsSet` | Top-level charger vehicle-details flag from `/ev_chargers/status`; observed value so far: `true` |
| `sch_d.status` | Schedule summary status integer from `/ev_chargers/status`; observed value so far: `1` |
| `sch_d.info[].type` | Schedule summary type label; observed value so far: `greencharging` |
| `sch_d.info[].limit` | Schedule summary limit value; observed value so far: `0` |
| `session_d.auth_status` | Session authorization status code; observed value so far: `4` |
| `session_d.auth_type` / `session_d.auth_id` | Session authorization metadata; observed values so far: `null` / `null` |
| `session_d.charge_level` | Session charge-level override; observed value so far: `null` |
| `connectorStatusType` | ENUM: `AVAILABLE`, `CHARGING`, `FINISHING`, `SUSPENDED`, `SUSPENDED_EV`, `SUSPENDED_EVSE`, `FAULTED` |
| `connectorStatusInfo` | Connector sub-state detail string; observed value so far: `""` |
| `connectorStatusReason` | Additional enum reason (e.g., `INSUFFICIENT_SOLAR`); observed value so far: `""` |
| `connectorId` | Connector index within `connectors[]`; observed value so far: `1` |
| `dlbActive` | Per-connector Dynamic Load Balancing activity flag; observed value so far: `false` |
| `session_d.e_c` | Session energy (Wh for IQ-EVSE/modern session payloads; legacy payloads may report small values as kWh) |
| `session_d.start_time` | Epoch seconds when session started |
| `chargeLevelDetails.min/max` | Min/max allowed amps |
| `chargeLevelDetails.granularity` | Charge-current step size as a string; observed value so far: `"1"` |
| `chargeLevelDetails.defaultChargeLevel` | Default charge-level behavior label; observed value so far: `disabled` |
| `maxCurrent` | Hardware max amp rating |
| `operatingVoltage` | Nominal voltage per summary v2 |
| `dlbEnabled` | Dynamic Load Balancing flag |
| `safeLimitState` | Safe-limit indicator within `connectors[]`. Observed: `1` when the charger reverts to its lowest charge-current level; the effective limit follows the charger capability, for example 8A on some single-phase chargers and 16A on an IQ-EVSE-80R with `chargeLevelDetails.min=16`. |
| `supportsUseBattery` | Summary v2 flag for green-mode "Use Battery" support |
| `hoControl` | Homeowner-control capability flag from summary v2; observed value so far: `true` |
| `activeConnection` | Active network transport label from summary v2; observed value so far: `ethernet` |
| `isConnected` | Connectivity flag from summary v2; observed value so far: `true` |
| `isLocallyConnected` | Local-network connectivity flag from summary v2; observed value so far: `true` |
| `isRetired` | Retirement flag from summary v2; observed value so far: `false` |
| `commissioningStatus` | Summary v2 commissioning state code; observed value so far: `1` |
| `status` (EV summary) | Summary v2 charger health label; observed value so far: `NORMAL` |
| `reportingInterval` | Charger telemetry reporting cadence in seconds; observed value so far: `300` |
| `gridType` | Electrical grid/topology code from summary v2; observed values so far: `2`, `4` |
| `phaseMode` | Charger phase-mode code from summary v2; observed values so far: `1`, `3` |
| `phaseCount` | Number of phases reported by summary v2; observed values so far: `1`, `3` |
| `skuScope` | Charger SKU family label; observed value so far: `GEN2_EU` |
| `warrantyPeriod` | Warranty duration in years from summary v2; observed value so far: `5` |
| `gatewayConnectivityDetails[].gwConnStatus` | Gateway connectivity status code for the paired IQ Gateway; observed value so far: `0` |
| `gatewayConnectivityDetails[].gwConnFailureReason` | Gateway connectivity failure-reason code; observed value so far: `0` |
| `functionalValDetails.state` | Functional validation state code; observed value so far: `1` |
| `networkConfig` | Interfaces with IP/fallback metadata |
| `firmwareVersion` | Charger firmware |
| `processorBoardVersion` | Hardware version |
| `latest_power.value` | Latest site-power sample in watts; negative values were observed (`-30`), so imports/reverse flow must be preserved |
| `latest_power.time` | Epoch-seconds timestamp for `latest_power` |
| `statusCode` (inverter status) | Inverter-health code from `/systems/<site_id>/inverter_status_x.json`; observed value so far: `normal` |
| `type` (inverter status) | Inverter model/family from `/systems/<site_id>/inverter_status_x.json`; observed values so far: `IQ8AC`, `IQ8HC` |
| `siteStatus.severity` | Site-status severity label from BatteryConfig/site-today payloads; observed value so far: `warning` even when `siteStatus.code="normal"` |
| `batteryGridMode` | Battery grid-mode label from BatteryConfig site settings; observed value so far: `ImportExport` |
| `buybackExportPlan` | Battery/HEMS export-plan label from profile details; observed values so far: `netFit`, `""` |
| `stormGuardState` / `severe_weather_watch` | Storm-guard state labels from profile/today payloads; observed value so far: `enabled` |
| `chargeMode` (profile details) | EVSE charging-mode label nested under `devices.iqEvse[]`; observed value so far: `GREEN` |
| `featureDetails.*` | Mixed readable and opaque feature-flag keys from BatteryConfig site settings; preserve unknown keys verbatim and record newly observed boolean values |
| `current_charge` | Site battery state-of-charge percentage string (for example `"48%"`) |
| `available_energy` / `max_capacity` | Site battery available/maximum capacity in kWh |
| `available_power` / `max_power` | Site battery instantaneous/maximum power in kW |
| `show_battery_banner` | Battery-card UI hint flag from `/pv/settings/<site_id>/battery_status.json`; observed value so far: `false` |
| `storages[].serial_number` | Battery serial identifier |
| `storages[].excluded` | Battery inclusion flag; observed value so far: `false` |
| `storages[].led_status` | Raw battery LED/runtime status code; observed values so far: `12`, `17` |
| `storages[].status` / `storages[].statusText` | Battery status code + display label; observed pair so far: `normal` / `Normal` |
| `storages[].last_report` | Epoch seconds for latest battery telemetry |
| `storages[].battery_mode` | Human-readable live controller profile label for the individual storage unit. The integration normalizes this as live controller telemetry and fallback profile context, but a confirmed BatteryConfig `profile` remains authoritative for the selected/effective system profile; observed value so far: `Self-Consumption` |
| `storages[].battery_phase_count` | Number of AC phases exposed for that battery/system view; observed values so far: `1`, `3` |
| `storages[].is_flex_phase` | Flex-phase capability flag observed on three-phase systems; observed values so far: `false`, `true` |
| `storages[].battery_soh` | Battery state-of-health percentage string |
| `included_count` / `excluded_count` | Active vs excluded battery counts in the payload |
| `supportsMqtt` | Battery settings capability flag indicating MQTT-backed config flows are available; observed value so far: `true` |
| `pollingInterval` | Suggested BatteryConfig refresh cadence in seconds; observed value so far: `60` |
| `requestedConfig` / `requestedConfigMqtt` | Pending or echoed config state objects in battery-settings responses; observed values so far: `{}` |
| `drEventActive` / `drEventMode` | Demand-response event state fields in battery-settings responses; observed values so far: `false` / `""` |
| `batteryBackupPercentageMin` / `batteryBackupPercentageMax` | Allowed reserve slider bounds from BatteryConfig; observed values so far: `5` / `100` |
| `previousBatteryBackupPercentage` | Per-profile remembered reserve percentage values |
| `dtgControl` / `cfgControl` / `rbdControl` | Battery UI feature-capability blocks with visibility, lock, and schedule support flags; observed booleans so far include `show=true`, `showDaySchedule=true`, `enabled=false`, `locked=false`, `scheduleSupported=true` |
| `systemTask` | Backend task/activity flag that may indicate settings are being managed asynchronously; observed value so far: `false` |
| `devices.iqEvse.useBatteryFrSelfConsumption` | Indicates IQ EV charger battery participation support in self-consumption mode; observed value so far: `true` |
| `grid_profiles` | Activation grid-profile discovery grouping keyed by display region such as `"VIC, AU"` |
| `recommended_profile` | Activation-selected recommended grid profile; can duplicate one of the grouped `grid_profiles` entries |
| `countries[]` | Activation reference-data country list with display `name` and ISO-like `code` |
| `regions.<country>[]` | Activation reference-data region/state list scoped by country code |
| `regionCode` | Region/state code used as `state` in `grid_profiles_filtered` requests |
| `regionName` | Region/state display name used for search and UI labels |
| `profile_id` (Activation grid profile) | Cloud grid-profile identifier, including `agf:` prefix; preserve exactly for apply requests |
| `pel_enabled` | Grid-profile flag indicating power export limiting support; observed `true` for zero-export profiles and `false` for the common non-export-limited Australian Region A profile |
| `is_277v_compatible` | Grid-profile compatibility flag returned by Activation discovery; observed value so far: `false` |
| `grid_profile_id` | Apply-request field containing the selected Activation profile ID, including `agf:` |
| `ensemble_envoy` | Apply-request Gateway capability flag observed as `true` for an Ensemble/IQ System Controller capable site |
| `device-uid` | Stable HEMS device identifier |
| `device-type` (HEMS) | HEMS device taxonomy values seen in captures: `IQ_ENERGY_ROUTER`, `IQ_GATEWAY`, `SG_READY_GATEWAY`, `ENERGY_METER`, `HEAT_PUMP` |
| `status` / `statusText` (HEMS) | HEMS device health code + display label; observed pair so far: `normal` / `Normal` |
| `pairing-status` (HEMS) | Pairing state label (for example `PAIRED`) for router-attached ecosystem devices |
| `device-state` (HEMS) | Additional router lifecycle label from `hems-devices`; observed value so far: `ACTIVE` |
| `hems-device-id` / `hems-device-facet-id` | HEMS backend identifiers present on router/heat-pump stack members in `hems-devices` payloads |
| `ip` / `ip-address` | Device LAN address fields seen on gateway/router inventory payloads; treat as sensitive in examples |
| `ap_mode` | Boolean flag on gateway inventory entries indicating the IQ Gateway access-point mode was enabled |
| `supportsEntrez` | Boolean capability flag observed on gateway inventory entries |
| `heatpump-status` | App-facing heat-pump runtime state from `/heatpump/<device_uid>/state`; observed values: `IDLE`, `RUNNING` |
| `sg-ready-mode` | SG Ready operating mode label from `/heatpump/<device_uid>/state`; observed values: `MODE_2` (normal), `MODE_3` (recommended consumption / SG Ready on) |
| `vpp-sgready-mode-override` | SG Ready override label from `/heatpump/<device_uid>/state`; observed value so far: `NONE` |
| `last-report-at` | ISO-8601 last telemetry timestamp from `/heatpump/<device_uid>/state` |

---

## 8. Error Handling & Rate Limiting
- HTTP 401 — credentials expired or invalid.
- HTTP 400/404/409/422 during control operations — validation failure or charger not ready/not plugged.
- Rate limiting presents as HTTP 429.
- Observed, non-official request budget: sustained cloud polling around or above roughly **600 requests/hour** (about **10 requests/minute**) appears to increase the risk of throttling, stale optional-service responses, or auth/session degradation. Treat this as a conservative operating ceiling, not an Enphase-published quota.

### 8.1 Cloud status codes (from the official v4 control API)
Enphase’s public “EV Charger Control” reference (https://developer-v4.enphase.com/docs.html) documents the same backend actions behind a `/api/v4/systems/{system_id}/ev_charger/{serial_no}/…` surface. The status codes it lists match the JSON payloads observed on the cloud endpoints documented here. The most relevant responses are:

| HTTP | Status / message | Meaning |
| --- | --- | --- |
| 400 | `Bad request` (`INVALID_SYSTEM_ID`, `Connector Id must be greater than 0`, `Charging level should be in the range [0-100]`) | Input validation failures for site, serial, connector, or requested amperage. |
| 401 | `Not Authorized` | Missing or expired authentication (bearer token or cookie). |
| 403 | `Forbidden` | Authenticated user lacks access to the target site. |
| 405 | `Method not allowed` | Endpoint does not accept the verb being sent (e.g. POST vs PUT). |
| 466 | `UNSUPPORTED_ENVOY` | Envoy must be online and running firmware ≥ 6.0.0 before live actions are accepted. |
| 468 | `INVALID_SYSTEM_ID` | Site ID does not exist or is not mapped to the authenticated account. |
| 472 | `LIVE_STREAM_NOT_SUPPORTED` | Site hardware mix cannot participate in the live polling burst. |
| 473 | `IQ_GATEWAY_NOT_REPORTING` | Backend cannot reach the site’s gateway, so commands and live data are rejected. |
| 550/551 | `SERVICE_UNREACHABLE` | Generic transient fault on the cloud side; retry later. |
| 552 | `CONNECTION_NOT_ESTABLISHED` | Command was queued but the service could not connect downstream to the charger. |

When these conditions occur against the `/service/evse_controller/...` paths, the response often uses an analogous JSON envelope with `"status": "error"` and the same `message`/`details`.

### 8.2 Runtime Request Budget and Minimum Refresh Cadence
Observed behavior from issue `#528` and the request-volume follow-up `#667` indicates that optional endpoint families should be paced conservatively. The integration should optimize for stable, low-volume polling rather than app-like live refresh.

Current implementation policy:
- Keep ordinary slow coordinator polling at **60 seconds or slower** by default.
- Keep temporary fast polling at **30 seconds or slower**, and reserve it for user actions or short-lived state transitions.
- Do not poll low-volatility inventory/topology endpoints on every coordinator refresh. Current minimum cache windows include site-device inventory and system-dashboard detail fan-out at **600 seconds**, HEMS device inventory at **60 seconds**, heat-pump runtime state at **60 seconds**, and Storm Alert at **300 seconds**.
- Derive heat-pump power from HEMS energy deltas on a slower cadence; current heat-pump power/daily energy refreshes use the HEMS daily-consumption cache window of **300 seconds**.
- Endpoint failures must use stale data, endpoint-family cooldowns, or optional-service degradation before increasing polling pressure.
- Optional HEMS/Heat Pump auth failures must not trigger global stored-password refresh. They are isolated behind the HEMS auth circuit and backoff so wallbox, battery, gateway, and other non-HEMS data can continue.
- Temporary Enphase auth blocks should be treated as retryable update failures until the retry window expires, not as immediate Home Assistant reauth unless the user explicitly starts reauth.

Minimum refresh guidance for new code:
- Avoid adding any recurring cloud endpoint faster than **60 seconds** unless it is clearly tied to a user action, an active charging state, or a documented backend polling interval.
- Avoid adding any optional diagnostic, inventory, or derived-energy endpoint faster than **300 seconds** unless freshness is required for an entity's primary state.
- Account for fan-out endpoints as multiple effective requests. System-dashboard device details, HEMS discovery, and per-device energy endpoints can multiply request volume quickly even when the coordinator refresh interval looks safe.
- Prefer diagnostics counters and endpoint-family `request_count` data over raw assumptions when tuning cadence on real sites.

### 8.3 Implementation Diagnostics
The integration exposes diagnostics and system-health data for endpoint behavior rather than raw API payloads.

Implementation notes:
- Diagnostics include multi-site health, endpoint-family cooldown/backoff state and request counts, payload health for EVSE summary/status, site-energy per-flow metadata, tariff health, HEMS auth circuit state, heat-pump runtime diagnostics, and firmware catalog status.
- Sensitive identifiers, tokens, cookies, LAN addresses, emails, and raw payload snippets are redacted or summarized before inclusion.

---

## 9. Known Variations & Open Questions
Endpoint-specific variations are documented in the affected sections so implementation notes stay near the parser or control path they affect.

Cross-cutting unresolved items:
- Local LAN endpoints (`/ivp/pdm/*`, `/ivp/peb/*`) exist but require installer permissions; they are outside the current owner-account cloud runtime.
- Activation grid-profile control requires installer-level access in the observed capture. The integration still needs a confirmed way to detect Activation role eligibility, obtain the same Activation bearer token reliably, and read a redacted activation-record payload before exposing runtime controls.
- Real EVSE MQTT frames have not yet been captured, even though the broker accepts exact-topic subscriptions. The charger card's instantaneous voltage/current/power source remains the main open telemetry capture target.
- Heat-pump write-path behavior remains unresolved; current HEMS captures cover inventory, runtime state, event diagnostics, timeseries, and stream toggles only.

---

## 10. References
- Reverse-engineered from Enlighten mobile/web network traces (2024–2026).
