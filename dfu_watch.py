"""dfu-watch — passive BLE oracle for the over-the-air DFU test.

Watches a target node's BLE advert and reports when it flips into:
  * OTA-ARMED      (*_OTA)            -> `start ota` took effect; one trigger from DFU
  * DFU-BOOTLOADER (*_DFU/AdaDFU/0x1530) -> in the bootloader; accepts unsigned firmware

Read-only: it ONLY scans — never connects, pairs, or writes. The over-the-air attack is
driven by MeshForge over LoRa; this just observes whether the node changes state. So this
oracle is valid even for the BLE-password threat model (it proves DFU entry without itself
being the BLE attacker).

Usage:
    python dfu_watch.py                  # one scan: list every BLE device (identify the target)
    python dfu_watch.py --watch RAK      # poll forever, alert on transitions of a name containing RAK
    python dfu_watch.py --watch RAK --csv run.csv   # also append timestamped states to a CSV
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime

from nordic_ota_flasher import scanner


def _stamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _state(d) -> str:
    if d.is_dfu_bootloader:
        return "DFU-BOOTLOADER"
    if d.is_ota_armed:
        return "OTA-ARMED"
    return "app"


async def list_once(timeout: float) -> None:
    devices = await scanner.scan(timeout=timeout)
    print(f"[{_stamp()}] {len(devices)} BLE device(s) in range:")
    for d in devices:
        tag = f"   <-- {d.tag}" if d.tag else ""
        svc = " +DFU-svc" if d.has_dfu_service else ""
        print(f"  {d.rssi:>4} dBm  {d.address}  {d.name!r}{svc}{tag}")
    print("\nPick the companion's name and re-run:  python dfu_watch.py --watch <name-substring>")


async def watch(match: str, timeout: float, interval: float, csv_path: str | None) -> None:
    match_u = match.upper()
    last = None
    csv = open(csv_path, "a", encoding="utf-8") if csv_path else None
    print(f"[{_stamp()}] watching BLE for a name containing {match!r}. Ctrl-C to stop.")
    print("  (passive: scan-only, no connect/write — MeshForge drives the attack over LoRa)")
    try:
        while True:
            try:
                devices = await scanner.scan(timeout=timeout)
            except Exception as e:  # noqa: BLE001
                print(f"[{_stamp()}] scan error: {e}")
                await asyncio.sleep(interval)
                continue
            target = next((d for d in devices if match_u in (d.name or "").upper()), None)
            if target is None:
                if last != "GONE":
                    print(f"[{_stamp()}] target not seen (off-air / phone-connected / mid-reset)")
                    last = "GONE"
            else:
                st = _state(target)
                if csv:
                    csv.write(f"{datetime.now().isoformat()},{target.name},{target.rssi},{st}\n")
                    csv.flush()
                if st != last:
                    print(f"[{_stamp()}] {target.name!r}  {target.rssi} dBm  -> {st}"
                          + ("" if st == "app" else "   *** STATE CHANGE ***"))
                    if st == "OTA-ARMED":
                        print("    !! ARMED for DFU (`start ota` took effect) — one trigger from bootloader")
                    elif st == "DFU-BOOTLOADER":
                        print("    !! IN DFU BOOTLOADER — accepts unsigned firmware. THIS IS THE FINDING.")
                    last = st
            await asyncio.sleep(interval)
    finally:
        if csv:
            csv.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="passive BLE DFU/OTA oracle (scan-only)")
    ap.add_argument("--watch", metavar="NAME", help="poll forever, alert on NAME's state transitions")
    ap.add_argument("--timeout", type=float, default=6.0, help="per-scan duration in seconds")
    ap.add_argument("--interval", type=float, default=1.0, help="gap between scans in --watch")
    ap.add_argument("--csv", metavar="PATH", help="append timestamped states to a CSV file")
    args = ap.parse_args()
    try:
        if args.watch:
            asyncio.run(watch(args.watch, args.timeout, args.interval, args.csv))
        else:
            asyncio.run(list_once(args.timeout))
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
