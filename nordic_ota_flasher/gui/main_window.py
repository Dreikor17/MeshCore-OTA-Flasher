"""Main window: scan -> pick firmware -> flash, with live progress + bitrate."""

from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime

from PySide6.QtCore import Qt, QTimer, QSettings
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)
from bleak import BleakScanner
from qasync import asyncSlot

from .. import constants as C
from .. import github_releases as gh
from ..dfu_package import DfuImage, DfuPackageError, load_dfu_zip
from ..flash_controller import FlashController
from ..scanner import FoundDevice, scan

# Keep downloads next to the app (exe or run.py), not the volatile cwd, so the cache
# persists and is reusable offline regardless of where the app is launched from.
DOWNLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "downloads")


def _fmt_bytes(n: float) -> str:
    if n < 1024:
        return f"{n:.0f} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KiB"
    return f"{n / 1024 / 1024:.2f} MiB"


class MainWindow(QWidget):
    def __init__(self):
        super().__init__()
        from .. import __version__
        self.setWindowTitle(f"RFLab.io OTA Flasher (Nordic nRF52840) — v{__version__}")
        self.resize(720, 760)

        self._devices: list[FoundDevice] = []
        self._selected: FoundDevice | None = None
        self._image: DfuImage | None = None
        self._firmware_path: str | None = None
        self._otafix_assets: list = []  # (board_label, Asset)
        self._busy = False
        self._live_rssi: dict[str, int] = {}
        self._rssi_scanner = None
        self._rssi_lock = asyncio.Lock()

        self.controller = FlashController()
        self.controller.log.connect(self._on_log)
        self.controller.phase.connect(self._on_phase)
        self.controller.progress.connect(self._on_progress)
        self.controller.finished.connect(self._on_finished)

        self._build_ui()
        # Populate the OTA-fix board list in the background once the loop is running.
        QTimer.singleShot(200, self.on_load_otafix_boards)
        # Refresh the live signal meter periodically (the scanner updates the data).
        self._rssi_timer = QTimer(self)
        self._rssi_timer.setInterval(700)
        self._rssi_timer.timeout.connect(self._refresh_rssi_label)
        self._rssi_timer.start()
        self._refresh_downloaded()
        # Persist the checkbox. New key (the old "reliable_transfer=20-byte" meaning is gone),
        # so it defaults UNticked → the full ladder (244 → 128 → 20) is tried on every flash.
        self._settings = QSettings("RFLab.io", "Nordic OTA Flasher")
        self.reliable_check.setChecked(self._settings.value("skip_20byte_fallback", False, type=bool))
        self.reliable_check.toggled.connect(
            lambda v: self._settings.setValue("skip_20byte_fallback", v)
        )

    # ------------------------------------------------------------------ UI
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        # --- 1. Device ---
        dev_box = QGroupBox("1.  Select BLE device")
        dv = QVBoxLayout(dev_box)
        row = QHBoxLayout()
        self.scan_btn = QPushButton("Scan for devices")
        self.scan_btn.clicked.connect(self.on_scan)
        self.scan_status = QLabel("Not scanned yet.")
        row.addWidget(self.scan_btn)
        row.addWidget(self.scan_status, 1)
        dv.addLayout(row)
        dev_row = QHBoxLayout()
        self.device_list = QListWidget()
        self.device_list.setMinimumHeight(150)
        self.device_list.currentRowChanged.connect(self._on_device_selected)
        dev_row.addWidget(self.device_list, 1)

        # Live signal meter for the selected device. This reads ADVERTISEMENT RSSI via a
        # background scanner and runs ONLY while idle — it is stopped during any flash so it
        # can never steal radio time from the transfer.
        rssi_box = QGroupBox("Signal (live)")
        rssi_box.setFixedWidth(150)
        rv = QVBoxLayout(rssi_box)
        self.rssi_value = QLabel("—")
        _big = QFont()
        _big.setPointSize(18)
        _big.setBold(True)
        self.rssi_value.setFont(_big)
        self.rssi_value.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.rssi_bars = QLabel("")
        self.rssi_bars.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.rssi_quality = QLabel("select a device")
        self.rssi_quality.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.rssi_quality.setWordWrap(True)
        rv.addWidget(self.rssi_value)
        rv.addWidget(self.rssi_bars)
        rv.addWidget(self.rssi_quality)
        rv.addStretch(1)
        dev_row.addWidget(rssi_box)
        dv.addLayout(dev_row)

        self.rssi_hint = QLabel(
            "Tip: select a device, then reposition the antenna/node to maximize the signal "
            "before flashing (live meter pauses during a flash)."
        )
        self.rssi_hint.setWordWrap(True)
        self.rssi_hint.setStyleSheet("color: gray;")
        dv.addWidget(self.rssi_hint)
        root.addWidget(dev_box)

        # --- 2. Firmware ---
        fw_box = QGroupBox("2.  Select firmware (.zip)")
        fv = QVBoxLayout(fw_box)
        local_row = QHBoxLayout()
        self.browse_btn = QPushButton("Browse local .zip...")
        self.browse_btn.clicked.connect(self.on_browse)
        local_row.addWidget(self.browse_btn)
        local_row.addWidget(QLabel("or fetch latest:"))
        self.role_combo = QComboBox()
        self.role_combo.addItems(["repeater", "companion", "room-server"])
        local_row.addWidget(self.role_combo)
        self.fetch_btn = QPushButton("Fetch latest from GitHub")
        self.fetch_btn.clicked.connect(self.on_fetch_latest)
        local_row.addWidget(self.fetch_btn)
        local_row.addStretch(1)
        fv.addLayout(local_row)

        # OTA-fix bootloader, flashed over BLE (combined SoftDevice+Bootloader DFU zip).
        otafix_row = QHBoxLayout()
        otafix_row.addWidget(QLabel("or fetch OTA-fix bootloader:"))
        self.otafix_combo = QComboBox()
        self.otafix_combo.addItem("(loading boards…)")
        self.otafix_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.otafix_combo.setMaximumWidth(240)
        otafix_row.addWidget(self.otafix_combo)
        self.fetch_otafix_btn = QPushButton("Fetch OTA-fix (BLE)")
        self.fetch_otafix_btn.clicked.connect(self.on_fetch_otafix)
        otafix_row.addWidget(self.fetch_otafix_btn)
        otafix_row.addStretch(1)
        fv.addLayout(otafix_row)
        otafix_note = QLabel(
            "⚠ Updating the bootloader ERASES the app — you must re-flash the firmware "
            "(and re-provision the node) afterward."
        )
        otafix_note.setWordWrap(True)
        otafix_note.setStyleSheet("color: #b8860b;")
        fv.addWidget(otafix_note)

        # Previously-downloaded packages — reuse without re-downloading, works offline.
        cached_row = QHBoxLayout()
        cached_row.addWidget(QLabel("or use downloaded:"))
        self.cached_combo = QComboBox()
        self.cached_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        self.cached_combo.setMaximumWidth(360)
        cached_row.addWidget(self.cached_combo, 1)
        self.load_cached_btn = QPushButton("Load")
        self.load_cached_btn.clicked.connect(self.on_load_cached)
        cached_row.addWidget(self.load_cached_btn)
        fv.addLayout(cached_row)

        self.fw_info = QLabel("No firmware selected.")
        self.fw_info.setWordWrap(True)
        self.fw_info.setTextFormat(Qt.TextFormat.RichText)
        fv.addWidget(self.fw_info)
        root.addWidget(fw_box)

        # --- Options + Flash ---
        flash_box = QGroupBox("3.  Flash")
        gv = QVBoxLayout(flash_box)
        opt_row = QHBoxLayout()
        self.skip_trigger = QCheckBox("Device already in DFU/bootloader mode (skip 'start ota' trigger)")
        opt_row.addWidget(self.skip_trigger)
        opt_row.addStretch(1)
        self.reliable_check = QCheckBox("Skip slow 20-byte fallback")
        self.reliable_check.setToolTip(
            "The flash streams at the full packet size first (matching the Nordic app), then\n"
            "falls back to smaller packets if needed. Ticking this omits the slow 20-byte\n"
            "last-resort rung — leave it UNticked unless you want the flash to fail rather\n"
            "than crawl at 20 bytes on a difficult adapter."
        )
        opt_row.addWidget(self.reliable_check)
        self.verbose_check = QCheckBox("Verbose log")
        self.verbose_check.setToolTip("Log every packet-receipt and extra timing detail for debugging.")
        opt_row.addWidget(self.verbose_check)
        opt_row.addWidget(QLabel("PRN:"))
        self.prn_spin = QSpinBox()
        self.prn_spin.setRange(0, 100)
        self.prn_spin.setValue(C.DEFAULT_PRN)
        self.prn_spin.setToolTip(
            "Packet-Receipt-Notification interval — the device acks every N firmware packets "
            "(flow control).\n"
            "Higher N = fewer round-trips = faster, but too high can overflow the device.\n"
            "This is safe to experiment with: a corrupted transfer fails the CRC check at the "
            "end and is NOT activated (old firmware is kept).\n"
            "10 is MeshCore's recommended value for RAK; try 20–30 to speed up a stock "
            "(MTU 23) bootloader flash."
        )
        opt_row.addWidget(self.prn_spin)
        prn_note = QLabel("(device acks every N packets; higher = faster, 10 = safe for RAK)")
        prn_note.setStyleSheet("color: gray;")
        opt_row.addWidget(prn_note)
        gv.addLayout(opt_row)

        self.flash_btn = QPushButton("⚡  Flash firmware")
        self.flash_btn.setMinimumHeight(40)
        f = QFont()
        f.setBold(True)
        self.flash_btn.setFont(f)
        self.flash_btn.clicked.connect(self.on_flash)
        gv.addWidget(self.flash_btn)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        gv.addWidget(self.progress)

        stat_row = QHBoxLayout()
        self.phase_label = QLabel("Idle")
        self.rate_label = QLabel("")
        self.rate_label.setAlignment(Qt.AlignmentFlag.AlignRight)
        stat_row.addWidget(self.phase_label, 1)
        stat_row.addWidget(self.rate_label, 1)
        gv.addLayout(stat_row)
        root.addWidget(flash_box)

        # --- Log ---
        log_hdr = QHBoxLayout()
        log_hdr.addWidget(QLabel("Log"))
        log_hdr.addStretch(1)
        self.save_log_btn = QPushButton("Save log…")
        self.save_log_btn.clicked.connect(self.on_save_log)
        log_hdr.addWidget(self.save_log_btn)
        self.clear_log_btn = QPushButton("Clear")
        self.clear_log_btn.clicked.connect(lambda: self.log_view.clear())
        log_hdr.addWidget(self.clear_log_btn)
        root.addLayout(log_hdr)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(20000)
        mono = QFont("Consolas")
        mono.setStyleHint(QFont.StyleHint.Monospace)
        self.log_view.setFont(mono)
        root.addWidget(self.log_view, 1)

        self._refresh_flash_enabled()

    # -------------------------------------------------------------- helpers
    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        for w in (self.scan_btn, self.browse_btn, self.fetch_btn, self.fetch_otafix_btn,
                  self.load_cached_btn, self.flash_btn):
            w.setEnabled(not busy)
        if not busy:
            self._refresh_flash_enabled()
        # Live signal scanner runs ONLY while idle with a device selected — never during a
        # flash/scan (so it can't steal radio time from the transfer).
        self._request_rssi_scanning((not busy) and self._selected is not None)

    def _refresh_flash_enabled(self) -> None:
        self.flash_btn.setEnabled(
            not self._busy and self._selected is not None and self._image is not None
        )

    def _log(self, text: str) -> None:
        self.log_view.appendPlainText(f"{datetime.now().strftime('%H:%M:%S.%f')[:-3]}  {text}")

    def on_save_log(self) -> None:
        default = os.path.join(
            os.getcwd(), f"meshcore-flash-log-{datetime.now().strftime('%Y%m%d-%H%M%S')}.txt"
        )
        path, _ = QFileDialog.getSaveFileName(self, "Save log", default, "Text files (*.txt)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.log_view.toPlainText())
            self._log(f"Log saved to {path}")
        except Exception as e:  # noqa: BLE001
            self._log(f"Could not save log: {e}")

    # ----------------------------------------------------- live signal meter
    def _request_rssi_scanning(self, active: bool) -> None:
        """Schedule the background advertisement scanner on/off (safe to call from sync code)."""
        try:
            asyncio.ensure_future(self._set_rssi_scanning(active))
        except RuntimeError:
            pass  # event loop not running yet

    async def _set_rssi_scanning(self, active: bool) -> None:
        async with self._rssi_lock:
            if active and self._rssi_scanner is None:
                try:
                    self._rssi_scanner = BleakScanner(detection_callback=self._on_adv)
                    await self._rssi_scanner.start()
                except Exception as e:  # noqa: BLE001
                    self._rssi_scanner = None
                    self._log(f"(live signal scan unavailable: {type(e).__name__}: {e})")
            elif not active and self._rssi_scanner is not None:
                scanner, self._rssi_scanner = self._rssi_scanner, None
                try:
                    await scanner.stop()
                except Exception:
                    pass

    def _on_adv(self, device, adv) -> None:
        if adv is not None and adv.rssi is not None:
            self._live_rssi[device.address] = int(adv.rssi)

    @staticmethod
    def _rssi_bars_quality(rssi: int) -> tuple[str, str, str]:
        if rssi >= -55:
            return "█████", "Excellent", "#27ae60"
        if rssi >= -67:
            return "████░", "Good", "#27ae60"
        if rssi >= -78:
            return "███░░", "Fair", "#d4a017"
        if rssi >= -88:
            return "██░░░", "Weak", "#e67e22"
        return "█░░░░", "Very weak", "#c0392b"

    def _refresh_rssi_label(self) -> None:
        if self._selected is None:
            self.rssi_value.setText("—")
            self.rssi_value.setStyleSheet("")
            self.rssi_bars.setText("")
            self.rssi_quality.setText("select a device")
            return
        if self._busy:
            self.rssi_quality.setText("paused (flashing)")
            return
        rssi = self._live_rssi.get(self._selected.address)
        if rssi is None:
            self.rssi_value.setText("…")
            self.rssi_value.setStyleSheet("")
            self.rssi_bars.setText("")
            self.rssi_quality.setText("listening…")
            return
        bars, word, color = self._rssi_bars_quality(rssi)
        self.rssi_value.setText(f"{rssi} dBm")
        self.rssi_value.setStyleSheet(f"color: {color};")
        self.rssi_bars.setText(bars)
        self.rssi_bars.setStyleSheet(f"color: {color};")
        self.rssi_quality.setText(word)

    # ----------------------------------------------------------- scan
    @asyncSlot()
    async def on_scan(self) -> None:
        if self._busy:
            return
        self._set_busy(True)
        self.scan_status.setText("Scanning (6 s)...")
        self.device_list.clear()
        self._devices = []
        self._selected = None
        try:
            self._devices = await scan(timeout=6.0)
        except Exception as e:  # noqa: BLE001
            self.scan_status.setText("Scan failed.")
            self._log(f"Scan error: {type(e).__name__}: {e}")
            self._set_busy(False)
            return
        # Hide unnamed "(unknown)" clutter, but keep DFU/OTA targets even if unnamed.
        self._devices = [
            d for d in self._devices
            if (d.name and d.name != "(unknown)") or d.is_dfu_bootloader or d.is_ota_armed
        ]
        for d in self._devices:
            tag = f"  [{d.tag}]" if d.tag else ""
            item = QListWidgetItem(f"{d.name}   ({d.address})   RSSI {d.rssi} dBm{tag}")
            self.device_list.addItem(item)
        self.scan_status.setText(f"Found {len(self._devices)} device(s).")
        self._log(f"Scan complete — {len(self._devices)} device(s).")
        self._set_busy(False)

    def _on_device_selected(self, row: int) -> None:
        if 0 <= row < len(self._devices):
            self._selected = self._devices[row]
            self.skip_trigger.setChecked(self._selected.is_dfu_bootloader)
            # seed the meter with the scan-time RSSI so it isn't blank until the next advert
            self._live_rssi.setdefault(self._selected.address, self._selected.rssi)
        else:
            self._selected = None
        self._request_rssi_scanning((not self._busy) and self._selected is not None)
        self._refresh_rssi_label()
        self._refresh_flash_enabled()

    def closeEvent(self, event) -> None:
        try:
            self._rssi_timer.stop()
        except Exception:
            pass
        self._request_rssi_scanning(False)
        super().closeEvent(event)

    # ----------------------------------------------------------- firmware
    def on_browse(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Select firmware .zip", os.getcwd(), "DFU package (*.zip);;All files (*.*)"
        )
        if path:
            self._load_firmware(path)

    def _refresh_downloaded(self) -> None:
        """List previously-downloaded .zip packages (newest first) for offline reuse."""
        files = []
        try:
            if os.path.isdir(DOWNLOAD_DIR):
                files = [f for f in os.listdir(DOWNLOAD_DIR) if f.lower().endswith(".zip")]
                files.sort(
                    key=lambda f: os.path.getmtime(os.path.join(DOWNLOAD_DIR, f)), reverse=True
                )
        except Exception:
            files = []
        current = self.cached_combo.currentText()
        self.cached_combo.clear()
        if files:
            self.cached_combo.addItems(files)
            idx = self.cached_combo.findText(current)
            if idx >= 0:
                self.cached_combo.setCurrentIndex(idx)
        else:
            self.cached_combo.addItem("(no downloads yet)")
        self.load_cached_btn.setEnabled(bool(files) and not self._busy)

    def on_load_cached(self) -> None:
        if self._busy:
            return
        name = self.cached_combo.currentText()
        path = os.path.join(DOWNLOAD_DIR, name)
        if name and os.path.isfile(path):
            self._load_firmware(path)
        else:
            self.fw_info.setText("No downloaded package selected.")

    def _load_firmware(self, path: str) -> None:
        try:
            img = load_dfu_zip(path)
        except DfuPackageError as e:
            self._image = None
            self._firmware_path = None
            self.fw_info.setText(f"<span style='color:#c0392b'>{e}</span>".replace("\n", "<br>"))
            self._log(f"Firmware rejected: {e}")
            self._refresh_flash_enabled()
            return
        self._image = img
        self._firmware_path = path
        dt_ok = img.device_type_ok()
        dt_color = "#27ae60" if dt_ok else "#c0392b"
        dt_note = "OK" if dt_ok else f"MISMATCH — expected 0x{C.ADAFRUIT_DEVICE_TYPE:04X}!"
        self.fw_info.setText(
            f"<b>{os.path.basename(path)}</b><br>"
            f"Type: {img.mode_label} &nbsp;|&nbsp; image size: {_fmt_bytes(img.total_size)}<br>"
            f"device_type: <span style='color:{dt_color}'>0x{(img.device_type or 0):04X} ({dt_note})</span>"
            f" &nbsp;|&nbsp; CRC16: 0x{(img.firmware_crc16 or 0):04X}"
            f" &nbsp;|&nbsp; app_version: {img.app_version}"
        )
        self._log(f"Loaded firmware: {os.path.basename(path)} ({_fmt_bytes(img.total_size)})")
        self._refresh_flash_enabled()

    @asyncSlot()
    async def on_fetch_latest(self) -> None:
        if self._busy:
            return
        role = self.role_combo.currentText()
        self._set_busy(True)
        self.fw_info.setText(f"Looking up latest RAK4631 {role} firmware...")
        try:
            asset = await asyncio.to_thread(gh.latest_meshcore_firmware, role)
            self._log(f"Latest {role}: {asset.name} ({asset.tag}, {_fmt_bytes(asset.size)})")
            os.makedirs(DOWNLOAD_DIR, exist_ok=True)
            dest = os.path.join(DOWNLOAD_DIR, asset.name)
            if os.path.isfile(dest):
                self._log("Already downloaded — using the cached copy.")
            else:
                self.fw_info.setText(f"Downloading {asset.name}...")

                def prog(recv: int, total: int) -> None:
                    pct = int(recv * 100 / total) if total else 0
                    self.progress.setValue(pct)

                await asyncio.to_thread(gh.download, asset.url, dest, prog)
                self.progress.setValue(0)
            self._load_firmware(dest)
            self._refresh_downloaded()
        except Exception as e:  # noqa: BLE001
            self.fw_info.setText(f"<span style='color:#c0392b'>Fetch failed: {e}</span>")
            self._log(f"Fetch failed: {type(e).__name__}: {e}")
        finally:
            self._set_busy(False)

    # ------------------------------------------------------- OTA-fix bootloader
    @asyncSlot()
    async def on_load_otafix_boards(self) -> None:
        """Fill the OTA-fix board combo from the latest OTAFIX release (background)."""
        try:
            boards = await asyncio.to_thread(gh.list_otafix_bootloader_zips)
        except Exception as e:  # noqa: BLE001
            self._log(f"(OTA-fix board list unavailable: {type(e).__name__}: {e})")
            return
        self._otafix_assets = boards
        self.otafix_combo.clear()
        for label, _asset in boards:
            self.otafix_combo.addItem(label)
        for i, (label, _asset) in enumerate(boards):
            if "rak4631" in label.lower():
                self.otafix_combo.setCurrentIndex(i)
                break

    @asyncSlot()
    async def on_fetch_otafix(self) -> None:
        if self._busy:
            return
        if not self._otafix_assets:
            await self.on_load_otafix_boards()
            if not self._otafix_assets:
                self.fw_info.setText(
                    "<span style='color:#c0392b'>Could not load the OTA-fix board list "
                    "(offline or GitHub rate-limited).</span>"
                )
                return
        idx = self.otafix_combo.currentIndex()
        if not (0 <= idx < len(self._otafix_assets)):
            return
        label, asset = self._otafix_assets[idx]
        self._set_busy(True)
        self.fw_info.setText(f"Downloading OTA-fix bootloader for {label}...")
        try:
            os.makedirs(DOWNLOAD_DIR, exist_ok=True)
            dest = os.path.join(DOWNLOAD_DIR, asset.name)
            self._log(f"OTA-fix bootloader: {asset.name} ({asset.tag}, {_fmt_bytes(asset.size)})")
            if os.path.isfile(dest):
                self._log("Already downloaded — using the cached copy.")
            else:
                await asyncio.to_thread(gh.download, asset.url, dest)
            self._load_firmware(dest)
            self._refresh_downloaded()
        except Exception as e:  # noqa: BLE001
            self.fw_info.setText(f"<span style='color:#c0392b'>Fetch failed: {e}</span>")
            self._log(f"OTA-fix fetch failed: {type(e).__name__}: {e}")
        finally:
            self._set_busy(False)

    # ----------------------------------------------------------- flash
    @asyncSlot()
    async def on_flash(self) -> None:
        if self._busy or self._selected is None or self._image is None:
            return
        if not self._image.device_type_ok():
            QMessageBox.critical(
                self, "Wrong firmware",
                "The init packet device_type does not match this hardware. "
                "Flashing is blocked to avoid bricking the node.",
            )
            return
        if self._image.is_bootloader:
            warn = QMessageBox.warning(
                self, "Flashing a bootloader over BLE",
                C.BOOTLOADER_FLASH_WARNING.format(
                    file=os.path.basename(self._firmware_path or "")
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if warn != QMessageBox.StandardButton.Yes:
                return
        confirm = QMessageBox.question(
            self, "Confirm flash",
            f"Flash '{os.path.basename(self._firmware_path or '')}'\n"
            f"to '{self._selected.name}' [{self._selected.address}]?\n\n"
            f"Do not disconnect or power off the device during the transfer.",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return

        self._set_busy(True)
        self.progress.setValue(0)
        self.rate_label.setText("")
        self._flash_start = time.monotonic()
        self._log("=== Starting flash ===")
        await self.controller.flash(
            self._selected,
            self._image,
            self.skip_trigger.isChecked(),
            self.prn_spin.value(),
            reliable=self.reliable_check.isChecked(),
            verbose=self.verbose_check.isChecked(),
        )

    # ----------------------------------------------------------- signals
    def _on_log(self, text: str) -> None:
        self._log(text)

    def _on_phase(self, phase: str) -> None:
        self.phase_label.setText(phase)

    def _on_progress(self, sent: int, total: int, bps: float) -> None:
        pct = int(sent * 100 / total) if total else 0
        self.progress.setValue(pct)
        kib_s = bps / 8 / 1024
        remaining = total - sent
        eta = (remaining * 8 / bps) if bps > 0 else 0
        self.rate_label.setText(
            f"{_fmt_bytes(sent)} / {_fmt_bytes(total)}  •  {kib_s:.1f} KiB/s  •  ETA {eta:0.0f}s"
        )

    def _on_finished(self, ok: bool, message: str) -> None:
        self._set_busy(False)
        self.phase_label.setText("Complete" if ok else "Failed")
        self._log(("SUCCESS: " if ok else "ERROR: ") + message)
        if ok:
            self.progress.setValue(100)
            # A bootloader flash erased the app — clear the loaded bootloader package so the
            # user picks the MeshCore app for stage 2 (and can't re-flash the bootloader).
            if self._image is not None and self._image.is_bootloader:
                self._image = None
                self._firmware_path = None
                self.fw_info.setText(
                    "Bootloader updated — now select the MeshCore <b>app</b> firmware and flash "
                    "it (tick 'skip trigger'), then re-provision the node."
                )
                # The node rebooted into the new bootloader on a fresh DFU advert (new MAC),
                # so the old selection is stale. Clear it and auto-rescan so stage 2 lands on
                # the new *_DFU device (selecting it auto-ticks 'skip trigger').
                self._selected = None
                self.device_list.clearSelection()
                self._refresh_flash_enabled()
                QTimer.singleShot(3000, self.on_scan)
            self._refresh_flash_enabled()
            QMessageBox.information(self, "Flash complete", message)
        else:
            QMessageBox.critical(self, "Flash failed", message)
