"""Exercise sparse Activation responses and retained catalogue state."""

from custom_components.enphase_ev.grid_profile_runtime import GridProfileRuntime

from .test_grid_profile_runtime import _FakeCoordinator, _FakeGridProfileClient


async def test_retained_region_survives_missing_activation_address() -> None:
    client = _FakeGridProfileClient()
    runtime = GridProfileRuntime(_FakeCoordinator(client))
    await runtime.async_refresh(force=True)
    client.activation_record.pop("address")
    await runtime.async_refresh(force=True)
    assert runtime.staged_region_code == runtime.site_region_code == "VIC"


async def test_catalogue_lookup_without_staged_region_and_expired_cache() -> None:
    client = _FakeGridProfileClient()
    runtime = GridProfileRuntime(_FakeCoordinator(client))
    await runtime.async_refresh(force=True)
    runtime.staged_region_code = None
    assert runtime.profile_for_id("agf:common").profile_id == "agf:common"
    runtime.staged_region_code = "VIC"
    key = ("AU", "VIC", True)
    runtime.catalog_cache[key] = (0.0, runtime.catalog_cache[key][1])
    requests = len(client.profile_requests)
    assert await runtime.async_load_profiles()
    assert len(client.profile_requests) == requests + 1


async def test_apply_fetches_missing_profile_from_common_catalogue() -> None:
    client = _FakeGridProfileClient()
    runtime = GridProfileRuntime(_FakeCoordinator(client))
    await runtime.async_refresh(force=True)
    runtime.catalog_cache.clear()
    client.profile_requests.clear()
    await runtime.async_apply_grid_profile("agf:common", region_code="VIC")
    assert client.profile_requests == [("AU", "VIC", True)]
    assert client.apply_requests[-1]["profile_id"] == "agf:common"


def test_sparse_activation_records_and_optional_settings() -> None:
    client = _FakeGridProfileClient()
    runtime = GridProfileRuntime(_FakeCoordinator(client))
    assert runtime._find_country_code([{}]) is None
    assert runtime._find_region_code([{}]) is None
    assert (
        runtime._envoy_serial_from_record(
            {"envoyCombiner": {"empty": [" "], "missing": None, "valid": ["GW"]}}
        )
        == "GW"
    )
    runtime._parse_activation_devices([{}])
    assert not runtime.gateway_targets
    client.activation_settings_grid_profiles = lambda: None
    assert not runtime._sync_settings_profiles()
    client.activation_settings_grid_profiles = lambda: [("", "Profile"), ("GW", "")]
    assert not runtime._sync_settings_profiles()
