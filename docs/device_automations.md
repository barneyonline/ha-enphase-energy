# Device automations

The Enphase Energy integration provides device triggers for IQ EV Chargers.
They are available in Home Assistant's automation editor when the charger has
the matching charging or plugged-in binary sensor.

## Triggers

| Trigger | Fires when |
| --- | --- |
| `charging_started` | The charger charging sensor changes from off to on. |
| `charging_stopped` | The charger charging sensor changes from on to off. |
| `plugged_in` | The charger plugged-in sensor changes to on. |
| `unplugged` | The charger plugged-in sensor changes to off. |

The charging triggers require an actual transition between `off` and `on`:
`charging_started` does not fire when an unavailable sensor first becomes
available as `on`, and `charging_stopped` does not fire when an unavailable
sensor first becomes available as `off`. The plug triggers fire whenever their
sensor changes to the named state.

### Use a trigger in the automation editor

To use one of these triggers:

1. In Home Assistant, go to **Settings** > **Automations & scenes**.
2. Create or edit an automation and add a trigger.
3. Select **Device**, select the IQ EV Charger, and choose the required charging
   or cable trigger.

Home Assistant stores the selected device and entity identifiers in the
automation. If a charger is removed and added again as a different device,
review automations that refer to the old device.

### Use the triggers in YAML

The automation editor generates the installation-specific `device_id` and
`entity_id`. The four trigger configurations have the same fields:

| Field | Description |
| --- | --- |
| `platform` | Required. Must be `device`. |
| `domain` | Required. Must be `enphase_ev`. |
| `device_id` | Required. The IQ EV Charger device-registry identifier. |
| `entity_id` | Required. The matching charging or plugged-in binary sensor. |
| `type` | Required. One of the four trigger types documented below. |

Prefer creating the trigger in the automation editor and then switching to
YAML so Home Assistant fills in the correct identifiers.

#### Charging started

`charging_started` fires when the charger's charging binary sensor changes
from `off` to `on`.

```yaml
triggers:
  - platform: device
    domain: enphase_ev
    device_id: YOUR_CHARGER_DEVICE_ID
    entity_id: binary_sensor.iq_ev_charger_charging
    type: charging_started
```

#### Charging stopped

`charging_stopped` fires when the charger's charging binary sensor changes
from `on` to `off`.

```yaml
triggers:
  - platform: device
    domain: enphase_ev
    device_id: YOUR_CHARGER_DEVICE_ID
    entity_id: binary_sensor.iq_ev_charger_charging
    type: charging_stopped
```

#### Plugged in

`plugged_in` fires when the charger's plugged-in binary sensor changes to
`on`.

```yaml
triggers:
  - platform: device
    domain: enphase_ev
    device_id: YOUR_CHARGER_DEVICE_ID
    entity_id: binary_sensor.iq_ev_charger_plugged_in
    type: plugged_in
```

#### Unplugged

`unplugged` fires when the charger's plugged-in binary sensor changes to
`off`.

```yaml
triggers:
  - platform: device
    domain: enphase_ev
    device_id: YOUR_CHARGER_DEVICE_ID
    entity_id: binary_sensor.iq_ev_charger_plugged_in
    type: unplugged
```

## Conditions

The integration does not provide custom automation conditions. Use Home
Assistant's standard device or entity state conditions with the charger
entities, such as requiring the plugged-in binary sensor to be on before a
charging action runs.
