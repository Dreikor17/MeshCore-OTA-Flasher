"""Entry point: python -m nordic_ota_flasher"""

from __future__ import annotations

import asyncio
import sys

# bleak's WinRT backend needs an MTA or a running Windows message loop. Qt provides a
# running (STA) message loop, so tell bleak that's fine. Must happen before any BLE op.
try:
    from bleak.backends.winrt.util import allow_sta

    allow_sta()
except Exception:
    pass

from PySide6.QtWidgets import QApplication  # noqa: E402
import qasync  # noqa: E402

from .gui.main_window import MainWindow  # noqa: E402


def main() -> int:
    # Raise the Windows timer resolution to 1 ms so the small per-packet pacing sleeps used
    # during streaming are honored (the default ~15.6 ms granularity would make them crawl).
    _winmm = None
    try:
        import ctypes

        _winmm = ctypes.WinDLL("winmm")
        _winmm.timeBeginPeriod(1)
    except Exception:
        _winmm = None

    app = QApplication(sys.argv)
    app.setApplicationName("RFLab.io OTA Flasher")
    app.setStyle("Fusion")

    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)

    window = MainWindow()
    window.show()

    try:
        with loop:
            loop.run_forever()
    finally:
        if _winmm is not None:
            try:
                _winmm.timeEndPeriod(1)
            except Exception:
                pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
