# Descriptive Activity entries

Descriptive entries are **disabled by default**. Enable **Configure →
Notifications → Enable Descriptive Activity Entries** to add descriptions to Home
Assistant Activity when reported conditions change for enabled status entities.
Open an entry to see its message in the **Event** row of the Activity details popup. These entries accompany normal state changes;
entity states and automation behavior stay unchanged.

Coverage includes **System Events**, **Service Status**, cloud reachability/errors,
charger status, gateway and microinverter connectivity, battery overall status,
Export Limit requests and confirmation, and heat-pump status/SG Ready details.
Messages include available reported facts and recovery. System Events includes
sanitized Enphase fault descriptions when supplied; missing descriptions do not
imply a known fault cause. Export Limit is confirmed only by matching gateway
readback, and request outcomes retain the requested target.

The first valid observation after setup, reload, or entity discovery establishes
a quiet baseline. Routine report times, retry countdowns and repeated polls do
not create entries. Messages use Home Assistant's configured language and timezone
and follow its Activity filters and Recorder retention. No additional polling or
frontend installation is required. Turning the option on establishes a new quiet
baseline; turning it off stops new descriptive entries. Existing Activity entries
remain subject to Recorder retention.
