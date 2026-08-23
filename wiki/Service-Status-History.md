# Service Status History

- Current status: **Fully Operational**
- Last updated: `2026-08-23 10:49 UTC`
- Failed checks in latest run: `0`
- Latest failed checks: None
- Retained hourly samples: `626`
- Incident windows in last 30 days: `2`

This page is generated from hourly synthetic checks against Enphase cloud endpoints. It may miss incidents that begin and recover between checks.

## Incident Timeline

```mermaid
gantt
    title Enphase Service Status Incident Timeline (Last 30 Days)
    dateFormat  YYYY-MM-DDTHH:mm:ss
    axisFormat  %b %d
    Window start :vert, window-start, 2026-07-24T10:49:31, 0ms
    Window end :vert, window-end, 2026-08-23T10:49:31, 0ms
    section Down
    Down 1 (2026-08-04 1810 UTC) :crit, down-1, 2026-08-04T18:10:46, 60m
    section Degraded
    Degraded 1 (2026-08-06 0348 UTC) :active, degraded-1, 2026-08-06T03:48:14, 60m
```

## Incident Summary

| Status | Started (UTC) | Ended (UTC) | Duration | Failed checks |
| --- | --- | --- | --- | --- |
| Down | 2026-08-04 18:10 UTC | Unknown after last seen 2026-08-04 18:10 UTC | Observed 0m | evse_runtime, evse_scheduler |
| Degraded | 2026-08-06 03:48 UTC | Unknown after last seen 2026-08-06 03:48 UTC | Observed 0m | evse_scheduler |

## Raw Artifacts

- [Current status.json](https://raw.githubusercontent.com/barneyonline/ha-enphase-energy/service-status/status.json)
- [30-day history.json](https://raw.githubusercontent.com/barneyonline/ha-enphase-energy/service-status/history.json)
- [30-day incidents.json](https://raw.githubusercontent.com/barneyonline/ha-enphase-energy/service-status/incidents.json)

