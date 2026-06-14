# MeshCore OTA Flasher

A Windows desktop app that flashes **MeshCore** firmware to Nordic **nRF52840** boards
(e.g. **RAK4631 / WisMesh**) over **Bluetooth LE** — no phone required. It speaks the legacy
Nordic DFU protocol used by the Adafruit nRF52 bootloader and the
[oltaco OTAFIX](https://github.com/oltaco/Adafruit_nRF52_Bootloader_OTAFIX) fork.

> An **RFLab.io** tool. Windows 11 · Python + PySide6 + bleak.
> Meshtastic support is planned; this release targets MeshCore.

---

## Download

Grab **`MeshCore-OTA-Flasher-v0.1.0.exe`** from the
[**latest release**](https://github.com/Dreikor17/MeshCore-OTA-Flasher/releases/latest) — a
single self-contained file, no install needed. Just run it.

*(Windows SmartScreen may warn on an unsigned app — click **More info → Run anyway**.)*

## What it does

- Scans for BLE devices and flashes a MeshCore firmware **`.zip`** over the air.
- Live **progress bar, transfer rate, ETA**, and a per-device **signal meter** for antenna tuning.
- **Fetches the latest firmware** (repeater / companion / room-server) straight from GitHub.
- Flashes the **OTA-fix bootloader** (OTAFIX) over BLE, with the right safety rails.
- Remembers downloaded packages for **offline** reuse.

## Updating MeshCore firmware (the usual case)

1. **Arm OTA on the node.** In the MeshCore app, log in to the repeater with admin rights,
   open the **Command Line** tab, type `start ota`, and confirm `OK`. It now advertises as
   `…_OTA` (e.g. `RAK4631_OTA`).
2. **Scan** and select the node. *(Use the live signal meter to position the antenna first.)*
3. **Select firmware** — *Browse* a local `.zip`, *Fetch latest from GitHub*, or pick one
   from *use downloaded*. Use the **ZIP**, not the `.uf2`.
4. Click **Flash**. The app triggers the bootloader, reconnects, streams the image, and
   activates it.

> Already in bootloader mode? **"Device already in DFU/bootloader mode"** is auto-ticked when
> you select a `…_DFU` device, which skips the trigger.

## Updating the bootloader to OTAFIX (recommended)

The stock RAK/Adafruit bootloader has two OTA issues OTAFIX fixes: it can fail to auto-reboot
after an OTA performed while on USB, and OTA is slower. You can flash OTAFIX **over BLE**:

1. *Select firmware* → **Fetch OTA-fix bootloader** → pick your board (e.g.
   `wiscore_rak4631_board`). It downloads the combined SoftDevice+Bootloader package.
2. **Flash.** ⚠️ This **erases the app and the node's identity** — it's a two-stage update.
3. The node is then in DFU mode with no app. **Flash the MeshCore app** (the app auto-rescans
   for you), then **restore the node's identity** over USB serial: run `get prv.key`
   *before* the swap to save it, and `set prv.key <hex>` *after* to restore it.

**Cautions** — a bootloader flash is the one operation that can brick a node:
- Flash only the package for **your exact board** (the device can't reject a wrong-board image).
- Keep the node **powered** for the whole transfer.
- Keep a USB recovery path ready (double-tap reset → [flasher.meshcore.io](https://flasher.meshcore.io)).
- **Don't** OTA-flash the bootloader on a remote node you can't physically reach.

## If a flash keeps failing or resets the node

**Flash on battery — unplug USB.** This is the single most reliable fix. On the nRF52840,
erasing a flash page halts the CPU for ~85 ms; while it's halted the USB stack isn't serviced,
so Windows **re-enumerates** the device — and that same stall drops the data in flight, leaving
the transfer a window short (you'll hear the USB device disconnect/reconnect mid-flash). With
**no USB cable** (battery, a charge-only cable, or a dumb wall charger) there's no host to reset
and the transfer completes cleanly. When this happens the app now tells you so explicitly.
*(Confirmed upstream: Adafruit bootloader issue #174 — it doesn't happen on battery power.)*

Some BLE adapters also can't sustain the fast high-MTU transfer and will drop the link. Tick
**"Reliable (20-byte)"** — it streams at the slow-but-solid 20-byte chunk size, paced to suit
the bootloader's flash erases. It's remembered per machine, and bootloader flashes always use it
automatically. *Tip: a different/better BLE adapter may unlock the faster high-MTU path.*

## Run from source

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python run.py
```
Requires Python 3.10+ on Windows.

## Build the .exe

```powershell
.\build_exe.ps1     # -> dist\RFLab.io OTA Flasher.exe  (single file, ~49 MB)
```
Uses PyInstaller against `NordicOTAFlasher.spec` (bundles the WinRT BLE backend and trims
unused Qt modules).

## Safety

- The init packet's `device_type` is verified **before** any upload; a mismatch aborts.
- `.uf2` files (USB-only) are detected and rejected for BLE flashing.
- A failed CRC check at the end means the new image is **not** activated — the old firmware stays.
- For clean, unattended OTAs, run the OTAFIX bootloader and keep a USB recovery path handy.

## Releases / Changelog

### v0.1.0
First public release.
- BLE OTA flashing of MeshCore firmware to nRF52840 / RAK4631 (legacy Nordic DFU).
- Buttonless `start ota` trigger → bootloader reconnect → stream → validate → activate.
- OTA-fix (OTAFIX) bootloader flashing over BLE (combined SoftDevice+Bootloader), with a full
  brick-risk warning and two-stage (re-flash app + re-provision) guidance.
- Fetch latest firmware (repeater / companion / room-server) and the OTAFIX bootloader from
  GitHub; cached for offline reuse.
- Live signal meter, progress + bitrate + ETA, and a post-flash summary (adapter, time, rate).
- "Reliable (20-byte)" mode, an automatic chunk-size fallback ladder, and stuck-state
  (SYS_RESET) recovery for robust transfers across varied BLE adapters.
