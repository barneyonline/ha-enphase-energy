# IQ EV Charger device triggers

IQ EV Chargers provide these Home Assistant device triggers:

- `charging_started`
- `charging_stopped`
- `plugged_in`
- `unplugged`

Device-trigger automation YAML uses `platform: device`, `domain: enphase_ev`,
`device_id:`, `entity_id:`, and `type:`. For example:

```yaml
trigger:
  - platform: device
    domain: enphase_ev
    device_id: DEVICE_ID
    entity_id: ENTITY_ID
    type: charging_started
```

The integration does not provide custom automation conditions. Use Home
Assistant's standard device or entity-state conditions with Enphase Energy
entities.
