"""
Parse and validate a Nordic LEGACY DFU firmware package (firmware.zip).

A legacy DFU .zip contains, flat:
    manifest.json + firmware.bin (raw image) + firmware.dat (init packet)

We parse it ourselves and stream the .dat *verbatim* — we never regenerate it,
because the bootloader validates the device_type / softdevice / CRC inside it.
"""

from __future__ import annotations

import json
import struct
import zipfile
from dataclasses import dataclass, field

from . import constants as C

UF2_MAGIC = b"UF2\n"  # first 4 bytes of a UF2 file (uint32 LE 0x0A324655)


class DfuPackageError(Exception):
    """Raised when the selected file is not a usable legacy DFU package."""


@dataclass
class DfuImage:
    mode: int          # image-type bitfield (0x04 app, 0x02 bl, 0x01 sd, or combos)
    sd_size: int
    bl_size: int
    app_size: int
    bin_data: bytes    # concatenated image bytes (sd, bl, app order)
    dat_data: bytes    # init packet, sent verbatim
    manifest: dict = field(default_factory=dict)
    # convenience values parsed out of the .dat:
    device_type: int | None = None
    device_rev: int | None = None
    app_version: int | None = None
    firmware_crc16: int | None = None
    source_name: str = ""

    @property
    def size_block(self) -> bytes:
        """The 12-byte image-size block written to the Packet characteristic after START."""
        return struct.pack("<III", self.sd_size, self.bl_size, self.app_size)

    @property
    def total_size(self) -> int:
        return len(self.bin_data)

    @property
    def mode_label(self) -> str:
        names = []
        if self.mode & C.IMG_SOFTDEVICE:
            names.append("SoftDevice")
        if self.mode & C.IMG_BOOTLOADER:
            names.append("Bootloader")
        if self.mode & C.IMG_APPLICATION:
            names.append("Application")
        return "+".join(names) or f"0x{self.mode:02X}"

    def device_type_ok(self) -> bool:
        # Fail CLOSED: a missing/unparsable device_type cannot be verified, so refuse.
        return self.device_type == C.ADAFRUIT_DEVICE_TYPE

    @property
    def is_bootloader(self) -> bool:
        """True if this package touches the SoftDevice and/or bootloader (brick-sensitive)."""
        return bool(self.mode & (C.IMG_SOFTDEVICE | C.IMG_BOOTLOADER))


def _parse_dat(dat: bytes) -> dict:
    """Best-effort decode of the legacy init packet for display / sanity checks."""
    out: dict = {}
    if len(dat) >= 2:
        out["device_type"] = struct.unpack_from("<H", dat, 0)[0]
    if len(dat) >= 4:
        out["device_rev"] = struct.unpack_from("<H", dat, 2)[0]
    if len(dat) >= 8:
        out["app_version"] = struct.unpack_from("<I", dat, 4)[0]
    if len(dat) >= 2:
        out["firmware_crc16"] = struct.unpack_from("<H", dat, len(dat) - 2)[0]
    return out


def load_dfu_zip(path: str) -> DfuImage:
    with open(path, "rb") as f:
        head = f.read(8)

    if head[:4] == UF2_MAGIC:
        raise DfuPackageError(
            "This is a .uf2 file (USB drag-drop firmware), not a BLE OTA package.\n"
            "Download the ZIP version of the firmware for over-the-air flashing."
        )
    if head[:2] != b"PK":
        raise DfuPackageError(
            "Not a ZIP archive. Select a Nordic DFU firmware .zip (it contains manifest.json)."
        )

    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise DfuPackageError(f"Corrupt ZIP archive: {e}")

    with zf:
        names = set(zf.namelist())
        if "manifest.json" not in names:
            raise DfuPackageError("ZIP has no manifest.json — this is not a Nordic DFU package.")

        try:
            manifest = json.loads(zf.read("manifest.json"))
        except json.JSONDecodeError as e:
            raise DfuPackageError(f"manifest.json is not valid JSON: {e}")

        m = manifest.get("manifest", manifest)
        is_legacy = "dfu_version" in m

        # Pick the image section. Order matters: prefer a combined SD+BL bundle's own key.
        candidates = [
            ("application", C.IMG_APPLICATION),
            ("softdevice_bootloader", C.IMG_SOFTDEVICE | C.IMG_BOOTLOADER),
            ("bootloader", C.IMG_BOOTLOADER),
            ("softdevice", C.IMG_SOFTDEVICE),
        ]
        section = None
        mode = 0
        for key, mval in candidates:
            if isinstance(m.get(key), dict):
                section = m[key]
                mode = mval
                break
        if section is None:
            raise DfuPackageError(
                "manifest.json has no application / bootloader / softdevice image section."
            )

        bin_name = section.get("bin_file")
        dat_name = section.get("dat_file")
        if not bin_name or bin_name not in names:
            raise DfuPackageError(f"Firmware binary '{bin_name}' is missing from the ZIP.")
        if not dat_name or dat_name not in names:
            raise DfuPackageError(f"Init packet '{dat_name}' is missing from the ZIP.")

        bin_data = zf.read(bin_name)
        dat_data = zf.read(dat_name)

    if not is_legacy:
        raise DfuPackageError(
            "This ZIP is a SECURE DFU package (no 'dfu_version' in its manifest). The "
            "Adafruit / OTAFIX bootloader on this hardware needs a LEGACY DFU package — "
            "use the MeshCore-provided firmware .zip."
        )

    # Application images are the common case; combined SoftDevice+Bootloader packages
    # (the OTAFIX over-the-air bootloader update) are also supported. The 12-byte size
    # block must split the concatenated image, so the sd/bl sizes come straight from the
    # manifest and are cross-checked against the binary length below.
    sd_size = bl_size = app_size = 0
    if mode & C.IMG_APPLICATION:
        app_size = len(bin_data)
    elif mode == (C.IMG_SOFTDEVICE | C.IMG_BOOTLOADER):
        sd_size = int(section.get("sd_size", 0) or 0)
        bl_size = int(section.get("bl_size", 0) or 0)
    elif mode == C.IMG_BOOTLOADER:
        bl_size = len(bin_data)
    elif mode == C.IMG_SOFTDEVICE:
        sd_size = len(bin_data)

    if len(bin_data) == 0:
        raise DfuPackageError(f"Firmware binary '{bin_name}' is empty (0 bytes).")
    if len(dat_data) < 12:
        raise DfuPackageError(
            f"Init packet (.dat) is only {len(dat_data)} bytes — too short for a valid "
            "legacy DFU header. The package is truncated or not a legacy DFU package."
        )
    if sd_size + bl_size + app_size != len(bin_data):
        raise DfuPackageError(
            f"Image size block (sd={sd_size}, bl={bl_size}, app={app_size}) does not sum "
            f"to the firmware length ({len(bin_data)} B); refusing an inconsistent image."
        )

    info = _parse_dat(dat_data)
    return DfuImage(
        mode=mode,
        sd_size=sd_size,
        bl_size=bl_size,
        app_size=app_size,
        bin_data=bin_data,
        dat_data=dat_data,
        manifest=manifest,
        device_type=info.get("device_type"),
        device_rev=info.get("device_rev"),
        app_version=info.get("app_version"),
        firmware_crc16=info.get("firmware_crc16"),
        source_name=bin_name,
    )
