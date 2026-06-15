"""
Verified LEGACY Nordic DFU constants for the Adafruit nRF52 bootloader (and the
oltaco "OTAFIX" fork) as used by MeshCore on RAK4631 / nRF52840.

Every value here was confirmed against primary source:
  * Adafruit_nRF52_Bootloader/lib/sdk11/.../ble_dfu/ble_dfu.h  (service + opcodes)
  * adafruit-nrfutil dfu_transport_ble.py                       (client opcodes)
  * Nordic Android-DFU-Library LegacyDfuImpl.java               (canonical client)
  * MeshCore src/helpers/BLEDfu.cpp / NRF52Board.cpp            (buttonless trigger)

DO NOT target Secure DFU (service 0xFE59 / 8EC9xxxx) — that protocol is NOT present
on this hardware and will simply fail to find the service.
"""

# --- GATT UUIDs (legacy Nordic DFU; base 1212-EFDE-1523-785FEABCD123) ---
DFU_SERVICE_UUID  = "00001530-1212-efde-1523-785feabcd123"
DFU_CONTROL_UUID  = "00001531-1212-efde-1523-785feabcd123"  # write-with-response + notify (CCCD required)
DFU_PACKET_UUID   = "00001532-1212-efde-1523-785feabcd123"  # write + write-without-response
DFU_STATUS_UUID   = "00001533-1212-efde-1523-785feabcd123"
DFU_REVISION_UUID = "00001534-1212-efde-1523-785feabcd123"

# --- Control Point opcodes ---
OP_START_DFU      = 0x01  # +1 image-type byte (APP=0x04); also the bare buttonless trigger
OP_RECEIVE_INIT   = 0x02  # +1 sub-state byte (0x00 rx, 0x01 complete)
OP_RECEIVE_FW     = 0x03
OP_VALIDATE       = 0x04
OP_ACTIVATE_RESET = 0x05  # no response; link drops
OP_SYS_RESET      = 0x06
OP_IMAGE_SIZE_REQ = 0x07  # also the request-opcode echoed inside bytes-received PRN reports
OP_PKT_RCPT_REQ   = 0x08  # +2 byte uint16 LE packet count (0 disables PRN)
OP_RESPONSE       = 0x10  # first byte of every command-response notification
OP_PKT_RCPT_NOTIF = 0x11

# init-packet sub-states (second byte after OP_RECEIVE_INIT)
INIT_RX           = 0x00
INIT_COMPLETE     = 0x01

# image-type / update-mode bitfield (byte after OP_START_DFU); OR to combine
IMG_SOFTDEVICE    = 0x01
IMG_BOOTLOADER    = 0x02
IMG_APPLICATION   = 0x04

# response status values
RESP_SUCCESS       = 0x01
RESP_INVALID_STATE = 0x02
RESP_NOT_SUPPORTED = 0x03
RESP_DATA_SIZE     = 0x04
RESP_CRC_ERROR     = 0x05
RESP_OPER_FAILED   = 0x06
RESP_NAMES = {
    1: "SUCCESS", 2: "INVALID_STATE", 3: "NOT_SUPPORTED",
    4: "DATA_SIZE", 5: "CRC_ERROR", 6: "OPER_FAILED",
}

# The bootloader HARD-REQUIRES the .dat device_type to equal this (else NRF_ERROR_FORBIDDEN).
ADAFRUIT_DEVICE_TYPE = 0x0052  # 82

# Packet-receipt-notification interval (packets between flow-control receipts). The bootloader's
# DFU receive-buffer pool holds only ~8 packets, and the per-window receipt gate keeps that many
# in flight, so too high a PRN overruns the pool and the device goes SILENT (no receipt) — PRN 10
# does exactly that on the RAK/OTAFIX bootloader (fails at both 244- and 128-byte chunks: it's
# the packet COUNT, not size). 4 is a safe, fast-enough default; _effective_prn hard-caps at 6.
DEFAULT_PRN = 4
PRN_MAX_SAFE = 6  # hard cap: keep a window under the ~8-packet RX pool so it can't go silent

# Firmware chunk = MTU-3 (244 at the negotiated 247). The Nordic Android DFU client streams
# firmware at MTU-3 too (it grows its send buffer to mtu-3 once MTU is negotiated), which FILLS
# the bootloader's MTU-sized RX buffer blocks. Feeding 20-byte writes into those ~244-byte
# blocks wastes most of each block and exhausts the small (~8-block) pool after a few packets —
# then the bootloader silently ignores all further Packet writes (no receipt, no error). That
# was the real "device won't ack the first window" bug, and WinRT won't let us lower the MTU,
# so we MATCH it instead. bleak/WinRT awaits each write-without-response (write_value_with_
# result), so 244-byte writes are serialized one-at-a-time, not a fire-and-forget burst.
MAX_CHUNK = 244
MIN_CHUNK = 20  # one un-fragmented packet at ATT MTU 23 — the slow fallback geometry

# Cap the firmware SEND rate (bytes/sec). The bootloader acks RECEIVED bytes, not flushed-to-
# flash bytes, so on a FAST BLE link we queue data faster than the flash can erase/write it: the
# device's flash pipeline backs up and it WEDGES the moment it hits an erase (the per-window
# receipt gate does NOT prevent this — receipts keep arriving because the data was "received").
# A slow link / the phone app stay under the flash's effective throughput and succeed (a known-
# good run streamed steadily at ~2.1 KiB/s with zero stalls). Cap at ~2 KiB/s so a fast adapter
# is throttled to behave like that proven run; a slow link is already under the cap (no delay).
# This also spaces packets enough to avoid the WinRT first-window write burst. Tunable.
MAX_STREAM_BPS = 2048
# A missed receipt is FATAL on the first miss: we must never stream the next window without the
# current window's receipt (that is what overruns the device during a deferred flash erase).
MAX_PRN_MISSES = 1
# Settle time after a SYS_RESET (0x06) recovery before rescanning for the rebooted bootloader.
SYS_RESET_SETTLE_S = 3.0
# Receipt-wait backstops. We must NEVER send the next window before its receipt arrives (that
# overruns the device), so on a timeout we abort + reset rather than stream on.
#   FIRST window: a healthy bootloader acks the first window in ~0 s, so a long silence here
#   means it is WEDGED (commonly left non-IDLE by a prior aborted transfer) — detect that fast
#   and reset, don't wait a full minute.
#   MID-STREAM: the SoftDevice can defer a flash erase several seconds behind radio events and
#   legitimately withhold the receipt, so be patient (mirror the Nordic reference's long wait).
# A real disconnect aborts instantly in both cases (BleDisconnected wakes next_notification).
FIRST_PRN_TIMEOUT_S = 15.0
PRN_TIMEOUT_S = 60.0
# START_DFU ack can take a while (the bootloader may erase flash before acking); give it room.
START_DFU_TIMEOUT_S = 60.0

# Advertised-name hints used to classify scan results.
OTA_NAME_HINT  = "_OTA"          # app-mode after `start ota` (e.g. RAK4631_OTA)
DFU_NAME_HINTS = ("DFU", "ADADFU")  # bootloader-mode advert (e.g. RAK4631_DFU, AdaDFU)
STOCK_DFU_NAME = "ADADFU"        # the stock Adafruit bootloader's advert name

# Shown when the image is flashed+validated but the device did not auto-reboot. This is
# the known stock Adafruit ("AdaDFU") bootloader hang (Adafruit #174): its BLE-OTA path
# omits the usb_teardown() that the serial/UF2 path does, so with USB attached the jump
# to the app stalls. The image is fine; it just needs a manual kick. OTAFIX fixes it.
# Shown before flashing a SoftDevice+Bootloader package over BLE (the highest-risk op).
BOOTLOADER_FLASH_WARNING = (
    "Flashing a SoftDevice + BOOTLOADER over BLE — this is NOT a normal app update.\n\n"
    "• It updates the SoftDevice + bootloader and leaves the node in OTA DFU mode, so you then "
    "re-flash the MeshCore app (tick 'skip trigger') — a second stage this tool does NOT "
    "automate. Your node's identity (its name and private key) is PRESERVED — no "
    "re-provisioning needed.\n\n"
    "• As a precaution, back up the node's name and private key first — read the key with "
    "'get prv.key' over the USB serial console. You shouldn't need to restore it, but any "
    "bootloader flash carries a brick risk.\n\n"
    "• Confirm this matches THIS board. File: '{file}'. The device CANNOT detect a "
    "wrong-board image — another nRF52840 board's bootloader passes validation and bricks "
    "the node.\n\n"
    "• Run on STABLE power (battery, USB unplugged) and don't power-cycle. A power loss "
    "during the final activate can leave no bootloader — recoverable only with SWD/J-Link.\n\n"
    "• Recovery needs PHYSICAL USB access (double-tap reset → web flasher). Do NOT do this "
    "on a remote node you can't physically reach.\n\n"
    "Officially the bootloader is installed over USB (.uf2), not BLE. Proceed anyway?"
)

# Shown after a successful bootloader flash — the app is gone; guide the user to stage 2.
BOOTLOADER_FLASHED_MSG = (
    "Bootloader updated and validated. The node is now in OTA DFU mode.\n\n"
    "STAGE 2: re-scan, select the node's DFU advert, tick 'skip trigger', and flash the "
    "MeshCore app .zip. Your node keeps its identity (name + private key) — no re-provisioning "
    "needed."
)

DEVICE_SHORT_MSG = (
    "The whole image streamed, but the device reports it is SHORT — a window was lost in "
    "transit. Legacy DFU can't retransmit a lost window, so the flash can't complete (the old "
    "firmware is untouched).\n\n"
    "The client now waits for every packet-receipt before sending more, so this should be "
    "rare. Try flashing again. If it keeps happening:\n"
    "  • Lower the PRN setting (try 6, then 4) — smaller windows are safer on a slow link.\n"
    "  • Save the verbose log so the exact failure point can be pinpointed."
)

STOCK_BOOTLOADER_HANG_MSG = (
    "Image flashed and validated OK — but the node did not auto-reboot. This is the known "
    "stock 'AdaDFU' bootloader USB hang: it doesn't release USB before booting the app, so "
    "with the node plugged into USB the reboot stalls. Press the RAK4631 RESET button (or "
    "power-cycle) to boot the new firmware.\n\nTo avoid it next time: flash with the node on "
    "battery (USB unplugged), or install the oltaco OTAFIX bootloader over USB for a "
    "permanent fix (it also enables much faster transfers)."
)
