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

# Packet-Receipt-Notification interval. DEFAULT IS 0 = DISABLED: we never send Op 0x08 and stream
# the image paced by per-packet write-with-response back-pressure instead. Forcing PRN on (our old
# default 4) and blocking on the bootloader's byte-count receipts is exactly what stalled the stock
# 'AdaDFU' SD+BL flash — the bootloader stops emitting receipts during the SoftDevice self-overwrite
# and our gate hung waiting for them. PRN > 0 is a selectable alternative (gate on the 0x11 receipts).
DEFAULT_PRN = 0
PRN_MAX_SAFE = 99  # PRN spinner ceiling (fully user-selectable for experimentation). PRN > 0 selects
# the no-response + receipt-gated mode; measured ~3.2 KiB/s at PRN 5 (MTU 23) vs ~0.9 at PRN 0.
# ~10-12 is the practical max — some bootloaders reject a too-high value (a clean OPERATION_FAILED,
# no harm) and a high PRN can overrun a small-RX-pool bootloader. Not the default.

# Firmware-packet flow control = WRITE-WITH-RESPONSE on the DFU Packet characteristic (0x1532).
# WinRT gives NO per-write back-pressure for write-without-response — the await only confirms the
# LOCAL Windows stack queued the PDU (confirmed by the bleak maintainer; field reports show writes
# burst then drop the link). The legacy bootloader's Packet char DECLARES char_props.write = 1
# (Nordic SDK11 ble_dfu.c), so a Write Request is GATT-legal here and returns a per-packet ATT
# acknowledgement = true one-in-flight back-pressure that cannot outrun the device. NOTE: legal on
# LEGACY DFU only (a Secure-DFU packet char is write-without-response-only and would reject this);
# we only target legacy DFU here. Set False to stream write-without-response (needs PRN as backpressure).
PACKET_WRITE_WITH_RESPONSE = True

# Firmware chunk = MTU-3 (244 at the negotiated 247; 20 at the stock bootloader's MTU 23). WinRT
# auto-negotiates the MTU and won't let us force it, but the stock 'AdaDFU' bootloader only grants
# MTU 23 anyway, so the chunk is 20 bytes there regardless.
MAX_CHUNK = 244
MIN_CHUNK = 20  # one un-fragmented packet at ATT MTU 23 (stock bootloader geometry)

# Fallback-only (PRN > 0): a missed receipt is fatal on the first miss — never stream the next
# window without the current window's receipt. Unused in the default PRN=0 (write-with-response) path.
MAX_PRN_MISSES = 1
# Settle time after a SYS_RESET (0x06) recovery before rescanning for the rebooted bootloader.
SYS_RESET_SETTLE_S = 3.0
# Response/ack-wait backstop — LONG and DISCONNECT-bounded (untimed in spirit, bounded only by link
# loss). A real disconnect still aborts instantly (BleDisconnected
# wakes next_notification), so a long ceiling costs nothing on a dead link; it only lets a
# genuinely-slow-but-alive bootloader finish. Used for the START_DFU / INIT / RECEIVE / VALIDATE acks.
ACK_BACKSTOP_S = 600.0
START_DFU_TIMEOUT_S = ACK_BACKSTOP_S
# Fallback-only PRN receipt-wait backstops (same long, disconnect-bounded value).
FIRST_PRN_TIMEOUT_S = ACK_BACKSTOP_S
PRN_TIMEOUT_S = ACK_BACKSTOP_S

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
