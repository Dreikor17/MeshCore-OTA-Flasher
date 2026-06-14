"""
Thin async wrapper over a bleak BleakClient for the legacy DFU GATT layout.

Responsibilities:
  * connect (forcing fresh GATT discovery — the DFU-mode MAC/layout change can be
    hidden by Windows' stale service cache)
  * subscribe to Control Point notifications and funnel them into a queue
  * expose write_ctrl (write-with-response) and write_packet (write-without-response)
  * compute a safe firmware chunk size from the negotiated ATT MTU
"""

from __future__ import annotations

import asyncio
import subprocess

from bleak import BleakClient

from . import constants as C


async def get_adapter_names() -> list[str]:
    """Best-effort friendly name(s) of the host Bluetooth radio bleak is using.

    Primary: the WinRT Radio API (clean name, needs an adapter present).
    Fallback: a PnP query that excludes paired peripherals.
    Returns [] if nothing can be determined.
    """
    try:
        from winrt.windows.devices.radios import Radio, RadioKind

        radios = await Radio.get_radios_async()
        names: list[str] = []
        for r in radios:
            try:
                if r.kind == RadioKind.BLUETOOTH and r.name and r.name not in names:
                    names.append(r.name)
            except Exception:
                pass
        if names:
            return names
    except Exception:
        pass
    try:
        return await asyncio.to_thread(_pnp_bluetooth_adapters)
    except Exception:
        return []


def _pnp_bluetooth_adapters() -> list[str]:
    """Bluetooth radio adapter name(s) via PnP, excluding paired peripherals (BTHENUM/BTHLE)."""
    ps = (
        "Get-PnpDevice -Class Bluetooth -PresentOnly -Status OK "
        "| Where-Object { $_.InstanceId -notlike 'BTHENUM\\*' -and $_.InstanceId -notlike 'BTHLE*' } "
        "| Select-Object -ExpandProperty FriendlyName"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
        capture_output=True,
        text=True,
        timeout=10,
        creationflags=0x08000000,  # CREATE_NO_WINDOW — no console flash in the windowed app
    )
    names: list[str] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if line and line not in names:
            names.append(line)
    return names


class BleDisconnected(Exception):
    """Raised when the peer drops the link while we were awaiting a notification."""

    def __init__(self, msg: str = "The device dropped the BLE link during the operation."):
        super().__init__(msg)


_DISCONNECT_SENTINEL = ("__disconnected__",)


class BleTransport:
    def __init__(self, target, log=None):
        # `target` may be a bleak BLEDevice (preferred on Windows — it carries the live
        # device object from the scan and avoids a fragile re-resolve of a just-appeared
        # random address) or a plain address string.
        self._target = target
        self.address: str = getattr(target, "address", target)
        self._log = log or (lambda *_: None)
        self.client: BleakClient | None = None
        self._queue: asyncio.Queue = asyncio.Queue()
        self._mtu = 23
        self._chunk_cap: int | None = None  # per-attempt ceiling for the fallback ladder

    async def connect(self, timeout: float = 20.0) -> None:
        # Pass the BLEDevice/address straight through. We do NOT force uncached services:
        # the DFU bootloader appears on a NEW address (no stale cache to defeat), and
        # uncached GATT reads are markedly less reliable on the WinRT backend.
        self.client = BleakClient(
            self._target,
            timeout=timeout,
            disconnected_callback=self._on_disconnect,
        )
        await self.client.connect()
        await self.client.start_notify(C.DFU_CONTROL_UUID, self._on_notify)
        # On WinRT the negotiated MTU is raised by an async event that can arrive AFTER
        # service discovery (bleak #1497 transient 20). Settle briefly, then read it.
        await asyncio.sleep(0.4)
        self._refresh_mtu()
        self._log(f"Connected — ATT MTU {self._mtu}, firmware chunk {self.chunk_size} B")

    # -- notifications ------------------------------------------------------
    def _on_disconnect(self, _client) -> None:
        try:
            self._queue.put_nowait(_DISCONNECT_SENTINEL)
        except Exception:
            pass

    def _on_notify(self, _sender, data: bytearray) -> None:
        self._queue.put_nowait(bytes(data))

    def drain(self) -> None:
        """Discard any buffered notifications before issuing a new command."""
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return

    async def next_notification(self, timeout: float) -> bytes:
        msg = await asyncio.wait_for(self._queue.get(), timeout=timeout)
        if msg is _DISCONNECT_SENTINEL:
            raise BleDisconnected()
        return msg

    # -- writes -------------------------------------------------------------
    def _refresh_mtu(self) -> None:
        # On WinRT the negotiated ATT MTU is raised by an async event that only fires
        # AFTER the first real GATT round-trip, so the value captured at connect() time
        # is usually still the default 23. Re-read it live (never shrink it).
        try:
            if self.client is not None and self.client.is_connected:
                self._mtu = max(self._mtu, int(self.client.mtu_size))
        except Exception:
            pass

    def set_chunk_cap(self, cap: int | None) -> None:
        """Per-attempt firmware-chunk ceiling for the fallback ladder; None = MAX_CHUNK.
        Pass MIN_CHUNK (20) to force the proven, un-fragmented stock path."""
        self._chunk_cap = cap

    @property
    def chunk_size(self) -> int:
        self._refresh_mtu()
        ceiling = min(C.MAX_CHUNK, self._chunk_cap) if self._chunk_cap else C.MAX_CHUNK
        return max(C.MIN_CHUNK, min(self._mtu - 3, ceiling))

    @property
    def mtu(self) -> int:
        self._refresh_mtu()
        return self._mtu

    async def write_ctrl(self, data: bytes) -> None:
        # Control Point only supports Write-Request; write-without-response is dropped.
        assert self.client is not None
        await self.client.write_gatt_char(C.DFU_CONTROL_UUID, bytes(data), response=True)

    async def write_packet(self, data: bytes) -> None:
        assert self.client is not None
        await self.client.write_gatt_char(C.DFU_PACKET_UUID, bytes(data), response=False)

    async def disconnect(self) -> None:
        if self.client is not None:
            try:
                await self.client.disconnect()
            except Exception:
                pass

    @property
    def is_connected(self) -> bool:
        return bool(self.client and self.client.is_connected)
