# MeshCore OTA Flasher

A Windows desktop app that flashes **MeshCore** firmware to Nordic **nRF52840** boards
(e.g. **RAK4631 / WisMesh**) over **Bluetooth LE** — no phone required. It speaks the legacy
Nordic DFU protocol used by the Adafruit nRF52 bootloader and the
[oltaco OTAFIX](https://github.com/oltaco/Adafruit_nRF52_Bootloader_OTAFIX) fork.

> An **RFLab.io** tool. Windows 11 · Python + PySide6 + bleak.
> Meshtastic support is planned; this release targets MeshCore.

![RFLab.io OTA Flasher — a completed RAK4631 flash over BLE](OTA%20Flasher.png)

---

## Download

Grab **`MeshCore-OTA-Flasher-v0.3.2.exe`** from the
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
2. **Flash.** It's a two-stage update: the node ends up in DFU mode, so you re-flash the app
   next. Your node's **identity (name + private key) is preserved** — no re-provisioning.
3. The node is then in DFU mode. **Flash the MeshCore app** (the app auto-rescans for you) and
   it boots back up with its original name and key.

> **Back up first (precaution).** A bootloader flash is the riskiest operation, so save your
> node's **name** and **private key** beforehand — read the key with `get prv.key` over the USB
> serial console (`set prv.key <hex>` restores it if you ever need to). You shouldn't need them
> for a normal OTA-fix update, but it's cheap insurance.

**Cautions** — a bootloader flash is the one operation that can brick a node:
- Flash only the package for **your exact board** (the device can't reject a wrong-board image).
- Keep the node **powered** for the whole transfer.
- Keep a USB recovery path ready (double-tap reset → [flasher.meshcore.io](https://flasher.meshcore.io)).
- **Don't** OTA-flash the bootloader on a remote node you can't physically reach.

## How the transfer works (and tuning it)

By default the flasher streams the firmware **one packet at a time using write-with-response** —
each packet is acknowledged by the device before the next is sent. That gives true per-packet
back-pressure, so the bootloader can never be outrun, and the tool simply waits whenever the
device pauses to erase flash (you'll see a "device busy — waiting…" note). This mirrors what the
Nordic nRF mobile app does, and it's what makes flashing the **stock AdaDFU bootloader** reliable.

The **PRN** control picks the flow-control mode:
- **0 (default, recommended)** — write-with-response, as above. Reliable everywhere.
- **1–6** — the nRF app's config: stream write-*without*-response and check the device's receipt
  every N packets. Can be faster on a BLE adapter that pipelines, but Windows gives no per-packet
  back-pressure there, so it may stall — use only to experiment.

If a flash fails, **save the verbose log** (it reports per-packet timing) so the exact point can
be pinpointed. A corrupted transfer fails the final CRC check and is **not** activated — the old
firmware stays put.

**Speed:** at the stock / early-OTAFIX bootloader's ATT MTU of 23 a flash runs ~0.9 KiB/s — a
Windows BLE limit (one acknowledged packet per connection interval), not a bug. Installing a
**high-MTU OTAFIX (2.1+)** bootloader raises the MTU to 247 and flashes roughly 10× faster.

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

### v0.3.2
Major reliability release — OTA now mirrors the Nordic nRF Android app, and **flashing the stock
RAK/AdaDFU bootloader to OTAFIX over BLE works** (it stalled mid-transfer before).
- **Matched the phone's flow control.** The nRF app streams with packet-receipt-notifications
  **off**, paced one packet at a time by the BLE stack. The flasher now does the same: by default
  it streams **write-with-response** (each packet acknowledged before the next), the per-packet
  back-pressure Windows/WinRT otherwise can't provide. This fixed the stock-bootloader
  SoftDevice+Bootloader flash that previously went silent partway through.
- **Brick-safety:** the tool never auto-resets the node during a bootloader (SD+BL) flash — a
  reset mid-SoftDevice-erase could corrupt it. A failed flash fails cleanly instead.
- **PRN is now a flow-control selector:** `0` (default) = write-with-response, reliable
  everywhere; `1–6` = the nRF app's config (write-without-response + a receipt every N packets),
  which can be faster on adapters that pipeline.
- Receipt/ack waits are now long and **disconnect-bounded**, like the phone's untimed waits — a
  slow flash erase is ridden out; a real link drop aborts instantly.
- The found-devices list **clears after a flash completes**, and verbose logging now reports
  per-packet timing (ms/packet).
- Transient GitHub API errors (gateway timeouts) are retried so the OTA-fix board list survives a
  hiccup.
- *Note:* at the stock bootloader's MTU 23 a flash is slow (~0.9 KiB/s) — a Windows BLE limit; a
  high-MTU **OTAFIX 2.1+** bootloader is roughly 10× faster.

### v0.2.4
Patch — fixes flashes freezing mid-transfer on faster BLE adapters.
- **Send-rate cap.** On a fast BLE link the tool could hand the bootloader firmware faster than
  its flash could erase/write it; the device's flash pipeline backed up and it froze mid-flash
  during an erase. (The bootloader acks *received* bytes, not *flushed-to-flash* bytes, so the
  per-window flow-control gate didn't catch it.) The send rate is now throttled so a quick
  adapter behaves like a slow one — which always worked. Reliable, at the cost of a slower
  transfer; an already-slow adapter is unaffected.

### v0.2.2
Patch — fixes a flash stall introduced in v0.2.0.
- **Fixed the PRN default.** The packet-receipt interval is now capped to fit the bootloader's
  ~8-packet receive buffer (default 4, max 6). v0.2.0 defaulted to 10, which could overrun the
  buffer and leave the device silent on the first packet (no receipt). It's the packet *count*,
  not size, so it affected every chunk size.
- ACTIVATE now tolerates the device dropping the link as it reboots — no more spurious
  `ERROR_CANCELLED` on an otherwise-successful flash.
- The "use downloaded" dropdown defaults to a blank selection.

### v0.2.0
Reliability release — OTA now completes end-to-end on RAK4631 / nRF52840 (firmware **and** the
OTAFIX bootloader), verified on hardware.
- **Fixed the core stall:** firmware now streams at the full negotiated packet size (MTU-3,
  ~244 bytes), exactly like the Nordic mobile app. The previous 20-byte path silently exhausted
  this bootloader's receive-buffer pool, so it never acknowledged the first packet and the
  transfer hung.
- **Strict per-window flow control:** waits for each packet-receipt before sending more (never
  streams ahead), with an automatic 244 → 128-byte fallback for difficult adapters.
- Fast detection + auto-reset/retry when a bootloader is left wedged by a prior aborted transfer.
- Verbose, timestamped logging and a one-click **Save log…** for diagnostics.
- Packet-receipt interval (PRN) now defaults to 10 and is adjustable.

### v0.1.0
First public release.
- BLE OTA flashing of MeshCore firmware to nRF52840 / RAK4631 (legacy Nordic DFU).
- Buttonless `start ota` trigger → bootloader reconnect → stream → validate → activate.
- OTA-fix (OTAFIX) bootloader flashing over BLE (combined SoftDevice+Bootloader), with a full
  brick-risk warning and two-stage (re-flash app; identity preserved) guidance.
- Fetch latest firmware (repeater / companion / room-server) and the OTAFIX bootloader from
  GitHub; cached for offline reuse.
- Live signal meter, progress + bitrate + ETA, and a post-flash summary (adapter, time, rate).
- "Reliable" mode, a chunk-size fallback ladder, and stuck-state (SYS_RESET) recovery.
