# Glossary

This glossary explains project and Enphase terms used in code, diagnostics, issues, and tests.

## Home Assistant Terms

**Config entry**  
The Home Assistant record created when a user adds the integration. It stores setup data, options, and runtime objects while loaded.

**Coordinator**  
The `EnphaseCoordinator` instance for one config entry. It owns refreshes, normalized state, endpoint health, runtime managers, and diagnostic state.

**Entity**  
A Home Assistant object exposed by the integration, such as a sensor, switch, button, select, number, time, calendar, binary sensor, or update entity.

**Entity registry**  
Home Assistant's persistent registry for entity IDs, unique IDs, disabled state, and user customizations.

**Device registry**  
Home Assistant's persistent registry for physical or logical devices. This integration creates type devices, charger devices, and site-related device groupings.

**Reauth**  
Home Assistant's flow for asking the user to authenticate again when stored credentials or tokens stop working.

**Repair issue**  
A Home Assistant issue registry entry shown to users when the integration detects actionable problems such as auth blocks, optional service outages, or persistent cloud failures.

**Runtime data**  
Objects attached to a loaded config entry. This integration stores the coordinator and editor managers there so platforms can share one runtime state.

## Enphase Product Terms

**Enlighten**  
The Enphase cloud application used by the integration for authentication, site discovery, telemetry, and controls.

**Site**  
An Enphase system associated with an account. The site ID is the primary integration identity and should be treated as sensitive in diagnostics and logs.

**IQ Gateway / Envoy**  
The gateway device that reports site telemetry and bridges many Enphase system components to Enlighten.

**System Controller / Enpower**  
The Enphase controller used for backup and grid-control capabilities on supported battery sites. Some older payloads expose it as `enpower`.

**IQ Battery / Encharge**  
The current Enphase battery family. Code often uses the legacy product key `encharge` because that is how many cloud payloads identify battery devices.

**AC Battery**  
An older Enphase battery family that is no longer supported by this integration. IQ Battery support is separate.

**IQ EV Charger / EVSE**  
The Enphase EV charger family. EVSE means electric vehicle supply equipment and is the common API term for charger devices.

**Microinverter**  
An Enphase inverter attached to solar panels. The integration exposes inventory, connectivity, and lifetime production data when available.

**HEMS**  
Home Energy Management System. In this integration, HEMS endpoints provide extra inventory and energy channels such as heat-pump runtime and consumption data.

**Heat pump**  
A HEMS-supported load type. Heat-pump entities may come from dedicated HEMS inventory and runtime endpoints rather than generic site inventory alone.

**Dry contact**  
Relay-style auxiliary contacts exposed by some Enphase systems. These are treated as concrete child devices rather than aggregate inventory type devices.

## API And Data Terms

**BatteryConfig**  
The Enphase battery profile web application and its backing API. It controls battery profile, reserve, schedule, Storm Guard, and grid-related settings on supported sites.

**Scheduler**  
The Enphase EVSE schedule service. It controls charger schedule slots and charge-mode behavior.

**Auth settings**  
EVSE configuration data that controls charger authentication features such as app authorization and RFID support.

**Bearer token**  
A JWT-like token sent in an `Authorization: Bearer ...` header. Some Enphase endpoints derive this from cookies, while others use the stored access token.

**e-auth token**  
An Enlighten auth token header or value used by several Enphase cloud endpoints.

**Cookie auth**  
Requests authenticated with the browser session cookie captured during login. Some BatteryConfig paths require cookie and XSRF values that match the original browser session.

**XSRF token**  
A cross-site request forgery token required by BatteryConfig writes. It may be delivered through headers, cookies, or an existing stored cookie depending on site and endpoint.

**Login wall**  
An HTML login page returned by Enlighten where JSON was expected. The integration treats this as an auth failure instead of a normal payload parse error.

**Optional endpoint**  
An Enphase endpoint family that enhances the integration but should not break the whole config entry when unavailable. Examples include scheduler details, HEMS runtime payloads, system-dashboard details, and some diagnostics-only payloads.

**Endpoint family**  
A group of related cloud calls tracked together for cache, stale data, diagnostics, and backoff decisions.

**Payload shape**  
The structure of a JSON or HTML response, such as wrapper keys, field names, nested lists, and status fields. Shape summaries are used for diagnostics without logging raw identifiers.

**Stale data**  
Previously fetched data reused after an optional endpoint fails. Stale data is used only where it is safer than removing entities or hiding recent context during a cloud blip.

**Backoff**  
A delay before retrying a failing endpoint family. Backoff protects Enphase services and prevents repeated user-facing failures during outages or rate limiting.

## Integration Terms

**Type key**  
A canonical inventory category such as `envoy`, `encharge`, `iqevse`, `heatpump`, `microinverter`, or `dry_contact`.

**Type bucket**  
The normalized inventory group for one type key. It contains count, label, member devices, and selected metadata.

**Inventory view**  
The `InventoryView` helper used by entity platforms to decide whether a device type should be created or available.

**Runtime manager**  
A class that owns one endpoint family or feature area, such as battery runtime, EVSE runtime, inventory runtime, heat-pump runtime, or auth-refresh runtime.

**Optimistic cache**  
A short-lived local value used after a successful write when Enphase read endpoints lag behind write acknowledgements.

**Selected type keys**  
The device categories the user enabled during onboarding or reconfigure. They gate which entity groups are created.

**Site-only mode**  
A configuration where no EV charger serials are selected, but site-level telemetry and selected device-category entities can still be used.

**Schedule slot**  
An EVSE scheduler record with an ID, start/end time, days, charge limit, enabled flag, and scheduler-owned metadata.

**Battery schedule family**  
One BatteryConfig schedule group: `cfg` for charge from grid, `dtg` for discharge to grid, or `rbd` for restricted battery discharge.

**Storm Guard**  
An Enphase battery feature that changes behavior for storm preparation. The integration exposes controls only when the site reports support.

**Grid control**  
Battery settings that control charge from grid, discharge to grid, and related schedule behavior.

**Diagnostics-safe**  
Data that has been redacted enough to share in GitHub issues or support discussions without exposing credentials, account identifiers, site IDs, serials, network details, or opaque private links.
