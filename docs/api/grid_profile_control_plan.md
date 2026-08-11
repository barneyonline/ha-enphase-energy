# Cloud-Only Installer Grid Profile Control Plan

This plan covers the v1 grid-profile feature implemented against Enphase cloud Activation endpoints only. It intentionally excludes local Gateway `/installer/agf/*` calls, local installer authentication, and LAN verification.

## Scope

- Require confirmed installer-level Enphase Activation access before exposing entities or applying a profile.
- Browse and search profiles only within the derived user/site country.
- Use Activation reference data for country-scoped region options.
- Use Activation records as the authoritative source for Gateway serial, part number, `ensemble_envoy`, and current/requested profile status.
- Stage profile selections in Home Assistant and apply only through the confirmed Options flow or a `set_grid_profile` call with `confirm: true`.
- Treat cloud HTTP success from `PUT /envoys` as accepted/pending, not local completion.

## Endpoint Set

- `GET /service/activation_service/api/details/reference_data`
- `GET /service/activation_backend/api/gateway/v4/activations/<site_id>?expand=owner,host`
- `POST /service/activation_backend/api/gateway/v4/systems/<site_id>/grid_profiles_filtered`
- `PUT /service/activation_backend/api/gateway/v4/systems/<site_id>/envoys`

No local Gateway endpoints are used in v1.

## Runtime Design

- Add an optional `activation_grid_profile` endpoint family with cooldown/backoff and suppressed non-installer failures. Do not probe it until the default-off installer Grid Profile controls option is enabled.
- Bootstrap the short-lived Activation JWT from the authenticated
  `/systems/<site_id>/details` page that embeds `/app/activation_ui/`, keep it
  in memory only, and synchronize it into both the bearer header and
  `enlighten_manager_token_production` cookie used by the working Enlighten UI.
- Preserve the original cookie header for stored-token fallback, and force one
  bootstrap/header rebuild when an Activation backend rejects a cached JWT.
- Keep Activation access failures visible in endpoint diagnostics and backoff,
  but do not roll this optional installer capability into overall degraded service.
- Derive country from Activation/site metadata first, then BatteryConfig country, then system-dashboard country.
- Expose `regions[user_country]` only.
- Cache profile catalogs by `(country, region_code, commonly_used)`.
- Search region code/name/group label/profile name/profile ID within the selected country only.
- Keep staged region, list mode, staged profile, and pending cloud apply state in `GridProfileRuntime`.
- Do not create grid-profile entities unless the feature option is enabled and installer access is confirmed.

## Public Interfaces

Services:
- `browse_grid_profiles`: response-capable browse/search result for the derived country.
- `refresh_grid_profiles`: response-capable forced Activation reference/record/catalog refresh.
- `set_grid_profile`: confirmed cloud apply. Requires installer access, valid profile in the derived-country catalog, complete Gateway metadata from Activation, and `confirm: true`.

Options and entities, exposed only after installer access is confirmed:
- Options > Advanced: country-scoped region, commonly used/all profile mode, profile selection, and confirmed apply.
- Sensor: Current Grid Profile on the IQ Gateway device.

## Safety Rules

- Never call `https://<gateway_ip>/installer/agf/...`.
- Never expose local auth options or local verification status.
- Submit `profile_id` exactly as returned by Activation, including the `agf:` prefix.
- Do not apply when Activation lacks Gateway serial or `ensemble_envoy`. Include `part_num` when Activation provides it; the cloud endpoint has been observed accepting requests without it.
- Redact tokens, cookies, site IDs, serials, part numbers, URLs, and profile IDs from diagnostics.
