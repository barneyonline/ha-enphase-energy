# Service Status History

- Current status: **Fully Operational**
- Last updated: `2026-06-02 05:09 UTC`
- Failed checks in latest run: `1`
- Latest failed checks: battery_config
- Retained hourly samples: `337`
- Incident windows in last 30 days: `17`

This page is generated from hourly synthetic checks against Enphase cloud endpoints. It may miss incidents that begin and recover between checks.

## Incident Timeline

```mermaid
gantt
    title Enphase Service Status Incident Timeline (Last 30 Days)
    dateFormat  YYYY-MM-DDTHH:mm:ss
    axisFormat  %b %d
    Window start :vert, window-start, 2026-05-03T05:09:53, 0ms
    Window end :vert, window-end, 2026-06-02T05:09:53, 0ms
    section Down
    Down 1 (2026-05-08 0529 UTC) :crit, down-1, 2026-05-08T05:29:47, 60m
    Down 2 (2026-05-15 0948 UTC) :crit, down-2, 2026-05-15T09:48:18, 60m
    Down 3 (2026-05-20 2251 UTC) :crit, down-3, 2026-05-20T22:51:14, 75m
    Down 4 (2026-05-24 1757 UTC) :crit, down-4, 2026-05-24T17:57:41, 86m
    Down 5 (2026-05-26 2144 UTC) :crit, down-5, 2026-05-26T21:44:11, 85m
    Down 6 (2026-05-27 2216 UTC) :crit, down-6, 2026-05-27T22:16:53, 87m
    section Degraded
    Degraded 1 (2026-05-07 1315 UTC) :active, degraded-1, 2026-05-07T13:15:25, 60m
    Degraded 2 (2026-05-07 1541 UTC) :active, degraded-2, 2026-05-07T15:41:58, 60m
    Degraded 3 (2026-05-07 1746 UTC) :active, degraded-3, 2026-05-07T17:46:51, 60m
    Degraded 4 (2026-05-07 1942 UTC) :active, degraded-4, 2026-05-07T19:42:10, 249m
    Degraded 5 (2026-05-08 0212 UTC) :active, degraded-5, 2026-05-08T02:12:39, 60m
    Degraded 6 (2026-05-08 0441 UTC) :active, degraded-6, 2026-05-08T04:41:46, 60m
    Degraded 7 (2026-05-08 0707 UTC) :active, degraded-7, 2026-05-08T07:07:30, 60m
    Degraded 8 (2026-05-08 0855 UTC) :active, degraded-8, 2026-05-08T08:55:20, 88m
    Degraded 9 (2026-05-24 2030 UTC) :active, degraded-9, 2026-05-24T20:30:57, 84m
    Degraded 10 (2026-05-27 1740 UTC) :active, degraded-10, 2026-05-27T17:40:52, 60m
    Degraded 11 (2026-05-30 2200 UTC) :active, degraded-11, 2026-05-30T22:00:37, 60m
```

## Incident Summary

| Status | Started (UTC) | Ended (UTC) | Duration | Failed checks |
| --- | --- | --- | --- | --- |
| Degraded | 2026-05-07 13:15 UTC | Unknown after last seen 2026-05-07 13:15 UTC | Observed 0m | battery_config, evse_scheduler |
| Degraded | 2026-05-07 15:41 UTC | Unknown after last seen 2026-05-07 15:41 UTC | Observed 0m | battery_config, evse_scheduler, site_live |
| Degraded | 2026-05-07 17:46 UTC | Unknown after last seen 2026-05-07 17:46 UTC | Observed 0m | battery_config, evse_scheduler |
| Degraded | 2026-05-07 19:42 UTC | 2026-05-07 23:52 UTC | 4h 9m | battery_config, evse_scheduler |
| Degraded | 2026-05-08 02:12 UTC | Unknown after last seen 2026-05-08 02:50 UTC | Observed 37m | battery_config, battery_runtime, discovery, evse_scheduler, inventory, microinverters, site_energy, site_live, system_dashboard_details, system_dashboard_tree |
| Degraded | 2026-05-08 04:41 UTC | 2026-05-08 05:29 UTC | 48m | battery_config, evse_scheduler |
| Down | 2026-05-08 05:29 UTC | Unknown after last seen 2026-05-08 05:29 UTC | Observed 0m | battery_config, evse_runtime |
| Degraded | 2026-05-08 07:07 UTC | Unknown after last seen 2026-05-08 07:07 UTC | Observed 0m | battery_config, evse_scheduler |
| Degraded | 2026-05-08 08:55 UTC | 2026-05-08 10:23 UTC | 1h 28m | battery_config, evse_scheduler |
| Down | 2026-05-15 09:48 UTC | Unknown after last seen 2026-05-15 09:48 UTC | Observed 0m | battery_config, battery_runtime, microinverters |
| Down | 2026-05-20 22:51 UTC | 2026-05-21 00:06 UTC | 1h 15m | battery_config, evse_runtime, evse_scheduler |
| Down | 2026-05-24 17:57 UTC | 2026-05-24 19:23 UTC | 1h 26m | battery_config, evse_runtime, evse_scheduler |
| Degraded | 2026-05-24 20:30 UTC | 2026-05-24 21:55 UTC | 1h 24m | battery_config, evse_scheduler |
| Down | 2026-05-26 21:44 UTC | 2026-05-26 23:09 UTC | 1h 25m | battery_config, evse_runtime, evse_scheduler |
| Degraded | 2026-05-27 17:40 UTC | Unknown after last seen 2026-05-27 17:40 UTC | Observed 0m | battery_config, evse_scheduler |
| Down | 2026-05-27 22:16 UTC | Unknown after last seen 2026-05-27 23:44 UTC | Observed 1h 27m | battery_config, evse_runtime, evse_scheduler |
| Degraded | 2026-05-30 22:00 UTC | Unknown after last seen 2026-05-30 22:00 UTC | Observed 0m | battery_config, evse_scheduler |

## Raw Artifacts

- [Current status.json](https://raw.githubusercontent.com/barneyonline/ha-enphase-energy/service-status/status.json)
- [30-day history.json](https://raw.githubusercontent.com/barneyonline/ha-enphase-energy/service-status/history.json)
- [30-day incidents.json](https://raw.githubusercontent.com/barneyonline/ha-enphase-energy/service-status/incidents.json)

