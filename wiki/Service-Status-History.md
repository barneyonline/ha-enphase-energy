# Service Status History

- Current status: **Down**
- Last updated: `2026-06-30 00:03 UTC`
- Failed checks in latest run: `4`
- Latest failed checks: evse_scheduler, session_history, battery_config, evse_control
- Retained hourly samples: `257`
- Incident windows in last 30 days: `12`

This page is generated from hourly synthetic checks against Enphase cloud endpoints. It may miss incidents that begin and recover between checks.

## Incident Timeline

```mermaid
gantt
    title Enphase Service Status Incident Timeline (Last 30 Days)
    dateFormat  YYYY-MM-DDTHH:mm:ss
    axisFormat  %b %d
    Window start :vert, window-start, 2026-05-31T00:03:59, 0ms
    Window end :vert, window-end, 2026-06-30T00:03:59, 0ms
    section Down
    Down 1 (2026-06-07 1523 UTC) :crit, down-1, 2026-06-07T15:23:22, 87m
    Down 2 (2026-06-09 2117 UTC) :crit, down-2, 2026-06-09T21:17:17, 60m
    Down 3 (2026-06-10 1219 UTC) :crit, down-3, 2026-06-10T12:19:21, 60m
    Down 4 (2026-06-11 0159 UTC) :crit, down-4, 2026-06-11T01:59:36, 60m
    Down 5 (2026-06-11 0658 UTC) :crit, down-5, 2026-06-11T06:58:45, 60m
    Down 6 (2026-06-17 1924 UTC) :crit, down-6, 2026-06-17T19:24:51, 60m
    Down 7 (2026-06-29 0646 UTC) :crit, down-7, 2026-06-29T06:46:18, 60m
    Down 8 (2026-06-29 1157 UTC) :crit, down-8, 2026-06-29T11:57:36, 60m
    Down 9 (2026-06-29 1559 UTC) :crit, down-9, 2026-06-29T15:59:02, 60m
    Down 10 (2026-06-29 1816 UTC) :crit, down-10, 2026-06-29T18:16:38, 60m
    Down 11 (2026-06-29 2023 UTC) :crit, down-11, 2026-06-29T20:23:16, 220m
    section Degraded
    Degraded 1 (2026-06-25 2352 UTC) :active, degraded-1, 2026-06-25T23:52:01, 60m
```

## Incident Summary

| Status | Started (UTC) | Ended (UTC) | Duration | Failed checks |
| --- | --- | --- | --- | --- |
| Down | 2026-06-07 15:23 UTC | 2026-06-07 16:50 UTC | 1h 27m | auth |
| Down | 2026-06-09 21:17 UTC | Unknown after last seen 2026-06-09 21:17 UTC | Observed 0m | battery_config, evse_runtime, evse_scheduler |
| Down | 2026-06-10 12:19 UTC | Unknown after last seen 2026-06-10 12:19 UTC | Observed 0m | battery_config, discovery, evse_scheduler |
| Down | 2026-06-11 01:59 UTC | Unknown after last seen 2026-06-11 01:59 UTC | Observed 0m | auth |
| Down | 2026-06-11 06:58 UTC | Unknown after last seen 2026-06-11 06:58 UTC | Observed 0m | auth |
| Down | 2026-06-17 19:24 UTC | Unknown after last seen 2026-06-17 19:24 UTC | Observed 0m | auth |
| Degraded | 2026-06-25 23:52 UTC | Unknown after last seen 2026-06-25 23:52 UTC | Observed 0m | battery_config, evse_scheduler |
| Down | 2026-06-29 06:46 UTC | Unknown after last seen 2026-06-29 06:46 UTC | Observed 0m | battery_config, evse_control, evse_scheduler, session_history |
| Down | 2026-06-29 11:57 UTC | Unknown after last seen 2026-06-29 11:57 UTC | Observed 0m | battery_config, evse_control, evse_scheduler, session_history |
| Down | 2026-06-29 15:59 UTC | Unknown after last seen 2026-06-29 15:59 UTC | Observed 0m | battery_config, evse_control, evse_scheduler, session_history |
| Down | 2026-06-29 18:16 UTC | Unknown after last seen 2026-06-29 18:16 UTC | Observed 0m | battery_config, evse_control, evse_scheduler, session_history |
| Down | 2026-06-29 20:23 UTC | Ongoing (last seen 2026-06-30 00:03 UTC) | Observed 3h 40m | battery_config, evse_control, evse_scheduler, session_history |

## Raw Artifacts

- [Current status.json](https://raw.githubusercontent.com/barneyonline/ha-enphase-energy/service-status/status.json)
- [30-day history.json](https://raw.githubusercontent.com/barneyonline/ha-enphase-energy/service-status/history.json)
- [30-day incidents.json](https://raw.githubusercontent.com/barneyonline/ha-enphase-energy/service-status/incidents.json)

