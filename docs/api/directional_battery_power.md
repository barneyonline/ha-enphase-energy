# Directional battery power source validation

The four site-level sensors share the IQ Battery device and the default-on
**Enable Directional Battery Energy** setting introduced in [#1029].
Each would report a nonnegative source-window average in watts. The Energy
Dashboard assumes omitted directions are zero, so configure all four together
when they become available. These are the Home Assistant 2026.12 power inputs.

| Sensor | Enphase `lifetime_energy` counter | Home Assistant `power_config` key |
| --- | --- | --- |
| Grid to Battery Power | `grid_battery` | `stat_rate_from_grid` |
| Solar to Battery Power | `solar_battery` | `stat_rate_from_solar` |
| Battery to Home Power | `battery_home` | `stat_rate_to_home` |
| Battery to Grid Power | `battery_grid` | `stat_rate_to_grid` |

The calculator uses raw Wh from an accepted daily response and elapsed time
between Enphase report timestamps. For example, 50 Wh over 300 seconds is
600 W. Reports under five minutes accumulate toward the minimum window.
Daily bucket width describes the aggregation period and can contain several
closer report timestamps for the current partial day. A valid interval with
unchanged counters can report 0 W. Missing channels or an invalid interval make
the group unavailable. A reconciled decrease in the current day's counters on
an advancing report clock discards both estimates and seeds a new power baseline.
Power resumes after a full, reconciled window following that baseline. The
counters for one or both directions can fall together with their direct
aggregate during a cloud dropout. The group waits for the affected counters to
recover to their pre-drop levels or for the site to enter a new day. It then
seeds again. On reload, the saved same-day energy counters protect against
starting in the middle of a dropout. A revision to a closed day needs a second
advancing report with the same revised history before power can reseed. The
cumulative energy history retains its own persisted baseline. The group stops
reporting after 30 minutes without a fresh accepted window.
**Use 15-minute Power Average** applies the same rolling,
elapsed-time-weighted averaging used by Battery Power. Cloud updates can arrive
late, and the five-minute refresh cadence adds up to roughly another poll
interval under normal conditions. The source delay itself remains unmeasured.

## Evidence available

- [#1029] summarizes two successive sanitized `lifetime_energy` responses from
  one site. Lifetime charge and discharge reconciled within 0.05% and 0.11%,
  respectively. The original capture analysis verified 1,572 date-aligned
  positions per counter across each response. All changes were confined to
  the current partial-day bucket. The two report times were 10:04:14 and
  10:09:17 AEDT on 28 November 2026, 303 seconds apart.
- The full JSON files were unavailable in this checkout. The recorded
  partial-day values below are replayed in the tests. Charge and discharge
  each had a 0 Wh window residual (0 W). Grid-to-battery averaged 35.6 W.
  The three other directions stayed unchanged. The absolute discharge
  totals differed by 1 Wh in both snapshots. This is one observed window
  from one site, with no active solar charging or discharge.
- Replaying the documented excerpts yielded one lifetime cursor with zero
  numeric battery observations. The separate current-day response reconciled
  71 + 0 = 71 Wh of charge and 2,139 + 18 = 2,157 Wh of discharge. Its three
  visible numeric quarter-hour buckets were all zero. The single report
  cursor was 24 slots and 802 seconds after the supplied start. The current-day
  response is a separate endpoint with no successive cursor in the excerpt.
- Constructed test replays cover a partial day, subminimum intervals, duplicate
  and regressed timestamps, delayed reports, rollover, and batched counters.
  A test window with 100 Wh directional charge and 50 Wh direct charge produces
  a 600 W disagreement over 300 s and is discarded. A coordinated batch of
  150 Wh over 900 s produces 600 W and can pass reconciliation even if its
  component energy was posted late. That fixture demonstrates why agreement
  alone cannot establish when the battery flow occurred.

| Wh counter | 10:04:14 AEDT | 10:09:17 AEDT | 303 s delta |
| --- | ---: | ---: | ---: |
| `grid_battery` | 199 | 202 | 3 |
| `solar_battery` | 320 | 320 | 0 |
| Direct `charge` | 519 | 522 | 3 |
| `battery_home` | 3,375 | 3,375 | 0 |
| `battery_grid` | 0 | 0 | 0 |
| Direct `discharge` | 3,376 | 3,376 | 0 |

The implementation checks both directional pairs for every candidate window.
When both sides have activity it uses the lifetime validator's rounding and
attribution allowance, the larger of 1 Wh or 5% of the direct delta. A 1 Wh
residual is 12 W over five minutes, even though its contribution to a lifetime
total is negligible. A directional pair can report zero only when its direct
counter also has zero change. These checks have fixture coverage. The available
recordings do not establish a real-world window error bound. The registration
gate in the integration stays closed.

## Captures needed to enable registration

Collect timestamped, sanitized `lifetime_energy` responses at the existing
polling cadence over active charge, active discharge, and idle periods. Include
observed nonzero grid-to-battery, solar-to-battery,
battery-to-home, and battery-to-grid events, and retain repeated polls through
partial days, site-local midnight, and delayed or backfilled updates. Preserve
the arrays or a contiguous range with its matching start date, the site
timezone, the interval, `update_pending`, `last_report_date`, and each local
receipt time. Keep direct `charge` and `discharge` beside all four raw
directional arrays. A same-period gateway power recording or independently
timestamped battery flow data can identify synchronized backfill. A second
site's captures can show whether the timing behavior generalizes.

For each 5–30 minute source window, record the elapsed time, each raw delta,
the charge and discharge residuals in Wh and W, receipt lag, and any missing
or stalled channel. Identify delayed updates, changes to closed buckets, and
windows where all counters pause or catch up together. Repeat for explicitly
reported quarter-hour lifetime data before supporting that payload shape.

[#1029]: https://github.com/barneyonline/ha-enphase-energy/pull/1029
