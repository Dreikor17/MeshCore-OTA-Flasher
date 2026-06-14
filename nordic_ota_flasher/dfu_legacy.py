"""
The LEGACY Nordic DFU state machine (the brick-sensitive part).

Sequence for an application image, all Control Point writes use write-with-response,
all Packet writes use write-without-response:

  0. (notifications already enabled by the transport)
  1. START      : CtrlPt <- [01, mode]      ; Packet <- 12-byte size block ; await [10,01,01]
  2. INIT       : CtrlPt <- [02, 00]         ; Packet <- .dat bytes ; CtrlPt <- [02, 01] ; await [10,02,01]
  3. PRN        : CtrlPt <- [08, n_lo, n_hi] (optional flow control)
  4. RECEIVE    : CtrlPt <- [03]             ; Packet <- firmware ; (PRN reports every n) ; await [10,03,01]
  5. VALIDATE   : CtrlPt <- [04]             ; await [10,04,01]  (status 05 = CRC error)
  6. ACTIVATE   : CtrlPt <- [05]             ; NO response — wait for the peer to drop the link

Safety: if VALIDATE fails we never send ACTIVATE, so the device keeps its old image.
"""

from __future__ import annotations

import asyncio
import struct
import time

from . import constants as C
from .ble_transport import BleDisconnected, BleTransport
from .dfu_package import DfuImage


class DfuError(Exception):
    pass


class DfuInvalidState(DfuError):
    """The device rejected a command with INVALID_STATE — its DFU state machine is wedged
    in a non-IDLE state (e.g. a prior transfer aborted mid-stream). Recover with SYS_RESET."""


class DfuDeviceShort(DfuError):
    """The whole image streamed but VALIDATE shows the device never reached "received-all" —
    data was lost in transit. Legacy DFU can't retransmit, so retrying the same way just
    re-drops; the fix is environmental (flash on battery — see DEVICE_SHORT_USB_RESET_MSG)."""


class LegacyDfu:
    def __init__(
        self,
        transport: BleTransport,
        image: DfuImage,
        prn: int = C.DEFAULT_PRN,
        log=None,
        progress=None,
        verbose: bool = False,
    ):
        self.t = transport
        self.img = image
        self.prn = max(0, int(prn))
        self._log = log or (lambda *_: None)
        # progress(sent: int, total: int, bits_per_sec: float)
        self._progress = progress or (lambda *_: None)
        self._verbose = verbose
        self._unknown_logged = 0
        self._eff_prn = 0
        self._prn_misses_total = 0  # cumulative multi-second receipt stalls (USB-reset tell)
        # populated by _stream_firmware for the post-flash summary
        self.transfer_bytes = 0
        self.transfer_seconds = 0.0

    def _effective_prn(self, cs: int) -> int:
        """Hold the per-receipt BYTE budget ~constant regardless of MTU. PRN counts packets,
        so big chunks need a smaller PRN (else bytes-in-flight overruns the receiver)."""
        if self.prn <= 0:
            return 0
        if cs <= C.MIN_CHUNK:
            return self.prn
        # cap to the byte-budget at high MTU, but honor a user who set an even lower PRN
        return max(1, min(self.prn, round(C.TARGET_PRN_BYTES / cs)))

    @property
    def avg_bps(self) -> float:
        """Average firmware-transfer rate in bits/sec (0 until streaming completes)."""
        return (self.transfer_bytes * 8 / self.transfer_seconds) if self.transfer_seconds > 0 else 0.0

    # -- notification helpers ----------------------------------------------
    @staticmethod
    def _parse_prn(msg: bytes) -> int | None:
        """Return the bytes-received count if msg is a packet-receipt notification.

        Two wire forms are accepted:
          * Standard Nordic legacy DFU: [0x11, <u32 LE>]  (what the nRF DFU app expects)
          * Adafruit 'bytes received' report variant: [0x10, 0x07, 0x01, <u32 LE>]
        """
        if len(msg) >= 5 and msg[0] == C.OP_PKT_RCPT_NOTIF:
            return int.from_bytes(msg[1:5], "little")
        if len(msg) >= 7 and msg[0] == C.OP_RESPONSE and msg[1] == C.OP_IMAGE_SIZE_REQ:
            return int.from_bytes(msg[3:7], "little")
        return None

    def _log_unknown(self, where: str, msg: bytes) -> None:
        # Surface the raw bytes of unexpected notifications (capped) so the real wire
        # format is visible if any assumption is still off.
        if self._unknown_logged < 8:
            self._unknown_logged += 1
            self._log(f"  [debug] unexpected notification during {where}: {msg.hex(' ')}")

    async def _await_response(self, expected_op: int, timeout: float = 30.0) -> int:
        """Wait for a [0x10, expected_op, status] ack, skipping packet-receipt reports."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DfuError(f"Timed out waiting for ack to op 0x{expected_op:02X}")
            try:
                msg = await self.t.next_notification(remaining)
            except TimeoutError:
                raise DfuError(f"Timed out waiting for ack to op 0x{expected_op:02X}")
            if self._parse_prn(msg) is not None:
                continue  # a packet-receipt / bytes-received report, not a command ack
            if len(msg) >= 3 and msg[0] == C.OP_RESPONSE:
                req, status = msg[1], msg[2]
                if req == expected_op:
                    if status != C.RESP_SUCCESS:
                        msg = (
                            f"Device rejected op 0x{expected_op:02X}: "
                            f"{C.RESP_NAMES.get(status, status)}"
                        )
                        if status == C.RESP_INVALID_STATE:
                            raise DfuInvalidState(msg)
                        raise DfuError(msg)
                    return status
                self._log_unknown(f"ack-wait 0x{expected_op:02X}", msg)
            else:
                self._log_unknown(f"ack-wait 0x{expected_op:02X}", msg)

    async def _await_prn(self, timeout: float = 30.0) -> int:
        """Wait for a packet-receipt notification; return the device's bytes-received count."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DfuError("Timed out waiting for a packet-receipt notification")
            try:
                msg = await self.t.next_notification(remaining)
            except TimeoutError:
                raise DfuError("Timed out waiting for a packet-receipt notification")
            n = self._parse_prn(msg)
            if n is not None:
                return n
            self._log_unknown("firmware streaming", msg)

    # -- main flow ----------------------------------------------------------
    async def run(self) -> bool:
        """Run the full DFU. Returns True if a manual reset is likely required
        (image is committed but the device did not auto-reboot)."""
        img = self.img
        if not img.device_type_ok():
            if img.device_type is None:
                raise DfuError(
                    "Could not read device_type from the init packet (.dat malformed or "
                    "too short). Refusing to flash: the brick-safety check requires "
                    f"device_type == 0x{C.ADAFRUIT_DEVICE_TYPE:04X}."
                )
            raise DfuError(
                f"Init packet device_type=0x{img.device_type:04X} but this bootloader "
                f"requires 0x{C.ADAFRUIT_DEVICE_TYPE:04X}. This firmware is for a different "
                f"device and would be rejected. Aborting before upload."
            )

        self.t.drain()

        # 1) START
        self._log(
            f"START_DFU mode={img.mode_label} "
            f"(sd={img.sd_size}, bl={img.bl_size}, app={img.app_size}, total={img.total_size} B)"
        )
        await self.t.write_ctrl(bytes([C.OP_START_DFU, img.mode]))
        await self.t.write_packet(img.size_block)
        await self._await_response(C.OP_START_DFU, timeout=C.START_DFU_TIMEOUT_S)

        # 2) INIT packet
        self._log(f"Sending init packet ({len(img.dat_data)} B)")
        await self.t.write_ctrl(bytes([C.OP_RECEIVE_INIT, C.INIT_RX]))
        cs = self.t.chunk_size
        for i in range(0, len(img.dat_data), cs):
            await self.t.write_packet(img.dat_data[i : i + cs])
        await self.t.write_ctrl(bytes([C.OP_RECEIVE_INIT, C.INIT_COMPLETE]))
        await self._await_response(C.OP_RECEIVE_INIT)

        # 3) Packet-receipt-notification interval (flow control). Scaled to the chunk size
        # so the bytes-in-flight stay near TARGET_PRN_BYTES regardless of negotiated MTU.
        cs = self.t.chunk_size
        self._eff_prn = self._effective_prn(cs)
        if self._eff_prn > 0:
            self._log(
                f"Setting packet-receipt interval = {self._eff_prn} "
                f"(chunk {cs} B, ~{self._eff_prn * cs} B in flight)"
            )
            await self.t.write_ctrl(bytes([C.OP_PKT_RCPT_REQ]) + struct.pack("<H", self._eff_prn))

        # 4) RECEIVE firmware
        self._log("Streaming firmware image...")
        await self.t.write_ctrl(bytes([C.OP_RECEIVE_FW]))
        await self._stream_firmware()
        # The "image received" handshake can be missed even when the device actually got the
        # whole image (packet-receipt notification lag desyncs us by a window). So DON'T treat
        # a missing ack as fatal — fall through to VALIDATE, whose CRC16 is the authoritative
        # test of a complete, correct image. If the device really is short, VALIDATE fails
        # (CRC_ERROR) or rejects (INVALID_STATE) and the controller retries — still safe.
        try:
            await self._await_response(
                C.OP_RECEIVE_FW, timeout=max(60.0, len(self.img.bin_data) / 50000)
            )
            self._log("Firmware image received by device.")
        except DfuError:
            self._log(
                "No 'image received' ack — the device may have it anyway (notification lag). "
                "Probing with VALIDATE; its CRC is the authoritative completion test."
            )

        # 5) VALIDATE (CRC16 over received image vs the .dat trailer) — the authoritative gate
        self._log("Validating image (CRC16)...")
        self.t.drain()  # clear any backlogged/late notifications so the ack reads clean
        await self.t.write_ctrl(bytes([C.OP_VALIDATE]))
        try:
            await self._await_response(C.OP_VALIDATE)
        except DfuInvalidState:
            # VALIDATE is "invalid" only because the device never reached "received-all" — it
            # is SHORT. We streamed the whole image, so a window was lost in transit (on the
            # nRF52840 almost always a USB re-enumeration during a flash erase). A reset+retry
            # just re-drops, so clear the wedged state and fail fast with actionable guidance.
            short_note = ""
            if self._prn_misses_total:
                short_note = (
                    f" (a {self._prn_misses_total}× multi-second packet-receipt stall was "
                    "seen during the transfer — the signature of that USB reset)"
                )
            self._log("Device reports the image is SHORT — data was lost in transit" + short_note + ".")
            try:
                await self.send_sys_reset()  # reboot to a clean OTA-DFU state for the next attempt
            except Exception:  # noqa: BLE001
                pass
            raise DfuDeviceShort(C.DEVICE_SHORT_USB_RESET_MSG)
        self._log("Validation OK.")

        # 6) ACTIVATE + RESET. Op 0x05 does NOT call NVIC_SystemReset() itself; the
        # bootloader requests a peer disconnect and the app-jump happens as the DFU loop
        # unwinds. A clean reboot drops the link in ~1-3 s, so 10 s is ample.
        self._log("Activating new image and resetting...")
        await self.t.write_ctrl(bytes([C.OP_ACTIVATE_RESET]))
        dropped = await self._wait_for_disconnect(timeout=10.0)
        if dropped:
            self._log("Device dropped the link — rebooting into the new firmware.")
            return False
        # No peer disconnect: image is committed (VALIDATE passed) but the device did not
        # auto-reboot — the stock 'AdaDFU' bootloader USB hang. Report success + manual kick.
        self._log("WARNING: " + C.STOCK_BOOTLOADER_HANG_MSG.replace("\n\n", " "))
        return True

    async def _stream_firmware(self) -> None:
        data = self.img.bin_data
        total = len(data)
        cs = self.t.chunk_size
        prn = self._eff_prn
        sent = 0
        pkts_since_prn = 0
        prn_misses = 0
        first_prn = True
        last_acked = 0
        start = time.monotonic()
        last_emit = 0.0
        last_log = start

        while sent < total:
            chunk = data[sent : sent + cs]
            await self.t.write_packet(chunk)
            # Pace EVERY packet: OTAFIX lazy-erases as data arrives into an 8-slot ring; a
            # tight burst overruns it during a page erase and the excess is silently dropped.
            await asyncio.sleep(C.STREAM_PACE_S)
            sent += len(chunk)
            pkts_since_prn += 1

            now = time.monotonic()
            if now - last_emit >= 0.1 or sent == total:
                elapsed = now - start
                bps = (sent * 8 / elapsed) if elapsed > 0 else 0.0
                self._progress(sent, total, bps)
                last_emit = now

            # Periodic timestamped progress to the log so a stall is visible, and so we can see
            # whether the device's acked count keeps pace with what we've sent (loss diagnosis).
            if now - last_log >= 5.0:
                el = now - start
                kib = (sent / 1024 / el) if el > 0 else 0.0
                self._log(
                    f"  …streaming {sent * 100 // total}% — {sent}/{total} B, "
                    f"{kib:.1f} KiB/s, device acked {last_acked} B"
                )
                last_log = now

            # Flow control: after N packets wait for the device's byte-count report,
            # but never wait after the final packet (the device sends the RECEIVE ack instead).
            if prn > 0 and pkts_since_prn >= prn and sent < total:
                timeout = C.FIRST_PRN_TIMEOUT_S if first_prn else C.PRN_TIMEOUT_S
                t0 = time.monotonic()
                try:
                    acked = await self._await_prn(timeout=timeout)
                    last_acked = acked
                    prn_misses = 0
                    if first_prn:
                        self._log(
                            f"  first packet-receipt after {time.monotonic() - t0:.1f}s "
                            f"(device acked {acked} B of {sent} sent)"
                        )
                    elif self._verbose:
                        self._log(f"  [v] receipt: device acked {acked} B (sent {sent} B)")
                except DfuError:
                    # Do NOT free-run: streaming past missing receipts is exactly what overruns
                    # the bootloader's 8-slot ring during a lazy page-erase and drops data.
                    prn_misses += 1
                    self._prn_misses_total += 1
                    self._log(
                        f"  no packet-receipt within {time.monotonic() - t0:.0f}s "
                        f"({'first window' if first_prn else 'mid-stream'}, "
                        f"{prn_misses}/{C.MAX_PRN_MISSES})"
                    )
                    if prn_misses >= C.MAX_PRN_MISSES:
                        raise DfuError(
                            f"No packet-receipt for {C.MAX_PRN_MISSES} consecutive windows — the "
                            "device is not draining (it may be overrunning during flash erase). "
                            "Aborting before more data is lost."
                        )
                first_prn = False
                pkts_since_prn = 0

        self.transfer_bytes = sent
        self.transfer_seconds = max(time.monotonic() - start, 1e-6)
        self._log(
            f"Stream finished: {sent} B in {self.transfer_seconds:.1f}s "
            f"({self.avg_bps / 8 / 1024:.1f} KiB/s); device last acked {last_acked} B"
        )

    async def _wait_for_disconnect(self, timeout: float) -> bool:
        """Wait for the peer-initiated link drop after ACTIVATE.

        The disconnect is the only proof ACTIVATE took effect (the op has no ack), so a
        timeout is reported as False rather than treated as confirmed success.
        Returns True if a real disconnect was observed, False on timeout.
        """
        try:
            while True:
                await self.t.next_notification(timeout)
        except BleDisconnected:
            return True
        except (asyncio.TimeoutError, TimeoutError):
            return False

    async def send_sys_reset(self) -> None:
        """Reboot the bootloader (SYS_RESET 0x06) to clear a wedged non-IDLE DFU state.

        Safe here: there is no valid app to corrupt — the bootloader re-enters OTA DFU in
        IDLE on reboot. The write may error as the device resets mid-operation; that's fine.
        """
        self.t.drain()
        try:
            await self.t.write_ctrl(bytes([C.OP_SYS_RESET]))
        except Exception as e:  # noqa: BLE001 - device reboots mid-write, benign
            self._log(f"(SYS_RESET write returned '{e}' — expected, device is resetting)")
        await self._wait_for_disconnect(timeout=5.0)
