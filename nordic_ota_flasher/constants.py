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

# Packet-receipt-notification interval. MeshCore FAQ: 10 for RAK, 8 for T114.
DEFAULT_PRN = 10

# Firmware chunk cap for the high-MTU path. Capped conservatively at 180 (not the full
# 247-3=244) because a high ATT MTU does NOT guarantee the link-layer Data Length was
# extended on Windows — 244-byte write-without-response then fragments into a ~10-PDU burst
# with no flow control and overruns the receiver. 180 is still ~9x the 20-byte path.
MAX_CHUNK = 180
MIN_CHUNK = 20  # one un-fragmented link-layer packet at the classic 23-byte ATT MTU

# write-without-response has no over-air back-pressure, and PRN counts PACKETS (not bytes),
# so the per-receipt byte budget must be held roughly constant regardless of MTU. Derive the
# effective PRN as round(TARGET_PRN_BYTES / chunk): ~3 at 180-byte, ~4 at 128-byte, 10 at
# 20-byte. ~480 bytes in flight matches the proven MTU-23 case (~200) closely enough.
TARGET_PRN_BYTES = 480
# Light per-packet pacing on the high-MTU path only (the stock 20-byte path stays full-speed).
HIGH_MTU_PACE_S = 0.002
# Tolerate transient missed packet-receipt notifications; only fail after this many in a row.
MAX_PRN_MISSES = 3
# Settle time after a SYS_RESET (0x06) recovery before rescanning for the rebooted bootloader.
SYS_RESET_SETTLE_S = 3.0
# The FIRST packet-receipt can be VERY late: an SD+BL (or large app) image makes the bootloader
# erase a big flash region on the opening write — tens of seconds during which it can't ack.
# Wait it out (don't push the next window into a full buffer mid-erase, which drops it).
FIRST_PRN_TIMEOUT_S = 120.0

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
    "• It ERASES the MeshCore app AND the node's identity. Afterward the node sits in OTA "
    "DFU mode with no app. You must then re-flash the MeshCore app (tick 'skip trigger') "
    "and re-provision it (restore prv.key via the MeshCore CLI) — a second stage this tool "
    "does NOT automate.\n\n"
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
    "Bootloader updated and validated. The MeshCore app was ERASED — the node is now in OTA "
    "DFU mode with no application.\n\n"
    "STAGE 2: re-scan, select the node's DFU advert, tick 'skip trigger', and flash the "
    "MeshCore app .zip. Then re-provision the node (restore its prv.key via the MeshCore "
    "CLI) to bring back its identity."
)

STOCK_BOOTLOADER_HANG_MSG = (
    "Image flashed and validated OK — but the node did not auto-reboot. This is the known "
    "stock 'AdaDFU' bootloader USB hang: it doesn't release USB before booting the app, so "
    "with the node plugged into USB the reboot stalls. Press the RAK4631 RESET button (or "
    "power-cycle) to boot the new firmware.\n\nTo avoid it next time: flash with the node on "
    "battery (USB unplugged), or install the oltaco OTAFIX bootloader over USB for a "
    "permanent fix (it also enables much faster transfers)."
)
