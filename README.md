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

## If a flash keeps failing or the device ends "short"

The client follows the Nordic reference's strict flow control: after each small batch of
packets it **waits for the device's receipt** before sending more, and waits as long as the
device needs — the SoftDevice can pause for several seconds to erase a flash page, and that's
normal (you'll see a brief "device busy — waiting…" note). It never streams ahead, so it won't
overrun the bootloader. If a flash still ends short:

- **Lower the PRN** (try 6, then 4). Smaller batches are safer on a weak link.
- **Save the verbose log** so the exact failure point can be pinpointed.

The firmware streams at the full packet size (MTU-3, ~244 bytes) — exactly what the Nordic
mobile app uses — and falls back to a smaller packet automatically if an adapter can't sustain
it. (Feeding this bootloader 20-byte packets is what made earlier versions stall.)

> Separately, the **stock** "AdaDFU" bootloader can fail to auto-reboot after an OTA done while
> on USB — flash on battery, or install OTAFIX, to avoid that. (That's a *post*-flash reboot
> quirk, not the transfer itself.)

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
- Full-MTU (244-byte) streaming matching the Nordic mobile app, with an automatic chunk-size
  fallback and stuck-state (SYS_RESET) recovery for robust transfers across varied BLE adapters.
