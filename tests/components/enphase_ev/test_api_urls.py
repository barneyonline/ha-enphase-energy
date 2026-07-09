from unittest.mock import MagicMock

import pytest

from custom_components.enphase_ev.api import EnphaseEVClient
from tests.components.enphase_ev.random_ids import RANDOM_SERIAL, RANDOM_SITE_ID


class StubClient(EnphaseEVClient):
    def __init__(self, site_id=RANDOM_SITE_ID):
        self.calls = []
        super().__init__(MagicMock(), site_id, "EAUTH", "COOKIE")

    async def _json(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs.get("json"), kwargs.get("data")))
        if "/service/evse_management/fwDetails/" in url:
            return []
        return {"status": "ok"}


@pytest.mark.asyncio
async def test_api_builds_urls_correctly():
    c = StubClient(site_id=RANDOM_SITE_ID)
    await c.status()
    await c.battery_status()
    await c.dry_contacts_settings()
    await c.grid_control_check()
    await c.off_grid_due_to_grid_outage()
    await c.request_grid_toggle_otp()
    await c.validate_grid_toggle_otp("1234")
    await c.set_grid_state("ENV123", 1)
    await c.log_grid_change("ENV123", "OLD", "NEW")
    await c.battery_backup_history()
    await c.inverters_inventory()
    await c.inverter_status()
    await c.inverter_production(start_date="2022-01-01", end_date="2026-01-01")
    await c.opt_out_storm_alert(alert_id="IDV21037", name="Severe Weather")
    await c.start_charging(RANDOM_SERIAL, 32, connector_id=1)
    await c.stop_charging(RANDOM_SERIAL)
    await c.trigger_message(RANDOM_SERIAL, "MeterValues")
    await c.evse_fw_details()
    await c.evse_feature_flags(country="DE")

    methods_urls = [(method, url) for (method, url, _, _) in c.calls]
    # First call should hit the primary status endpoint
    assert methods_urls[0][0] == "GET"
    assert (
        f"/service/evse_controller/{RANDOM_SITE_ID}/ev_chargers/status"
        in methods_urls[0][1]
    )
    assert methods_urls[1][0] == "GET"
    assert f"/pv/settings/{RANDOM_SITE_ID}/battery_status.json" in methods_urls[1][1]
    assert methods_urls[2][0] == "GET"
    assert f"/pv/settings/{RANDOM_SITE_ID}/dry_contacts" in methods_urls[2][1]
    assert methods_urls[3][0] == "GET"
    assert f"/app-api/{RANDOM_SITE_ID}/grid_control_check.json" in methods_urls[3][1]
    assert methods_urls[4][0] == "GET"
    assert (
        f"/app-api/{RANDOM_SITE_ID}/off_grid_due_to_grid_outage" in methods_urls[4][1]
    )
    assert methods_urls[5][0] == "GET"
    assert f"/app-api/{RANDOM_SITE_ID}/grid_toggle_otp.json" in methods_urls[5][1]
    assert methods_urls[6][0] == "POST"
    assert "/app-api/grid_toggle_otp.json" in methods_urls[6][1]
    assert methods_urls[7][0] == "POST"
    assert "/pv/settings/grid_state.json" in methods_urls[7][1]
    assert methods_urls[8][0] == "POST"
    assert "/pv/settings/log_grid_change.json" in methods_urls[8][1]
    assert methods_urls[9][0] == "GET"
    assert (
        f"/app-api/{RANDOM_SITE_ID}/battery_backup_history.json" in methods_urls[9][1]
    )
    assert methods_urls[10][0] == "GET"
    assert f"/app-api/{RANDOM_SITE_ID}/inverters.json" in methods_urls[10][1]
    assert methods_urls[11][0] == "GET"
    assert f"/systems/{RANDOM_SITE_ID}/inverter_status_x.json" in methods_urls[11][1]
    assert methods_urls[12][0] == "GET"
    assert (
        f"/systems/{RANDOM_SITE_ID}/inverter_data_x/energy.json" in methods_urls[12][1]
    )
    opt_out_call = next(
        (
            (method, url)
            for method, url in methods_urls
            if f"/service/batteryConfig/api/v1/stormGuard/{RANDOM_SITE_ID}/stormAlert"
            in url
        ),
        None,
    )
    assert opt_out_call is not None
    assert opt_out_call[0] == "PUT"
    fw_details_call = next(
        (
            (method, url)
            for method, url in methods_urls
            if f"/service/evse_management/fwDetails/{RANDOM_SITE_ID}" in url
        ),
        None,
    )
    assert fw_details_call is not None
    assert fw_details_call[0] == "GET"
    feature_flags_call = next(
        (
            (method, url)
            for method, url in methods_urls
            if "/service/evse_management/api/v1/config/feature-flags" in url
        ),
        None,
    )
    assert feature_flags_call is not None
    assert feature_flags_call[0] == "GET"
    assert f"site_id={RANDOM_SITE_ID}" in feature_flags_call[1]
    assert "country=DE" in feature_flags_call[1]
    # Final five calls should be start/stop/trigger/fwDetails/feature-flags in order.
    start_call = methods_urls[-5]
    stop_call = methods_urls[-4]
    trig_call = methods_urls[-3]
    assert start_call[0] == "POST"
    assert (
        f"/service/evse_controller/{RANDOM_SITE_ID}/ev_chargers/{RANDOM_SERIAL}/start_charging"
        in start_call[1]
    )
    assert stop_call[0] == "PUT"
    assert (
        f"/service/evse_controller/{RANDOM_SITE_ID}/ev_chargers/{RANDOM_SERIAL}/stop_charging"
        in stop_call[1]
    )
    assert trig_call[0] == "POST"
    assert (
        f"/service/evse_controller/{RANDOM_SITE_ID}/ev_charger/{RANDOM_SERIAL}/trigger_message"
        in trig_call[1]
    )

    _, _, payload, _ = c.calls[-5]
    assert payload == {"chargingLevel": 32, "connectorId": 1}
    _, _, trigger_payload, _ = c.calls[-3]
    assert trigger_payload == {"requestedMessage": "MeterValues"}

    opt_out_payload = next(
        (
            payload
            for method, url, payload, _data in c.calls
            if method == "PUT"
            and f"/service/batteryConfig/api/v1/stormGuard/{RANDOM_SITE_ID}/stormAlert"
            in url
        ),
        None,
    )
    assert opt_out_payload == {
        "stormAlerts": [
            {"id": "IDV21037", "name": "Severe Weather", "status": "opted-out"}
        ]
    }


@pytest.mark.asyncio
async def test_trigger_message_rejects_unsupported_requested_message():
    c = StubClient(site_id=RANDOM_SITE_ID)

    with pytest.raises(ValueError, match="Unsupported OCPP trigger message"):
        await c.trigger_message(RANDOM_SERIAL, "DataTransfer")

    assert c.calls == []
