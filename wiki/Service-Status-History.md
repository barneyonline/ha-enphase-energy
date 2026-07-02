# Service Status History

- Current status: **Down**
- Last updated: `2026-07-02 06:55 UTC`
- Failed checks in latest run: `4`
- Latest failed checks: evse_scheduler, session_history, battery_config, evse_control
- Retained hourly samples: `258`
- Incident windows in last 30 days: `30`

This page is generated from hourly synthetic checks against Enphase cloud endpoints. It may miss incidents that begin and recover between checks.

## Incident Timeline

```mermaid
gantt
    title Enphase Service Status Incident Timeline (Last 30 Days)
    dateFormat  YYYY-MM-DDTHH:mm:ss
    axisFormat  %b %d
    Window start :vert, window-start, 2026-06-02T06:55:07, 0ms
    Window end :vert, window-end, 2026-07-02T06:55:07, 0ms
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
    Down 12 (2026-06-30 0443 UTC) :crit, down-12, 2026-06-30T04:43:54, 60m
    Down 13 (2026-06-30 0828 UTC) :crit, down-13, 2026-06-30T08:28:43, 60m
    Down 14 (2026-06-30 1129 UTC) :crit, down-14, 2026-06-30T11:29:15, 60m
    Down 15 (2026-06-30 1324 UTC) :crit, down-15, 2026-06-30T13:24:25, 60m
    Down 16 (2026-06-30 1603 UTC) :crit, down-16, 2026-06-30T16:03:26, 60m
    Down 17 (2026-06-30 1818 UTC) :crit, down-17, 2026-06-30T18:18:39, 60m
    Down 18 (2026-06-30 2028 UTC) :crit, down-18, 2026-06-30T20:28:54, 171m
    Down 19 (2026-07-01 0139 UTC) :crit, down-19, 2026-07-01T01:39:18, 60m
    Down 20 (2026-07-01 0625 UTC) :crit, down-20, 2026-07-01T06:25:35, 60m
    Down 21 (2026-07-01 1029 UTC) :crit, down-21, 2026-07-01T10:29:24, 60m
    Down 22 (2026-07-01 1318 UTC) :crit, down-22, 2026-07-01T13:18:31, 60m
    Down 23 (2026-07-01 1604 UTC) :crit, down-23, 2026-07-01T16:04:29, 60m
    Down 24 (2026-07-01 1824 UTC) :crit, down-24, 2026-07-01T18:24:21, 60m
    Down 25 (2026-07-01 2027 UTC) :crit, down-25, 2026-07-01T20:27:17, 60m
    Down 26 (2026-07-01 2203 UTC) :crit, down-26, 2026-07-01T22:03:46, 60m
    Down 27 (2026-07-01 2350 UTC) :crit, down-27, 2026-07-01T23:50:52, 60m
    Down 28 (2026-07-02 0328 UTC) :crit, down-28, 2026-07-02T03:28:33, 60m
    Down 29 (2026-07-02 0655 UTC) :crit, down-29, 2026-07-02T06:55:07, 60m
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
| Down | 2026-06-29 20:23 UTC | Unknown after last seen 2026-06-30 00:03 UTC | Observed 3h 40m | battery_config, evse_control, evse_scheduler, session_history |
| Down | 2026-06-30 04:43 UTC | Unknown after last seen 2026-06-30 04:43 UTC | Observed 0m | battery_config, evse_control, evse_scheduler, session_history |
| Down | 2026-06-30 08:28 UTC | Unknown after last seen 2026-06-30 08:28 UTC | Observed 0m | battery_config, evse_control, evse_scheduler, session_history |
| Down | 2026-06-30 11:29 UTC | Unknown after last seen 2026-06-30 11:29 UTC | Observed 0m | battery_config, evse_control, evse_scheduler, session_history |
| Down | 2026-06-30 13:24 UTC | Unknown after last seen 2026-06-30 13:24 UTC | Observed 0m | battery_config, evse_control, evse_scheduler, session_history |
| Down | 2026-06-30 16:03 UTC | Unknown after last seen 2026-06-30 16:03 UTC | Observed 0m | battery_config, evse_control, evse_scheduler, session_history |
| Down | 2026-06-30 18:18 UTC | Unknown after last seen 2026-06-30 18:18 UTC | Observed 0m | battery_config, evse_control, evse_scheduler, session_history |
| Down | 2026-06-30 20:28 UTC | Unknown after last seen 2026-06-30 23:20 UTC | Observed 2h 51m | battery_config, evse_control, evse_scheduler, session_history |
| Down | 2026-07-01 01:39 UTC | Unknown after last seen 2026-07-01 01:39 UTC | Observed 0m | battery_config, evse_control, evse_scheduler, session_history |
| Down | 2026-07-01 06:25 UTC | Unknown after last seen 2026-07-01 06:25 UTC | Observed 0m | battery_config, evse_control, evse_scheduler, session_history |
| Down | 2026-07-01 10:29 UTC | Unknown after last seen 2026-07-01 10:29 UTC | Observed 0m | battery_config, evse_control, evse_scheduler, session_history |
| Down | 2026-07-01 13:18 UTC | Unknown after last seen 2026-07-01 13:18 UTC | Observed 0m | battery_config, evse_control, evse_scheduler, session_history |
| Down | 2026-07-01 16:04 UTC | Unknown after last seen 2026-07-01 16:04 UTC | Observed 0m | battery_config, evse_control, evse_scheduler, session_history |
| Down | 2026-07-01 18:24 UTC | Unknown after last seen 2026-07-01 18:24 UTC | Observed 0m | battery_config, evse_control, evse_scheduler, session_history |
| Down | 2026-07-01 20:27 UTC | Unknown after last seen 2026-07-01 20:27 UTC | Observed 0m | battery_config, evse_control, evse_scheduler, session_history |
| Down | 2026-07-01 22:03 UTC | Unknown after last seen 2026-07-01 22:03 UTC | Observed 0m | battery_config, evse_control, evse_scheduler, session_history |
| Down | 2026-07-01 23:50 UTC | Unknown after last seen 2026-07-01 23:50 UTC | Observed 0m | battery_config, evse_control, evse_scheduler, session_history |
| Down | 2026-07-02 03:28 UTC | Unknown after last seen 2026-07-02 03:28 UTC | Observed 0m | battery_config, evse_control, evse_scheduler, session_history |
| Down | 2026-07-02 06:55 UTC | Ongoing (last seen 2026-07-02 06:55 UTC) | Observed at latest check | battery_config, evse_control, evse_scheduler, session_history |

## Raw Artifacts

- [Current status.json](https://raw.githubusercontent.com/barneyonline/ha-enphase-energy/service-status/status.json)
- [30-day history.json](https://raw.githubusercontent.com/barneyonline/ha-enphase-energy/service-status/history.json)
- [30-day incidents.json](https://raw.githubusercontent.com/barneyonline/ha-enphase-energy/service-status/incidents.json)

