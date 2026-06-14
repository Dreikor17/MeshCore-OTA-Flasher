"""
Orchestrates the full flash flow and reports progress to the GUI via Qt signals.

Flow (decided per the selected device's mode):
  * Device already in bootloader DFU mode  -> connect and flash directly.
  * MeshCore node armed with `start ota`    -> connect, send buttonless trigger,
                                               rescan for the *_DFU bootloader, flash.
"""

from __future__ import annotations

import asyncio
import time

from PySide6.QtCore import QObject, Signal

from . import buttonless
from . import constants as C
from .ble_transport import BleDisconnected, BleTransport, get_adapter_names
from .dfu_legacy import DfuDeviceShort, DfuError, DfuInvalidState, LegacyDfu
from .dfu_package import DfuImage
from .scanner import FoundDevice


class FlashController(QObject):
    log = Signal(str)
    phase = Signal(str)
    progress = Signal(int, int, float)  # sent, total, bits/sec
    finished = Signal(bool, str)        # ok, message

    async def flash(
        self,
        device: FoundDevice,
        image: DfuImage,
        skip_trigger: bool,
        prn: int,
        reliable: bool = False,
        verbose: bool = False,
    ) -> None:
        transport: BleTransport | None = None
        t0 = time.monotonic()
        try:
            target = device

            need_trigger = not skip_trigger and not device.is_dfu_bootloader
            if need_trigger:
                self.phase.emit("Arming OTA mode")
                self.log.emit(f"Connecting to {device.name} [{device.address}] to arm DFU...")
                trig = BleTransport(device.ble_device or device.address, log=self.log.emit)
                await trig.connect()
                await buttonless.send_buttonless_trigger(trig, self.log.emit)
                await trig.disconnect()

                self.phase.emit("Waiting for bootloader")
                target = await buttonless.find_bootloader(
                    log=self.log.emit, ota_name=device.name
                )
                if target is None:
                    raise DfuError(
                        "The bootloader DFU target never appeared. Make sure you ran "
                        "'start ota' on the node (admin CLI), or on an OTAFIX device hold "
                        "the button next to the D-pad and tap reset, then scan again."
                    )
            else:
                self.log.emit(f"Device is already in DFU/bootloader mode: {device.name}")

            # Fallback ladder: try the fast high-MTU path first, then progressively smaller
            # chunks, ending at the proven 20-byte stock path so the flash always completes
            # if the hardware works at all. Each rung is a full reconnect + fresh START_DFU.
            # The bootloader (SD+BL) flash is brick-sensitive and many adapters can't sustain
            # high-MTU writes for it, so always use the reliable 20-byte path for it.
            if image.is_bootloader and not reliable:
                reliable = True
                self.log.emit("Bootloader package — using the reliable 20-byte path.")
            # Reliable mode skips the high-MTU rungs and goes straight to the proven 20-byte path.
            rungs = [C.MIN_CHUNK] if reliable else [None, 128, C.MIN_CHUNK]
            max_last_rung_tries = 3  # retry the reliable rung on transient failures
            last_rung_tries = 0
            max_resets = 4  # each failed attempt wedges the state; allow a reset per retry
            resets_used = 0
            dfu = None
            manual_reset = False
            i = 0
            while i < len(rungs):
                cap = rungs[i]
                if transport is None:
                    self.phase.emit("Connecting to bootloader")
                    transport = await self._connect_bootloader(target)
                transport.set_chunk_cap(cap)
                self.phase.emit("Flashing")
                dfu = LegacyDfu(
                    transport,
                    image,
                    prn=prn,
                    log=self.log.emit,
                    progress=lambda s, t, b: self.progress.emit(s, t, b),
                    verbose=verbose,
                )
                try:
                    manual_reset = await dfu.run()
                    break
                except DfuDeviceShort:
                    # A window was lost in transit. On a high-MTU rung, step down to the proven
                    # smaller chunk (write-without-response fragmentation can overrun). On the
                    # last/reliable rung, don't retry the same thing — fail fast with guidance.
                    if i < len(rungs) - 1:
                        try:
                            await transport.disconnect()
                        except Exception:
                            pass
                        transport = None
                        await asyncio.sleep(2.5)
                        self.log.emit(
                            "Device ended a window short — stepping down to a smaller, more "
                            "reliable chunk size..."
                        )
                        i += 1
                        continue
                    raise  # last/reliable rung → clean fail-fast handler with guidance
                except DfuInvalidState:
                    # Bootloader wedged in a non-IDLE state (a prior transfer aborted
                    # mid-stream). Only a reset clears it — not a smaller chunk.
                    if resets_used >= max_resets:
                        raise DfuError(
                            "The bootloader is stuck in a non-IDLE DFU state and did not "
                            "recover automatically. Power-cycle the node (or double-tap RESET) "
                            "and flash again."
                        )
                    resets_used += 1
                    self.log.emit(
                        "Bootloader stuck (INVALID_STATE) from a prior aborted transfer — "
                        "sending a reset and reconnecting to clear it..."
                    )
                    self.phase.emit("Resetting bootloader")
                    try:
                        await dfu.send_sys_reset()
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        await transport.disconnect()
                    except Exception:
                        pass
                    transport = None
                    await asyncio.sleep(C.SYS_RESET_SETTLE_S)
                    refreshed = await buttonless.find_bootloader(
                        attempts=4, per_scan=4.0, log=self.log.emit
                    )
                    if refreshed is not None:
                        target = refreshed
                    continue  # retry the SAME rung after the reset
                except (DfuError, BleDisconnected) as e:
                    if not self._retryable_stream_error(e):
                        raise
                    try:
                        await transport.disconnect()
                    except Exception:
                        pass
                    transport = None
                    await asyncio.sleep(2.5)
                    if i < len(rungs) - 1:
                        self.log.emit(
                            f"Transfer failed ({e}). Retrying at a smaller, more reliable chunk size..."
                        )
                        i += 1  # step down to a smaller chunk
                    else:
                        # Last (most reliable) rung: retry it a few times for transient drops.
                        last_rung_tries += 1
                        if last_rung_tries >= max_last_rung_tries:
                            raise
                        self.log.emit(
                            f"Transfer failed ({e}). Retrying the reliable path "
                            f"(attempt {last_rung_tries + 1}/{max_last_rung_tries})..."
                        )

            await self._log_summary(dfu, t0)
            self.phase.emit("Complete")
            if image.is_bootloader:
                self.finished.emit(True, C.BOOTLOADER_FLASHED_MSG)
            elif manual_reset:
                self.finished.emit(True, C.STOCK_BOOTLOADER_HANG_MSG)
            else:
                self.finished.emit(
                    True, "Firmware flashed and validated — the device is rebooting into it."
                )
        except DfuDeviceShort as e:
            # Streamed fully but the device is short — surface the actionable battery guidance
            # cleanly (no exception-type prefix) instead of a cryptic INVALID_STATE.
            self.phase.emit("Failed")
            self.finished.emit(False, str(e))
        except BleDisconnected as e:
            # Only escapes from a pre-ACTIVATE phase, so the old image is untouched.
            self.phase.emit("Failed")
            self.finished.emit(
                False,
                f"BLE link dropped mid-flash: {e} The previous firmware is unchanged "
                "(ACTIVATE was never sent). Re-scan and try again.",
            )
        except Exception as e:  # noqa: BLE001 - surface everything to the user
            self.phase.emit("Failed")
            self.finished.emit(False, f"{type(e).__name__}: {e}")
        finally:
            if transport is not None:
                await transport.disconnect()

    @staticmethod
    def _retryable_stream_error(e: Exception) -> bool:
        """True for PRE-ACTIVATE streaming failures safe to retry at a smaller chunk (the
        old image is intact because ACTIVATE was never sent). Excludes e.g. device_type."""
        if isinstance(e, BleDisconnected):
            return True
        msg = str(e).lower()
        return any(k in msg for k in ("packet-receipt", "crc", "timed out", "dropped"))

    async def _log_summary(self, dfu: LegacyDfu, t0: float) -> None:
        """Emit a post-flash summary: BLE adapter, transfer time, average bitrate."""
        try:
            adapters = await get_adapter_names()
        except Exception:  # noqa: BLE001
            adapters = []
        adapter = ", ".join(adapters) if adapters else "(unknown)"
        secs = dfu.transfer_seconds
        kib_s = dfu.avg_bps / 8 / 1024
        kbit_s = dfu.avg_bps / 1000
        total = time.monotonic() - t0
        self.log.emit("=== Flash summary ===")
        self.log.emit(f"BLE adapter: {adapter}")
        self.log.emit(
            f"Transfer: {dfu.transfer_bytes / 1024:.1f} KiB in {secs:.1f} s "
            f"(avg {kib_s:.1f} KiB/s · {kbit_s:.0f} kbit/s)"
        )
        self.log.emit(f"Total flash time: {total:.1f} s")

    async def _connect_bootloader(self, target: FoundDevice, attempts: int = 4) -> BleTransport:
        """Connect to the freshly-rebooted bootloader, retrying with a re-scan.

        The bootloader advertises on a new random address that has only just appeared,
        which the WinRT stack can fail to resolve (0x8000FFFF "Catastrophic failure").
        Passing the live BLEDevice from a fresh scan — and re-scanning between attempts to
        refresh that handle — is the reliable approach.
        """
        if C.STOCK_DFU_NAME in (target.name or "").upper():
            self.log.emit(
                "Note: stock 'AdaDFU' bootloader detected. If the node is on USB it may not "
                "auto-reboot after flashing (known stock-bootloader issue) — flashing on "
                "battery with USB unplugged avoids it; OTAFIX fixes it permanently."
            )
        last_err: Exception | None = None
        current = target
        for i in range(attempts):
            self.log.emit(
                f"Connecting to bootloader {current.name} [{current.address}] "
                f"(attempt {i + 1}/{attempts})..."
            )
            t = BleTransport(current.ble_device or current.address, log=self.log.emit)
            try:
                await t.connect()
                return t
            except Exception as e:  # noqa: BLE001
                last_err = e
                self.log.emit(f"Connect failed: {type(e).__name__}: {e}")
                try:
                    await t.disconnect()
                except Exception:
                    pass
                if i < attempts - 1:
                    await asyncio.sleep(2.5)
                    refreshed = await buttonless.find_bootloader(
                        attempts=2, per_scan=4.0, log=self.log.emit
                    )
                    if refreshed is not None:
                        current = refreshed
        raise DfuError(
            f"Could not connect to the bootloader after {attempts} attempts "
            f"({type(last_err).__name__ if last_err else 'unknown'}: {last_err}). "
            "Move closer to the node, or remove it under Windows Settings > Bluetooth "
            "and scan again."
        )
