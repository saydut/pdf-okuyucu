import json
import os
import sys
import threading
import webbrowser
from typing import Optional, Tuple

import requests
from PySide6.QtCore import Qt, QTimer, QUrl, QPointF
from PySide6.QtGui import QKeySequence
from PySide6.QtPrintSupport import QPrinter, QPrintDialog
from PySide6.QtPdf import QPdfDocument
from PySide6.QtPdfWidgets import QPdfView
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSlider,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


APP_NAME = "Saydut PDF"
APP_VERSION = "1.0"
PROGRAMS_URL = "https://www.saydut.com/static/programs.json"
PROGRAM_ID = "pdf-okuyucu"

CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Saydut", "PdfReader")
RECENTS_PATH = os.path.join(CONFIG_DIR, "recent.json")


def ensure_config_dir() -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)


def _semver_tuple(v: str) -> Tuple[int, int, int]:
    v = (v or "").strip()
    if v.startswith("v"):
        v = v[1:]
    parts = v.split(".")
    out = []
    for i in range(3):
        try:
            out.append(int(parts[i]))
        except Exception:
            out.append(0)
    return tuple(out)  # type: ignore


def _http_get_json(url: str, timeout: int = 15) -> dict:
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    return response.json()


class ZoomPdfView(QPdfView):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setPageMode(QPdfView.PageMode.MultiPage)
        self.setZoomMode(QPdfView.ZoomMode.Custom)
        self.setZoomFactor(1.0)

    def wheelEvent(self, event) -> None:
        delta = event.angleDelta().y()
        if delta == 0:
            return super().wheelEvent(event)
        factor = 1.1 if delta > 0 else 1 / 1.1
        new_zoom = max(0.2, min(self.zoomFactor() * factor, 5.0))
        self.setZoomFactor(new_zoom)
        event.accept()


class PdfMainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1200, 780)

        ensure_config_dir()
        self._update_prompted = False
        self.recent_files = self._load_recent_files()

        self.doc = QPdfDocument(self)
        self.view = ZoomPdfView()
        self.view.setDocument(self.doc)
        self.navigator = self.view.pageNavigator()
        self.navigator.currentPageChanged.connect(self._on_page_changed)
        self.doc.statusChanged.connect(self._on_doc_status)
        self._pending_open = False

        self._build_ui()
        QTimer.singleShot(1500, self.check_launcher_update_background)

    def _build_ui(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel("Saydut PDF Reader")
        title.setStyleSheet("font-size:20px; font-weight:bold;")
        header.addWidget(title)
        header.addStretch()
        layout.addLayout(header)

        toolbar = QHBoxLayout()

        btn_open = QPushButton("PDF Ac")
        btn_open.clicked.connect(self.open_file)
        toolbar.addWidget(btn_open)

        btn_print = QPushButton("Yazdir")
        btn_print.clicked.connect(self.print_dialog)
        toolbar.addWidget(btn_print)

        toolbar.addSpacing(12)

        btn_zoom_out = QToolButton()
        btn_zoom_out.setText("-")
        btn_zoom_out.clicked.connect(lambda: self.adjust_zoom(-0.1))
        toolbar.addWidget(btn_zoom_out)

        self.zoom_slider = QSlider(Qt.Horizontal)
        self.zoom_slider.setMinimum(50)
        self.zoom_slider.setMaximum(400)
        self.zoom_slider.setValue(100)
        self.zoom_slider.valueChanged.connect(self.slider_zoom_changed)
        toolbar.addWidget(self.zoom_slider)

        btn_zoom_in = QToolButton()
        btn_zoom_in.setText("+")
        btn_zoom_in.clicked.connect(lambda: self.adjust_zoom(0.1))
        toolbar.addWidget(btn_zoom_in)

        self.btn_fit_width = QToolButton()
        self.btn_fit_width.setText("Genislige sigdir")
        self.btn_fit_width.clicked.connect(self.fit_width)
        toolbar.addWidget(self.btn_fit_width)

        self.btn_fit_page = QToolButton()
        self.btn_fit_page.setText("Sayfaya sigdir")
        self.btn_fit_page.clicked.connect(self.fit_page)
        toolbar.addWidget(self.btn_fit_page)

        toolbar.addSpacing(12)

        self.page_spin = QSpinBox()
        self.page_spin.setMinimum(1)
        self.page_spin.valueChanged.connect(self.go_to_page)
        toolbar.addWidget(self.page_spin)

        self.total_pages = QLabel("/ 0")
        toolbar.addWidget(self.total_pages)

        toolbar.addSpacing(12)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Metin ara (QtPdf)")
        toolbar.addWidget(self.search_input)

        layout.addLayout(toolbar)
        layout.addWidget(self.view, 1)

        self.setCentralWidget(root)
        self._bind_shortcuts()

    def _bind_shortcuts(self) -> None:
        self.addAction(self._make_action("Open", QKeySequence("Ctrl+O"), self.open_file))
        self.addAction(self._make_action("Print", QKeySequence("Ctrl+P"), self.print_dialog))
        self.addAction(self._make_action("ZoomIn", QKeySequence.ZoomIn, lambda: self.adjust_zoom(0.1)))
        self.addAction(self._make_action("ZoomOut", QKeySequence.ZoomOut, lambda: self.adjust_zoom(-0.1)))

    def _make_action(self, name: str, shortcut: QKeySequence, handler) -> None:
        act = self.addAction(name)
        act.setShortcut(shortcut)
        act.triggered.connect(handler)
        return act

    def open_file(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(self, "PDF Ac", "", "PDF Files (*.pdf)")
        if not file_path:
            return
        self.open_document(file_path)

    def open_document(self, file_path: str) -> None:
        status = self.doc.load(file_path)
        self._pending_open = True
        if status == QPdfDocument.Status.Error:
            self._pending_open = False
            QMessageBox.warning(self, APP_NAME, "PDF acilamadi.")
            return
        self._add_recent_file(file_path)
        if status == QPdfDocument.Status.Ready:
            self._pending_open = False
            self._update_page_count()
            self.navigator.jump(0, QPointF(0, 0), None)

    def _update_page_count(self) -> None:
        count = self.doc.pageCount()
        self.page_spin.blockSignals(True)
        self.page_spin.setMaximum(max(count, 1))
        self.page_spin.setValue(1 if count else 0)
        self.page_spin.blockSignals(False)
        self.total_pages.setText(f"/ {count}")

    def _on_page_changed(self, page: int) -> None:
        self.page_spin.blockSignals(True)
        self.page_spin.setValue(page + 1)
        self.page_spin.blockSignals(False)

    def _on_doc_status(self, status) -> None:
        if not self._pending_open:
            return
        if status == QPdfDocument.Status.Ready:
            self._pending_open = False
            self._update_page_count()
            self.navigator.jump(0, QPointF(0, 0), None)
        elif status == QPdfDocument.Status.Error:
            self._pending_open = False
            QMessageBox.warning(self, APP_NAME, "PDF acilamadi.")

    def go_to_page(self) -> None:
        page = self.page_spin.value() - 1
        if page < 0 or page >= self.doc.pageCount():
            return
        self.navigator.jump(page, QPointF(0, 0), None)

    def adjust_zoom(self, delta: float) -> None:
        zoom = self.view.zoomFactor() + delta
        zoom = max(0.2, min(zoom, 5.0))
        self.view.setZoomMode(QPdfView.ZoomMode.Custom)
        self.view.setZoomFactor(zoom)
        self.zoom_slider.blockSignals(True)
        self.zoom_slider.setValue(int(zoom * 100))
        self.zoom_slider.blockSignals(False)

    def slider_zoom_changed(self, value: int) -> None:
        zoom = value / 100.0
        self.view.setZoomMode(QPdfView.ZoomMode.Custom)
        self.view.setZoomFactor(zoom)

    def fit_width(self) -> None:
        self.view.setZoomMode(QPdfView.ZoomMode.FitToWidth)

    def fit_page(self) -> None:
        self.view.setZoomMode(QPdfView.ZoomMode.FitInView)

    def print_dialog(self) -> None:
        if self.doc.pageCount() == 0:
            return
        printer = QPrinter()
        dialog = QPrintDialog(printer, self)
        if dialog.exec() == QPrintDialog.Accepted:
            self.view.print_(printer)

    def _load_recent_files(self) -> list:
        if not os.path.exists(RECENTS_PATH):
            return []
        try:
            with open(RECENTS_PATH, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, list) else []
        except (OSError, json.JSONDecodeError):
            return []

    def _save_recent_files(self) -> None:
        try:
            with open(RECENTS_PATH, "w", encoding="utf-8") as handle:
                json.dump(self.recent_files[:10], handle, indent=2)
        except OSError:
            pass

    def _add_recent_file(self, file_path: str) -> None:
        if file_path in self.recent_files:
            self.recent_files.remove(file_path)
        self.recent_files.insert(0, file_path)
        self._save_recent_files()

    def check_launcher_update_background(self) -> None:
        if self._update_prompted:
            return

        def worker() -> None:
            try:
                payload = _http_get_json(PROGRAMS_URL)
                latest = ""
                for item in payload.get("programs", []):
                    if item.get("id") == PROGRAM_ID:
                        latest = str(item.get("version", "")).strip()
                        break
                if latest and _semver_tuple(latest) > _semver_tuple(APP_VERSION):
                    self._update_prompted = True
                    self._show_update_prompt(latest)
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _show_update_prompt(self, latest: str) -> None:
        def show() -> None:
            if QMessageBox.question(
                self,
                "Guncelleme mevcut",
                f"Mevcut surum: {APP_VERSION}\nYeni surum: {latest}\n\nGuncellemeyi Saydut Launcher ile yapmak ister misin?",
            ) == QMessageBox.Yes:
                self.open_launcher_update()

        QTimer.singleShot(0, show)

    def open_launcher_update(self) -> None:
        launcher_hint_path = r"C:\Saydut\launcher_path.txt"
        launcher_candidates = [
            r"C:\Saydut\SaydutLauncher\SaydutLauncher.exe",
            r"C:\Saydut\Saydut Launcher\SaydutLauncher.exe",
            r"C:\Saydut\SaydutLauncher\Saydut Launcher.exe",
        ]

        launcher_path = None
        if os.path.exists(launcher_hint_path):
            try:
                with open(launcher_hint_path, "r", encoding="utf-8") as handle:
                    candidate = handle.read().strip()
                if candidate and os.path.exists(candidate):
                    launcher_path = candidate
            except OSError:
                launcher_path = None

        if not launcher_path:
            for candidate in launcher_candidates:
                if os.path.exists(candidate):
                    launcher_path = candidate
                    break

        if launcher_path:
            os.startfile(launcher_path)  # type: ignore[attr-defined]
            return

        QMessageBox.information(
            self,
            "Launcher gerekli",
            "Guncelleme icin Saydut Launcher gerekli. Launcher sayfasini aciyorum.",
        )
        webbrowser.open("https://www.saydut.com")


def main() -> None:
    app = QApplication(sys.argv)
    window = PdfMainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
