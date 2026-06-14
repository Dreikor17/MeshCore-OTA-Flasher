"""
Buttonless DFU entry for a MeshCore node that has been armed with `start ota`.

Choreography:
  1. Connect to the app-mode device (advertises e.g. RAK4631_OTA), notifications on.
  2. Write a single byte 0x01 to the Control Point — MeshCore's BLEDfu sets
     GPREGRET=0xB1 and hands off to the bootloader. The link drops.
  3. The bootloader re-advertises on a DIFFERENT MAC with a *_DFU name. Rescan and
     return that target (never reuse the old address/connection).
"""

from __future__ import annotations

import asyncio

from . import constants as C
from .ble_transport import BleTransport
from .scanner import FoundDevice, scan


async def send_buttonless_trigger(transport: BleTransport, log) -> None:
    log("Sending buttonless DFU trigger (0x01); the node will reboot into its bootloader...")
    try:
        await transport.write_ctrl(bytes([C.OP_START_DFU]))
    except Exception as e:
        # The device often resets mid-write, so a write error here is usually benign.
        log(f"(trigger write returned '{e}' — expected if the node reset immediately)")
    await asyncio.sleep(1.5)  # let it tear down and the bootloader come up


async def find_bootloader(
    exclude_address: str | None = None,
    attempts: int = 8,
    per_scan: float = 4.0,
    log=None,
    ota_name: str | None = None,
) -> FoundDevice | None:
    log = log or (lambda *_: None)
    excl = (exclude_address or "").lower()
    ota = (ota_name or "").upper()
    for i in range(attempts):
        log(f"Scanning for the bootloader DFU target (attempt {i + 1}/{attempts})...")
        devs = await scan(timeout=per_scan)
        cands = [d for d in devs if d.is_dfu_bootloader and d.address.lower() != excl]
        if cands:
            best = cands[0]
            log(f"Found bootloader: {best.name} [{best.address}] RSSI {best.rssi}")
            return best

        # --- diagnostics: explain WHY nothing matched this round ---
        svc = [d for d in devs if d.has_dfu_service]
        if svc:
            log("  0x1530 DFU-service advertisers: "
                + ", ".join(f"{d.name}[{d.address}]" for d in svc))
        if ota:
            still = [d for d in devs if ota in (d.name or "").upper()]
            if still:
                log(f"  '{ota_name}' is STILL advertising [{still[0].address}] — the node "
                    "has NOT entered its bootloader (the trigger did not take effect).")
        top = sorted(devs, key=lambda d: -d.rssi)[:8]
        log("  nearby: " + ", ".join(f"{d.name}({d.rssi})" for d in top))
        await asyncio.sleep(1.0)
    return None
