import pytest


@pytest.mark.asyncio
async def test_summary_v2_enrichment(hass, monkeypatch):
    from custom_components.enphase_ev.const import (
        CONF_COOKIE,
        CONF_EAUTH,
        CONF_SCAN_INTERVAL,
        CONF_SERIALS,
        CONF_SITE_ID,
    )
    from custom_components.enphase_ev.coordinator import EnphaseCoordinator

    cfg = {
        CONF_SITE_ID: "1234567",
        CONF_SERIALS: ["555555555555"],
        CONF_EAUTH: "EAUTH",
        CONF_COOKIE: "COOKIE",
        CONF_SCAN_INTERVAL: 30,
    }
    from custom_components.enphase_ev import coordinator as coord_mod

    monkeypatch.setattr(
        coord_mod, "async_get_clientsession", lambda *args, **kwargs: object()
    )
    coord = EnphaseCoordinator(hass, cfg)
    coord._has_successful_refresh = True  # noqa: SLF001

    status_payload = {
        "evChargerData": [
            {
                "sn": "555555555555",
                "name": "IQ EV Charger",
                "connected": True,
                "pluggedIn": False,
                "charging": False,
                "faulted": False,
                "lst_rpt_at": None,
                "connectors": [
                    {
                        "connectorId": 1,
                        "connectorStatusType": "AVAILABLE",
                        "connectorStatusReason": "INSUFFICIENT_SOLAR",
                        "connectorStatusInfo": "DETAILS",
                        "dlbActive": False,
                    }
                ],
                "offlineAt": "2025-09-08T02:00:00Z",
            }
        ],
        "ts": 1757299870275,
    }

    summary_list = [
        {
            "serialNumber": "555555555555",
            "displayName": "Garage Charger",
            "lastReportedAt": "2025-09-08T02:55:30.347Z[UTC]",
            "chargeLevelDetails": {
                "min": "6",
                "max": "32",
                "granularity": "1",
                "defaultChargeLevel": "disabled",
            },
            "maxCurrent": 32,
            "phaseMode": 1,
            "status": "NORMAL",
            "dlbEnabled": 1,
            "activeConnection": "ethernet",
            "networkConfig": '[\n"netmask=255.255.255.0,mac_addr=00:1d:c0:e1:23:1d,interface_name=eth0,connectionStatus=1,ipaddr=192.168.1.184,bootproto=dhcp,gateway=192.168.1.1",\n"netmask=,mac_addr=,interface_name=mlan0,connectionStatus=0,ipaddr=,bootproto=dhcp,gateway="\n]',
            "reportingInterval": "300",
            "lifeTimeConsumption": 39153.87,
            "commissioningStatus": 1,
            # Additional metadata for DeviceInfo enrichment (placeholder values)
            "firmwareVersion": "1.2.3",
            "processorBoardVersion": "A.B.C",
            "modelName": "MODEL-NAME",
            "sku": "MODEL-SKU-0000",
            "supportsUseBattery": True,
        }
    ]

    class StubClient:
        async def status(self):
            return status_payload

        async def summary_v2(self):
            return summary_list

        async def charge_mode(self, sn: str):
            return "MANUAL_CHARGING"

    coord.client = StubClient()

    data = await coord._async_update_data()
    st = data["555555555555"]

    assert st["min_amp"] == 6
    assert st["max_amp"] == 32
    assert st["max_current"] == 32
    assert st["amp_granularity"] == 1
    assert st["phase_mode"] == 1
    assert st["status"] == "NORMAL"
    assert st["commissioned"] is True
    assert st["dlb_enabled"] is True
    assert st["dlb_active"] is False
    assert st["connection"] == "ethernet"
    assert st["ip_address"] == "192.168.1.184"
    assert st["reporting_interval"] == 300
    assert st["connector_status_info"] == "DETAILS"
    # lifetime consumption normalized to kWh if backend returns Wh-like values
    assert st["lifetime_kwh"] == pytest.approx(39.154, abs=1e-3)
    # last_reported_at should come from summary
    assert "last_reported_at" in st and st["last_reported_at"].startswith("2025-09-08")
    # charge mode cached/derived value
    assert st["charge_mode"] == "MANUAL_CHARGING"
    # Optional device metadata mapped from summary_v2
    assert st["sw_version"] == "1.2.3"
    assert st["hw_version"] == "A.B.C"
    assert st["model_name"] == "MODEL-NAME"
    assert st["model_id"] == "MODEL-SKU-0000"
    assert st["display_name"] == "Garage Charger"
    assert st["green_battery_supported"] is True
    assert st["default_charge_level"] == "disabled"
    assert "default_charge_level_supported" not in st
    assert st["energy_today_sessions"] == []
    assert st["energy_today_sessions_kwh"] == 0.0
