"""What the rule validator and the dry run need to know about one home, read through the household's own data port."""
from __future__ import annotations

from collections.abc import Mapping, Sequence

from app.agents_platform.automation.readings import Reading, reading_from_record
from app.agents_platform.automation.validation import DeviceSpec, ValidationContext

DEFAULT_TIMEZONE = "Asia/Kolkata"
DRY_RUN_RECORDS = 2000  # the most the energy service returns


def validation_context(profile, *, timezone: str | None) -> ValidationContext:
    """Devices are keyed by the telemetry `device_id`, the key a rule's device metrics and actions use."""
    devices = [
        DeviceSpec(key=d.device_id, critical=bool(d.critical), controllable=bool(d.controllable)) for d in profile.devices
    ]
    return ValidationContext.with_devices(
        devices,
        home_timezone=timezone or DEFAULT_TIMEZONE,
        battery_present=bool(profile.battery_present),
    )


def device_names(profile) -> Mapping[str, str]:
    return {d.device_id: d.device_name.lower() for d in profile.devices}


def readings_from(records: Sequence) -> list[Reading]:
    out: list[Reading] = []
    for record in records:
        try:
            out.append(reading_from_record(record))
        except ValueError:
            continue  # a record without a usable timestamp is skipped, never fatal to a dry run
    return out
