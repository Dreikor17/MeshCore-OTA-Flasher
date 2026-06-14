"""BLE discovery and classification of scan results."""

from __future__ import annotations

from dataclasses import dataclass, field

from bleak import BleakScanner

from . import constants as C


@dataclass
class FoundDevice:
    address: str
    name: str
    rssi: int
    service_uuids: list[str] = field(default_factory=list)
    # The live bleak BLEDevice from the scan. Pass THIS (not the address string) to
    # BleakClient on Windows — connecting by a just-appeared random address by string
    # often fails with 0x8000FFFF "Catastrophic failure".
    ble_device: object = None

    @property
    def has_dfu_service(self) -> bool:
        return C.DFU_SERVICE_UUID.lower() in [s.lower() for s in self.service_uuids]

    @property
    def is_dfu_bootloader(self) -> bool:
        """True if the device is already in bootloader DFU mode (flash directly)."""
        name = (self.name or "").upper()
        if any(hint in name for hint in C.DFU_NAME_HINTS):
            return True
        # A bare DFU service with no MeshCore *_OTA name is also a bootloader target.
        return self.has_dfu_service and C.OTA_NAME_HINT not in name

    @property
    def is_ota_armed(self) -> bool:
        """True if a MeshCore node has been armed with `start ota` (needs a trigger)."""
        return C.OTA_NAME_HINT in (self.name or "").upper()

    @property
    def tag(self) -> str:
        if self.is_dfu_bootloader:
            return "DFU bootloader"
        if self.is_ota_armed:
            return "OTA-armed"
        return ""


async def scan(timeout: float = 6.0) -> list[FoundDevice]:
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    devices: list[FoundDevice] = []
    for _addr, (dev, adv) in found.items():
        name = adv.local_name or dev.name or "(unknown)"
        devices.append(
            FoundDevice(
                address=dev.address,
                name=name,
                rssi=adv.rssi if adv.rssi is not None else -127,
                service_uuids=list(adv.service_uuids or []),
                ble_device=dev,
            )
        )
    # MeshCore / DFU targets first, then by signal strength.
    devices.sort(key=lambda d: (not (d.is_dfu_bootloader or d.is_ota_armed), -d.rssi))
    return devices
