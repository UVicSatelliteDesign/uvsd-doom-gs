"""
The Telemetry tab: live readouts, a ground track and stacked time-series plots
of telemetry_data.csv, refreshed every second.

The data side (schema, CSV reading, conversions, flight events) lives in
telemetry.py; this module only lays out and draws.
"""

import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import matplotlib.dates as mdates
import numpy as np
from matplotlib.artist import Artist
from matplotlib.axes import Axes
from matplotlib.backend_bases import DrawEvent, LocationEvent, MouseEvent
from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg, NavigationToolbar2QT
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from matplotlib.patches import Patch
from matplotlib.text import Text
from matplotlib.ticker import MaxNLocator
from PyQt6 import QtGui
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QBoxLayout,
    QButtonGroup,
    QCheckBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from telemetry import (
    COLUMNS,
    DEFAULT_CSV_PATH,
    FIELDS,
    PLOTS,
    Field,
    TelemetryFormatError,
    TelemetryLog,
    display_unit,
    display_values,
    flight_events,
    format_age,
    format_coordinate,
    format_gps_time,
    missing_spans,
)

HEADING_FONT = QtGui.QFont("Avenir Next", 24, QtGui.QFont.Weight.DemiBold)
VALUE_FONT = QtGui.QFont("Avenir Next", 12, QtGui.QFont.Weight.DemiBold)

SERIES_COLORS = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100")
SURFACE = "#fcfcfb"
GRIDLINE = "#e1e0d9"
AXIS_LINE = "#c3c2b7"
PRIMARY_TEXT = "#0b0b0b"
SECONDARY_TEXT = "#52514e"
MUTED_TEXT = "#898781"
STATUS_COLORS = {
    "LIVE": "#0ca30c",
    "STALE": "#fab219",
    "NO DATA": MUTED_TEXT,
    "ERROR": "#d03b3b",
}

LINE_WIDTH = 1.0
PLOT_HEIGHT_PX = 110
SIDEBAR_WIDTH_PX = 290
DEFAULT_PLOTS = ("altitude", "temperature", "battery_voltage", "groundspeed")
TIME_WINDOWS = {"All": None, "1 h": 3600, "15 min": 900, "5 min": 300}
SECONDS_PER_DAY = 86_400
STALE_AFTER_S = 10
MIN_TRACK_HALF_SPAN_DEG = 0.02
TITLES = dict(PLOTS)
HEMISPHERES = {"latitude": ("N", "S"), "longitude": ("E", "W")}
GPS_GROUPS = {field.group for field in FIELDS if field.key.startswith("gps_")}
NO_TIMES = np.array([], dtype="datetime64[s]")
STRETCH = None

NOTE_STYLE: dict[str, Any] = {
    "ha": "right",
    "va": "top",
    "fontsize": 8,
    "color": SECONDARY_TEXT,
    "clip_on": True,
    "in_layout": False,
}
READING_STYLE: dict[str, Any] = {
    "va": "top",
    "fontsize": 8,
    "color": PRIMARY_TEXT,
    "zorder": 4,
    "animated": True,
    "in_layout": False,
    "bbox": {"facecolor": SURFACE, "edgecolor": "none", "alpha": 0.9},
}

READOUT: tuple[tuple[str, tuple[str, ...], Callable[..., str]], ...] = (
    ("Altitude", ("altitude",), "{:.2f} km".format),
    ("Battery", ("battery_charge", "battery_voltage"), "{:.0f} %   {:.2f} V".format),
    ("Current", ("battery_current",), "{:.2f} A".format),
    ("Battery temp", ("battery_temp",), "{:.1f} °C".format),
    (
        "OBC · TTC · BMS",
        ("obc_temp", "ttc_temp", "bms_temp"),
        "{:.0f} · {:.0f} · {:.0f} °C".format,
    ),
    ("Latitude", ("gps_lat",), lambda lat: format_coordinate(lat, "N", "S")),
    ("Longitude", ("gps_long",), lambda lon: format_coordinate(lon, "E", "W")),
    ("Ground speed", ("gps_groundspeed", "gps_course"), "{:.1f} kn   {:.0f}°".format),
    ("GPS time (UTC)", ("gps_ts",), format_gps_time),
)


def fields_in(group: str) -> list[Field]:
    return [field for field in FIELDS if field.group == group]


def hemisphere_ticks(group: str) -> Callable[[float, float], str]:
    positive, negative = HEMISPHERES[group]
    return lambda value, _: format_coordinate(value, positive, negative, decimals=2)


def padded_range(arrays: list[np.ndarray]) -> tuple[float, float]:
    values = np.concatenate(arrays)
    values = values[np.isfinite(values)]
    if not len(values):
        return 0.0, 1.0
    low, high = float(values.min()), float(values.max())
    pad = 0.08 * (high - low) if high > low else 0.5
    return low - pad, high + pad


def number(value: float) -> str:
    return "–" if np.isnan(value) else f"{value:.2f}"


def style(ax: Axes) -> None:
    ax.set_facecolor(SURFACE)
    ax.grid(True, color=GRIDLINE, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.spines[["top", "right"]].set_visible(False)
    ax.spines[["left", "bottom"]].set_color(AXIS_LINE)
    ax.tick_params(colors=MUTED_TEXT, labelsize=8)


def legend(ax: Axes, handles: Sequence[Artist]) -> None:
    ax.legend(
        handles=handles,
        loc="lower right",
        bbox_to_anchor=(1, 1),
        ncols=len(handles),
        frameon=False,
        fontsize=8,
        labelcolor=SECONDARY_TEXT,
    )


def row(*items: QWidget | None) -> QBoxLayout:
    return filled(QHBoxLayout(), items)


def column(*items: QWidget | None) -> QBoxLayout:
    return filled(QVBoxLayout(), items)


def filled(layout: QBoxLayout, items: tuple[QWidget | None, ...]) -> QBoxLayout:
    for item in items:
        if item is STRETCH:
            layout.addStretch()
        else:
            layout.addWidget(item)
    return layout


def scrollable(widget: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setWidget(widget)
    return area


def caption(text: str) -> QLabel:
    label = QLabel(text.upper())
    label.setStyleSheet(f"color: {MUTED_TEXT}; font-weight: bold;")
    return label


class Canvas(FigureCanvasQTAgg):
    """A matplotlib canvas that only draws while it's on screen."""

    def redraw(self) -> None:
        if self.isVisible():
            self.draw_idle()

    def showEvent(self, event):
        super().showEvent(event)
        self.draw_idle()


class Toolbar(NavigationToolbar2QT):
    toolitems = [
        item
        for item in NavigationToolbar2QT.toolitems
        if item[0] not in ("Subplots", "Customize")
    ]


class Readout(QWidget):
    """The latest value of the key telemetry fields."""

    def __init__(self) -> None:
        super().__init__()
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setVerticalSpacing(2)
        self.labels: list[QLabel] = []
        for line, (name, _, _) in enumerate(READOUT):
            name_label = QLabel(name)
            name_label.setStyleSheet(f"color: {MUTED_TEXT};")
            value_label = QLabel("–")
            value_label.setFont(VALUE_FONT)
            grid.addWidget(name_label, line, 0)
            grid.addWidget(value_label, line, 1)
            self.labels.append(value_label)

    def show_latest(self, values: dict[str, np.ndarray]) -> None:
        for label, (_, keys, text) in zip(self.labels, READOUT):
            latest = [values[key][-1] if len(values[key]) else np.nan for key in keys]
            label.setText("–" if np.isnan(latest).any() else text(*latest))


class GroundTrack(Canvas):
    """The flight path, from launch to the latest GPS fix."""

    def __init__(self) -> None:
        super().__init__(
            Figure(figsize=(2.8, 2.0), layout="constrained", facecolor=SURFACE)
        )
        self.setFixedHeight(200)
        self.lat = self.lon = np.array([])
        self.ax = self.figure.subplots()
        color = SERIES_COLORS[0]
        (self.path,) = self.ax.plot([], [], color=color, linewidth=LINE_WIDTH)
        (self.launch,) = self.ax.plot(
            [],
            [],
            "o",
            color=SURFACE,
            markeredgecolor=color,
            markersize=7,
            label="Launch",
        )
        (self.latest,) = self.ax.plot(
            [],
            [],
            "o",
            color=color,
            markeredgecolor=SURFACE,
            markersize=9,
            label="Latest",
        )
        (self.cursor,) = self.ax.plot(
            [], [], "o", color="none", markeredgecolor=PRIMARY_TEXT, markersize=11
        )
        self.ax.margins(0.12)
        legend(self.ax, [self.launch, self.latest])
        for axis, group in ((self.ax.xaxis, "longitude"), (self.ax.yaxis, "latitude")):
            axis.set_major_locator(MaxNLocator(3))
            axis.set_major_formatter(hemisphere_ticks(group))
        style(self.ax)

    def show_path(self, lat: np.ndarray, lon: np.ndarray) -> None:
        self.lat, self.lon = lat, lon
        fixed = np.isfinite(lat) & np.isfinite(lon)
        self.path.set_data(lon[fixed], lat[fixed])
        self.launch.set_data(lon[fixed][:1], lat[fixed][:1])
        self.latest.set_data(lon[fixed][-1:], lat[fixed][-1:])
        has_fix = bool(fixed.any())
        self.ax.set_visible(has_fix)
        if has_fix:
            stretch = 1 / np.cos(np.radians(lat[fixed].mean()))
            self.ax.set_aspect(stretch, adjustable="datalim")
            self.ax.relim()
            self.ax.update_datalim(self.around(lon[fixed][0], lat[fixed][0]))
            self.ax.autoscale_view()
        self.redraw()

    @staticmethod
    def around(lon: float, lat: float) -> list[tuple[float, float]]:
        half = MIN_TRACK_HALF_SPAN_DEG
        return [(lon - half, lat - half), (lon + half, lat + half)]

    def mark(self, index: int | None) -> None:
        if index is None or np.isnan(self.lat[index]):
            self.cursor.set_data([], [])
        else:
            self.cursor.set_data([self.lon[index]], [self.lat[index]])
        self.redraw()


class TimeSeries(Canvas):
    """Stacked plots on one shared time axis, with a crosshair that follows the mouse."""

    def __init__(
        self, on_hover: Callable[[int | None], None], on_user_zoom: Callable[[], None]
    ) -> None:
        super().__init__(Figure(layout="constrained", facecolor=SURFACE))
        self.setMinimumWidth(360)
        self.on_hover = on_hover
        self.on_user_zoom = on_user_zoom
        self.navigation = Toolbar(self, None, coordinates=False)

        self.times = np.array([])
        self.values = {key: np.array([]) for key in COLUMNS}
        self.time_window: int | None = None
        self.follow = True
        self.hovered: int | None = None
        self.background: Any = None

        self.groups: list[str] = []
        self.axes: list[Axes] = []
        self.lines: dict[str, Line2D] = {}
        self.cursors: list[Line2D] = []
        self.readings: list[Text] = []
        self.context: list[Artist] = []

        self.mpl_connect("motion_notify_event", self.hover)
        self.mpl_connect("figure_leave_event", self.hover)
        self.mpl_connect("button_release_event", self.released)
        self.mpl_connect("draw_event", self.save_background)
        self.show_plots(DEFAULT_PLOTS)

    def show_plots(self, groups: Sequence[str]) -> None:
        view = self.axes[0].get_xlim() if self.axes else None
        self.groups = [group for group, _ in PLOTS if group in groups]
        self.figure.clear()
        self.lines, self.cursors, self.readings, self.context = {}, [], [], []
        self.axes = []
        if self.groups:
            grid = self.figure.subplots(len(self.groups), 1, sharex=True, squeeze=False)
            self.axes = list(grid[:, 0])
            for ax, group in zip(self.axes, self.groups):
                self.build_plot(ax, group)
            self.label_time_axis()
        self.setMinimumHeight(PLOT_HEIGHT_PX * len(self.groups))
        self.navigation.update()

        self.draw_data()
        if self.follow or view is None:
            self.follow_latest()
        else:
            self.fit(*view)
        self.redraw()

    def build_plot(self, ax: Axes, group: str) -> None:
        fields = fields_in(group)
        marks = (
            {"linestyle": "none", "marker": ".", "markersize": 3}
            if group == "course"
            else {"linewidth": LINE_WIDTH}
        )
        for color, field in zip(SERIES_COLORS, fields):
            (self.lines[field.key],) = ax.plot(
                NO_TIMES, [], color=color, label=field.short or field.label, **marks
            )

        handles: list[Artist] = [self.lines[field.key] for field in fields]
        if group in GPS_GROUPS:
            handles.append(Patch(color=GRIDLINE, label="No GPS fix"))
        if len(handles) > 1:
            legend(ax, handles)

        unit = display_unit(fields[0].key) or "units TBC"
        ax.set_title(
            f"{TITLES[group]} ({unit})", loc="left", fontsize=10, color=PRIMARY_TEXT
        )
        if group == "course":
            ax.set_ylim(0, 360)
            ax.set_yticks([0, 90, 180, 270, 360], ["N", "E", "S", "W", "N"])
        if group in HEMISPHERES:
            ax.yaxis.set_major_formatter(hemisphere_ticks(group))

        self.cursors.append(
            ax.axvline(
                np.nan,
                color=SECONDARY_TEXT,
                linewidth=0.8,
                zorder=3,
                visible=False,
                animated=True,
            )
        )
        self.readings.append(
            ax.text(
                0.01, 0.95, "", transform=ax.transAxes, visible=False, **READING_STYLE
            )
        )
        style(ax)

    def label_time_axis(self) -> None:
        locator = mdates.AutoDateLocator()
        bottom = self.axes[-1]
        bottom.xaxis.set_major_locator(locator)
        bottom.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
        bottom.set_xlabel("Time (UTC)", fontsize=9, color=SECONDARY_TEXT)
        for ax in self.axes[:-1]:
            ax.tick_params(labelbottom=True)
            ax.xaxis.get_offset_text().set_visible(False)

    def show_data(self, times: np.ndarray, values: dict[str, np.ndarray]) -> None:
        self.times, self.values = times, values
        self.draw_data()
        if self.follow:
            self.follow_latest()
        self.redraw()

    def draw_data(self) -> None:
        for key, line in self.lines.items():
            line.set_data(self.times, self.values[key])
        self.draw_context()

    def draw_context(self) -> None:
        for artist in self.context:
            artist.remove()
        self.context = []

        events = flight_events(self.times, self.values["altitude"])
        no_fix = missing_spans(self.times, self.values["gps_lat"])
        for ax, group in zip(self.axes, self.groups):
            for moment in events.values():
                self.context.append(ax.axvline(moment, color=AXIS_LINE, linewidth=1))
            if group in GPS_GROUPS:
                for start, end in no_fix:
                    self.context.append(
                        ax.axvspan(start, end, color=GRIDLINE, linewidth=0)
                    )

        if self.axes:
            top = self.axes[0]
            for name, moment in events.items():
                label = top.text(
                    moment,
                    0.97,
                    f"{name} ",
                    transform=top.get_xaxis_transform(),
                    **NOTE_STYLE,
                )
                self.context.append(label)

    def set_follow(self, follow: bool) -> None:
        self.follow = follow
        if follow:
            self.follow_latest()
            self.redraw()

    def set_window(self, seconds: int | None) -> None:
        self.time_window = seconds
        self.follow_latest()
        self.redraw()

    def follow_latest(self) -> None:
        if not len(self.times):
            return
        end = self.times[-1]
        if self.time_window is None:
            start = self.times[0]
        else:
            start = end - self.time_window / SECONDS_PER_DAY
        self.fit(start, end)

    def fit(self, start: float, end: float) -> None:
        start = min(start, end - 60 / SECONDS_PER_DAY)
        in_view = (self.times >= start) & (self.times <= end)
        for ax, group in zip(self.axes, self.groups):
            ax.set_xlim(start, end)
            if group != "course":
                visible = [
                    self.values[field.key][in_view] for field in fields_in(group)
                ]
                ax.set_ylim(*padded_range(visible))

    def hover(self, event: LocationEvent) -> None:
        x = event.xdata if event.inaxes in self.axes else None
        if x is None or not len(self.times):
            index = None
        else:
            index = int(np.abs(self.times - x).argmin())
        if index == self.hovered:
            return

        self.hovered = index
        for cursor, reading, group in zip(self.cursors, self.readings, self.groups):
            cursor.set_visible(index is not None)
            reading.set_visible(index is not None)
            if index is not None:
                cursor.set_xdata([self.times[index]] * 2)
                reading.set_text(self.reading(group, index))
        self.on_hover(index)
        if self.background is not None:
            self.restore_region(self.background)
            self.draw_crosshair()
            self.blit(self.figure.bbox)

    def save_background(self, event: DrawEvent) -> None:
        self.background = self.copy_from_bbox(self.figure.bbox)
        self.draw_crosshair()

    def draw_crosshair(self) -> None:
        for artist in self.cursors + self.readings:
            self.figure.draw_artist(artist)

    def reading(self, group: str, index: int) -> str:
        fields = fields_in(group)
        moment = mdates.num2date(self.times[index]).strftime("%H:%M:%S")
        if len(fields) == 1:
            numbers = number(self.values[fields[0].key][index])
        else:
            numbers = "   ".join(
                f"{field.short} {number(self.values[field.key][index])}"
                for field in fields
            )
        return f"{moment}   {numbers} {display_unit(fields[0].key)}".rstrip()

    def released(self, event: MouseEvent) -> None:
        if self.navigation.mode:
            self.on_user_zoom()


class PlotPicker(QWidget):
    """A checkbox for each plot; on_change receives the ticked groups."""

    def __init__(self, on_change: Callable[[list[str]], None]) -> None:
        super().__init__()
        grid = QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        self.boxes: dict[str, QCheckBox] = {}
        for i, (group, title) in enumerate(PLOTS):
            box = QCheckBox(title)
            box.setChecked(group in DEFAULT_PLOTS)
            box.toggled.connect(lambda _: on_change(self.ticked()))
            grid.addWidget(box, i // 2, i % 2)
            self.boxes[group] = box

    def ticked(self) -> list[str]:
        return [group for group, box in self.boxes.items() if box.isChecked()]


class TelemetryPage(QWidget):
    """Live readouts, a ground track and plots of the telemetry CSV."""

    def __init__(self, path: Path | str = DEFAULT_CSV_PATH) -> None:
        super().__init__()
        self.log = TelemetryLog(path)

        self.status = QLabel()
        self.readout = Readout()
        self.track = GroundTrack()
        self.follow = QCheckBox("Follow live")
        self.plots = TimeSeries(
            on_hover=self.track.mark,
            on_user_zoom=lambda: self.follow.setChecked(False),
        )
        self.follow.setChecked(True)
        self.follow.toggled.connect(self.plots.set_follow)

        self.placeholder = QLabel(f"Waiting for {self.log.path.name}…")
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setWordWrap(True)
        self.placeholder.setStyleSheet(f"color: {MUTED_TEXT};")
        self.plot_area = QStackedWidget()
        self.plot_area.addWidget(self.placeholder)
        self.plot_area.addWidget(scrollable(self.plots))

        title = QLabel("Telemetry")
        title.setFont(HEADING_FONT)
        sidebar = QWidget()
        sidebar.setLayout(
            column(
                caption("Latest"),
                self.readout,
                caption("Ground track"),
                self.track,
                caption("Plots"),
                PlotPicker(self.plots.show_plots),
                STRETCH,
            )
        )
        sidebar_area = scrollable(sidebar)
        sidebar_area.setFixedWidth(SIDEBAR_WIDTH_PX)
        sidebar_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        layout = QVBoxLayout(self)
        layout.addLayout(row(title, STRETCH, self.status))
        layout.addLayout(
            row(*self.window_buttons(), self.follow, STRETCH, self.plots.navigation)
        )
        layout.addLayout(row(sidebar_area, self.plot_area))

        timer = QTimer(self)
        timer.timeout.connect(self.refresh)
        timer.start(1000)
        self.refresh()

    def window_buttons(self) -> list[QWidget]:
        group = QButtonGroup(self)
        for name, seconds in TIME_WINDOWS.items():
            button = QPushButton(name, self)
            button.setCheckable(True)
            button.setChecked(seconds is None)
            button.clicked.connect(lambda _, seconds=seconds: self.show_window(seconds))
            group.addButton(button)
        return list(group.buttons())

    def show_window(self, seconds: int | None) -> None:
        self.plots.set_window(seconds)
        self.follow.setChecked(True)

    def refresh(self) -> None:
        try:
            changed = self.log.poll()
        except TelemetryFormatError as error:
            self.show_problem(str(error))
            return
        if changed:
            self.show_data()
        self.show_status()

    def show_data(self) -> None:
        times = mdates.date2num(self.log.times())
        values = {key: display_values(key, self.log.column(key)) for key in COLUMNS}
        self.readout.show_latest(values)
        self.track.show_path(values["gps_lat"], values["gps_long"])
        self.plots.show_data(times, values)
        self.placeholder.setText(f"Waiting for {self.log.path.name}…")
        self.plot_area.setCurrentIndex(1 if len(times) else 0)

    def show_problem(self, message: str) -> None:
        self.show_data()
        self.placeholder.setText(message)
        self.set_status("ERROR", f"Can't read {self.log.path.name}")

    def show_status(self) -> None:
        if not len(self.log):
            self.set_status("NO DATA", f"Waiting for {self.log.path.name}")
            return
        stamps = self.log.column("ts")
        age = time.time() - stamps[-1]
        interval = float(np.median(np.diff(stamps))) if len(stamps) > 1 else 0.0
        state = "LIVE" if 0 <= age <= max(STALE_AFTER_S, 3 * interval) else "STALE"
        detail = f"Last packet {format_age(age)}  ·  {len(self.log)} packets"
        skipped = self.log.skipped_rows
        if skipped:
            rows = "row" if skipped == 1 else "rows"
            detail += f"  ·  {skipped} unreadable {rows} skipped"
        self.set_status(state, detail)

    def set_status(self, state: str, detail: str) -> None:
        self.status.setText(
            f'<span style="color: {STATUS_COLORS[state]}">●</span> <b>{state}</b>'
            f'&nbsp;&nbsp;<span style="color: {SECONDARY_TEXT}">{detail}</span>'
        )
