# Service Status History

- Current status: **Fully Operational**
- Last updated: `2026-08-03 20:10 UTC`
- Failed checks in latest run: `1`
- Latest failed checks: system_dashboard
- Retained hourly samples: `375`
- Incident windows in last 30 days: `5`

This page is generated from hourly synthetic checks against Enphase cloud endpoints. It may miss incidents that begin and recover between checks.

## Incident Timeline

```mermaid
gantt
    title Enphase Service Status Incident Timeline (Last 30 Days)
    dateFormat  YYYY-MM-DDTHH:mm:ss
    axisFormat  %b %d
    Window start :vert, window-start, 2026-07-04T20:10:14, 0ms
    Window end :vert, window-end, 2026-08-03T20:10:14, 0ms
    section Down
    Down 1 (2026-07-04 2127 UTC) :crit, down-1, 2026-07-04T21:27:16, 126m
    Down 2 (2026-07-05 0227 UTC) :crit, down-2, 2026-07-05T02:27:32, 109m
    Down 3 (2026-07-11 1420 UTC) :crit, down-3, 2026-07-11T14:20:54, 69m
    section Degraded
    Degraded 1 (2026-07-09 1701 UTC) :active, degraded-1, 2026-07-09T17:01:02, 60m
    Degraded 2 (2026-07-15 0221 UTC) :active, degraded-2, 2026-07-15T02:21:35, 60m
```

## Incident Summary

| Status | Started (UTC) | Ended (UTC) | Duration | Failed checks |
| --- | --- | --- | --- | --- |
| Down | 2026-07-04 21:27 UTC | Unknown after last seen 2026-07-04 23:33 UTC | Observed 2h 6m | battery_config, evse_control, evse_scheduler, session_history |
| Down | 2026-07-05 02:27 UTC | 2026-07-05 04:16 UTC | 1h 49m | battery_config, evse_control, evse_scheduler, session_history |
| Degraded | 2026-07-09 17:01 UTC | Unknown after last seen 2026-07-09 17:01 UTC | Observed 0m | site_energy |
| Down | 2026-07-11 14:20 UTC | 2026-07-11 15:30 UTC | 1h 9m | auth |
| Degraded | 2026-07-15 02:21 UTC | Unknown after last seen 2026-07-15 02:21 UTC | Observed 0m | evse_scheduler |

## Raw Artifacts

- [Current status.json](https://raw.githubusercontent.com/barneyonline/ha-enphase-energy/service-status/status.json)
- [30-day history.json](https://raw.githubusercontent.com/barneyonline/ha-enphase-energy/service-status/history.json)
- [30-day incidents.json](https://raw.githubusercontent.com/barneyonline/ha-enphase-energy/service-status/incidents.json)

