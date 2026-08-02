# Service Status History

- Current status: **Fully Operational**
- Last updated: `2026-08-02 14:46 UTC`
- Failed checks in latest run: `1`
- Latest failed checks: system_dashboard
- Retained hourly samples: `376`
- Incident windows in last 30 days: `13`

This page is generated from hourly synthetic checks against Enphase cloud endpoints. It may miss incidents that begin and recover between checks.

## Incident Timeline

```mermaid
gantt
    title Enphase Service Status Incident Timeline (Last 30 Days)
    dateFormat  YYYY-MM-DDTHH:mm:ss
    axisFormat  %b %d
    Window start :vert, window-start, 2026-07-03T14:46:38, 0ms
    Window end :vert, window-end, 2026-08-02T14:46:38, 0ms
    section Down
    Down 1 (2026-07-03 1605 UTC) :crit, down-1, 2026-07-03T16:05:49, 60m
    Down 2 (2026-07-03 1755 UTC) :crit, down-2, 2026-07-03T17:55:54, 338m
    Down 3 (2026-07-04 0247 UTC) :crit, down-3, 2026-07-04T02:47:22, 60m
    Down 4 (2026-07-04 0618 UTC) :crit, down-4, 2026-07-04T06:18:09, 60m
    Down 5 (2026-07-04 0904 UTC) :crit, down-5, 2026-07-04T09:04:41, 60m
    Down 6 (2026-07-04 1105 UTC) :crit, down-6, 2026-07-04T11:05:21, 60m
    Down 7 (2026-07-04 1243 UTC) :crit, down-7, 2026-07-04T12:43:25, 60m
    Down 8 (2026-07-04 1428 UTC) :crit, down-8, 2026-07-04T14:28:07, 243m
    Down 9 (2026-07-04 2007 UTC) :crit, down-9, 2026-07-04T20:07:50, 206m
    Down 10 (2026-07-05 0227 UTC) :crit, down-10, 2026-07-05T02:27:32, 109m
    Down 11 (2026-07-11 1420 UTC) :crit, down-11, 2026-07-11T14:20:54, 69m
    section Degraded
    Degraded 1 (2026-07-09 1701 UTC) :active, degraded-1, 2026-07-09T17:01:02, 60m
    Degraded 2 (2026-07-15 0221 UTC) :active, degraded-2, 2026-07-15T02:21:35, 60m
```

## Incident Summary

| Status | Started (UTC) | Ended (UTC) | Duration | Failed checks |
| --- | --- | --- | --- | --- |
| Down | 2026-07-03 16:05 UTC | Unknown after last seen 2026-07-03 16:05 UTC | Observed 0m | battery_config, evse_control, evse_scheduler, session_history |
| Down | 2026-07-03 17:55 UTC | Unknown after last seen 2026-07-03 23:34 UTC | Observed 5h 38m | battery_config, evse_control, evse_scheduler, session_history |
| Down | 2026-07-04 02:47 UTC | Unknown after last seen 2026-07-04 02:47 UTC | Observed 0m | battery_config, evse_control, evse_scheduler, session_history |
| Down | 2026-07-04 06:18 UTC | Unknown after last seen 2026-07-04 06:18 UTC | Observed 0m | battery_config, evse_control, evse_scheduler, session_history |
| Down | 2026-07-04 09:04 UTC | Unknown after last seen 2026-07-04 09:04 UTC | Observed 0m | battery_config, evse_control, evse_scheduler, session_history |
| Down | 2026-07-04 11:05 UTC | Unknown after last seen 2026-07-04 11:05 UTC | Observed 0m | battery_config, evse_control, evse_scheduler, session_history |
| Down | 2026-07-04 12:43 UTC | Unknown after last seen 2026-07-04 12:43 UTC | Observed 0m | battery_config, evse_control, evse_scheduler, session_history |
| Down | 2026-07-04 14:28 UTC | Unknown after last seen 2026-07-04 18:31 UTC | Observed 4h 3m | battery_config, evse_control, evse_scheduler, session_history |
| Down | 2026-07-04 20:07 UTC | Unknown after last seen 2026-07-04 23:33 UTC | Observed 3h 26m | battery_config, evse_control, evse_scheduler, session_history |
| Down | 2026-07-05 02:27 UTC | 2026-07-05 04:16 UTC | 1h 49m | battery_config, evse_control, evse_scheduler, session_history |
| Degraded | 2026-07-09 17:01 UTC | Unknown after last seen 2026-07-09 17:01 UTC | Observed 0m | site_energy |
| Down | 2026-07-11 14:20 UTC | 2026-07-11 15:30 UTC | 1h 9m | auth |
| Degraded | 2026-07-15 02:21 UTC | Unknown after last seen 2026-07-15 02:21 UTC | Observed 0m | evse_scheduler |

## Raw Artifacts

- [Current status.json](https://raw.githubusercontent.com/barneyonline/ha-enphase-energy/service-status/status.json)
- [30-day history.json](https://raw.githubusercontent.com/barneyonline/ha-enphase-energy/service-status/history.json)
- [30-day incidents.json](https://raw.githubusercontent.com/barneyonline/ha-enphase-energy/service-status/incidents.json)

