import signal
import sys
import time

from hid import HID_MODIFIERS_TO_DESCRIPTION, HID_TO_DESCRIPTION
from messages import DOOMKeystroke, DOOMKeystrokeList


# import sdl3
from PyQt6 import QtGui
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QProgressBar,
    QPushButton,
    QStyleFactory,
    QTabWidget,
    QTreeView,
    QVBoxLayout,
    QWidget,
    QGraphicsScene,
    QGraphicsView,
    QGraphicsPixmapItem
    
)
import numpy as np

def perf_counter_ms() -> int:
    return time.perf_counter_ns() // 1_000_000


HEADING_FONT = QtGui.QFont("Avenir Next", 24, QtGui.QFont.Weight.DemiBold)
BODY_FONT = QtGui.QFont("Avenir Next", 18)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        self.init_ui()

    def init_ui(self):
        self.setWindowTitle("DOOMSat Ground Station")
        self.setMinimumSize(400, 300)
        self.resize(800, 600)

        shortcut = QtGui.QShortcut(QtGui.QKeySequence("Ctrl+W"), self)
        shortcut.activated.connect(self.close)

        tabs = QTabWidget()
        tabs.addTab(KeyRecordingPage(), "Keystrokes")
        tabs.addTab(VideoFeedPage(), "DOOM Viewer")
        tabs.addTab(QWidget(), "Satellite Status")

        self.setCentralWidget(tabs)

class VideoFeedPage(QWidget):
    def __init__(self):
        super().__init__()

        title = QLabel("Live DOOM Feed")
        title.setFont(HEADING_FONT)

        #raw image data and parameters
        self.raw_data = b''
        self.height = 200
        self.width = 320
        self.colormap_list = self.colormap_from_hex("./assets/pallet0.hex")

        #Create QgraphicsView and Scene to hold the video feed
        self.view = QGraphicsView()
        self.scene = QGraphicsScene(self)
        self.view.setScene(self.scene)
        self.view.setMinimumSize(self.width, self.height)

        #Create a persistent pixmap_item item to hold image frame and scale to match CRT aspect ratio (20% taller than width)
        self.pixmap_item = QGraphicsPixmapItem()
        transform = QtGui.QTransform()
        transform.scale(1.0, 1.2) #scale up y pixels by 20% to match original CRT aspect ratio
        self.pixmap_item.setTransform(transform)
        self.view.fitInView(self.view.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)
        self.scene.addItem(self.pixmap_item)
        
        #Add a button to generate random data for testing
        self.btn_refresh = QPushButton("Generate New Raw Data")
        self.btn_refresh.clicked.connect(self.update_with_random_data)

        #add to layout
        layout = QVBoxLayout()
        layout.addWidget(title)
        layout.addWidget(self.view)
        layout.addWidget(self.btn_refresh)
        self.setLayout(layout)

    def update_with_random_data(self):
        # Generate dummy 320x200 L8 RGB data
        self.raw_data = np.random.randint(0, 255, (self.height, self.width), dtype=np.uint8).flatten().tobytes()
        self.update_image()
    
    def update_image(self):
        # Convert Raw Data -> QImage -> QPixmap
        q_img = QtGui.QImage(self.raw_data, self.width, self.height, self.width*1, QtGui.QImage.Format.Format_Indexed8)
        # Set a color table for the indexed image
        q_img.setColorTable(self.colormap_list)

        # Display via Pixmap Item
        pixmap = QtGui.QPixmap.fromImage(q_img)
        self.pixmap_item.setPixmap(pixmap)

        #fit to view 
        self.view.fitInView(self.view.sceneRect(), Qt.AspectRatioMode.KeepAspectRatio)

    
    def colormap_from_hex(self, filename: str) -> list[QtGui.QRgb]:
        """
        Create a QList of QRgb colors from a list of hex color strings.

        Args:
            hex_file: List of hex color strings, one per line, in the format "RRGGBB" (e.g., "FF0000" for red).

        Returns:
            list[QRgb]: A Qt color list ready for use in palettes or colormaps.

        Example:
            >>> colors = colormap_from_hex("my_colors.hex")
        
        """
        qrgb_list: list[QtGui.QRgb] = []
        
        try:
            with open(filename, 'rb') as f:
                hex_values = [line.rstrip() for line in f.readlines()]
        except FileNotFoundError:
            # Return an empty list if the file doesn't exist
            return qrgb_list

        for hex_str in hex_values:
            # Parse hex to integer
            rgb_int = int(hex_str, 16)
            qrgb = QtGui.qRgb((rgb_int >> 16) & 0xFF, (rgb_int >> 8) & 0xFF, rgb_int & 0xFF)
            qrgb_list.append(qrgb)
        
        return qrgb_list

    #NOTES -M.R.O.
    ## Each pallet will be its down hex file
    ## want to create a dict of pallets , where each value is a qlist of QRgb colors
    ## Make a ColorPallet<dict>
    ## methods to load file from hex and convert to list of QRgb colors
    ## has default return pallet if file not found 


class KeyRecordingPage(QWidget):
    KEYSTROKE_TIMEOUT_MS = 2_000

    def __init__(self):
        super().__init__()

        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self.key_timer = QTimer()
        self.key_timer.setInterval(1000 // 30)  # 30 reads/sec
        self.key_timer.timeout.connect(self.read_state)
        self.key_timer.start()

        self.progressbar_timer = QTimer()
        self.progressbar_timer.setInterval(1000 // 60)  # 60fps
        self.progressbar_timer.timeout.connect(self.update_bar)
        self.progressbar_timer.start()

        # The set of currently pressed keyboard keys
        self.active_keys: set[Qt.Key] = set()
        # The last time a keystroke was recorded
        self.last_key_update = 0
        self.key_list = DOOMKeystrokeList()

        self.init_ui()

    def init_ui(self):
        layout = QHBoxLayout()

        left_layout = QVBoxLayout()

        heading = QLabel("Keystroke Capture")
        heading.setFont(HEADING_FONT)
        heading.setMinimumWidth(300)

        description = QLabel(
            "Recording will start when you first begin typing. Recording ends two seconds after your last keystroke."
        )
        description.setWordWrap(True)
        description.setFont(BODY_FONT)

        self.rec_ind = RecordingIndicator()

        left_layout.addWidget(heading)
        left_layout.addWidget(description)
        left_layout.addWidget(self.rec_ind)
        left_layout.addStretch()

        right_layout = QVBoxLayout()

        self.tree_model = QtGui.QStandardItemModel()

        hist_tree = QTreeView()
        hist_tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        hist_tree.setHeaderHidden(True)
        hist_tree.setModel(self.tree_model)
        hist_tree.setFocusPolicy(Qt.FocusPolicy.NoFocus)

        btn_box = QWidget()
        btn_layout = QHBoxLayout()

        export_btn = QPushButton()
        export_btn.setText("Export")
        btn_layout.addWidget(export_btn)

        send_btn = QPushButton()
        send_btn.setText("Send to Satellite Status")
        btn_layout.addWidget(send_btn)

        btn_box.setLayout(btn_layout)

        right_layout.addWidget(hist_tree)
        right_layout.addWidget(btn_box)

        layout.addLayout(left_layout)
        layout.addLayout(right_layout)

        self.setLayout(layout)

    def read_state(self) -> None:
        now = perf_counter_ms()
        elapsed = now - self.last_key_update

        # If no keys are pressed and it's been 2 seconds since the last press
        if not self.active_keys and elapsed > KeyRecordingPage.KEYSTROKE_TIMEOUT_MS:
            if self.key_list:
                self.key_list.remove_trailing_idles()
                print(
                    f"Produced recording of {len(self.key_list)} keys: {self.key_list}"
                )
                self.add_keyset_entry(self.key_list)
                self.key_list.clear()
            return

        new_stroke = DOOMKeystroke.from_qt_keys(self.active_keys)
        self.key_list.append(new_stroke)

    def add_keyset_entry(self, key_list: DOOMKeystrokeList) -> None:
        duration = len(key_list) * 1000 // 30
        (duration_s, duration) = (duration // 1000, duration % 1000)

        head = QtGui.QStandardItem(
            f"Keyboard ({duration_s}.{duration}s; {key_list.size_in_bytes} bytes)"
        )
        head.setFont(BODY_FONT)

        chunk_size = 6
        chunked = (
            key_list[i : i + chunk_size] for i in range(0, len(key_list), chunk_size)
        )

        labels = []
        for slice in chunked:
            modifiers = 0
            keys = set()
            for keystroke in slice:
                keys.add(keystroke.keys[0])
                keys.add(keystroke.keys[1])
                keys.add(keystroke.keys[2])
                modifiers |= keystroke.modifiers
            keys.discard(0)
            label = []
            for modifier in HID_MODIFIERS_TO_DESCRIPTION:
                if modifiers & modifier:
                    label.append(HID_MODIFIERS_TO_DESCRIPTION[modifier])
            for key in keys:
                label.append(HID_TO_DESCRIPTION[key])
            labels.append("-".join(label))

        for label in labels:
            item = QtGui.QStandardItem(label)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            item.setFont(BODY_FONT)
            head.appendRow(item)

        self.tree_model.appendRow(head)

    def update_bar(self) -> None:
        now = perf_counter_ms()
        elapsed = now - self.last_key_update

        if self.active_keys:
            # If keys are held, the bar stays full
            self.rec_ind.set_timeout_remaining(KeyRecordingPage.KEYSTROKE_TIMEOUT_MS)
            self.rec_ind.set_active(True)
        elif self.key_list:
            # Keys are still being listened for; it drains toward 0
            remaining = max(0, (KeyRecordingPage.KEYSTROKE_TIMEOUT_MS - elapsed))
            self.rec_ind.set_timeout_remaining(remaining)
            self.rec_ind.set_active(True)
        else:
            self.rec_ind.set_active(False)

    def keyPressEvent(self, a0: QtGui.QKeyEvent | None) -> None:
        if not a0:
            return

        self.active_keys.add(a0.keyCombination().key())
        self.last_key_update = perf_counter_ms()

        return super().keyPressEvent(a0)

    def keyReleaseEvent(self, a0: QtGui.QKeyEvent | None) -> None:
        if not a0:
            return

        self.active_keys.discard(a0.keyCombination().key())
        self.last_key_update = perf_counter_ms()

        return super().keyReleaseEvent(a0)


class RecordingIndicator(QWidget):
    def __init__(self):
        super().__init__()

        self.setContentsMargins(50, 0, 50, 0)

        self.box = QFrame()
        self.box.setObjectName("recording-indicator")

        box_layout = QGridLayout(self)
        status_layout = QVBoxLayout()
        status_layout.setContentsMargins(0, 0, 0, 0)

        self.status_label = QLabel()
        self.status_label.setContentsMargins(0, 10, 0, 2)
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.set_active(False)

        self.timeout_bar = QProgressBar()
        self.timeout_bar.setFixedHeight(5)
        self.timeout_bar.setTextVisible(False)
        self.timeout_bar.setRange(0, 2_000)
        self.timeout_bar.setStyleSheet("""
            QProgressBar {
                background-color: #d4d4d8;
                border: none;
            }
            QProgressBar::chunk {
                width: 1px;
                background-color: #991b1b;
            }
        """)

        status_layout.addWidget(self.status_label)
        status_layout.addWidget(self.timeout_bar)
        self.box.setLayout(status_layout)

        box_layout.addWidget(self.box, 0, 0)
        self.setLayout(box_layout)

    def set_timeout_remaining(self, remaining: int):
        self.timeout_bar.setValue(remaining)

    def set_active(self, is_recording: bool):
        IDLE = ("○ IDLE", "#52525b", "#e4e4e7")
        RECORDING = ("● RECORDING", "#991b1b", "#fecaca")

        style = RECORDING if is_recording else IDLE

        self.status_label.setText(style[0])
        self.status_label.setStyleSheet(f"""
            color: {style[1]};
            font-weight: bold;
        """)
        self.box.setStyleSheet(f"""
            background-color: {style[2]};
        """)


def main():
    print("Launching GS...")

    QApplication.setStyle(QStyleFactory.create("Fusion"))
    app = QApplication(sys.argv)

    app.setApplicationName("DOOMBalloon Ground Station")

    window = MainWindow()
    window.show()

    def sigint_handler(*args):
        app.quit()

    signal.signal(signal.SIGINT, sigint_handler)

    ecode = app.exec()
    print("Stopping GS...")
    sys.exit(ecode)


if __name__ == "__main__":
    main()
