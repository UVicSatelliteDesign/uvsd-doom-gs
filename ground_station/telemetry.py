"""
Telemetry downlinked from the DOOM balloon, as logged to telemetry_data.csv.

This module has no Qt dependency, so the telemetry page, the test-data
generator and the tests can all share it.
"""

import csv
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

DEFAULT_CSV_PATH = Path(__file__).resolve().parent.parent / "telemetry_data.csv"


@dataclass(frozen=True)
class Field:
    """
    One column of the telemetry CSV.

    Attributes:
        key: Column name in the CSV header.
        label: Human-readable name for legends and readouts.
        unit: Unit of the value *as stored in the CSV*.
        group: Name of the plot the field is drawn on. Fields sharing a group
            share an axes, so they must share a unit.
    """

    key: str
    label: str
    unit: str
    group: str


# The schema from the work package, in its column order. Everything else
# (parsing, the generator, the plots) is driven from this table.
FIELDS: tuple[Field, ...] = (
    Field("ts", "Timestamp", "s", "time"),
    Field("battery_voltage", "Battery voltage", "V", "battery_voltage"),
    Field("battery_current", "Battery current", "A", "battery_current"),
    Field("battery_temp", "Battery temperature", "°C", "temperature"),
    Field("battery_charge", "Battery charge", "%", "battery_charge"),
    Field("obc_temp", "OBC temperature", "°C", "temperature"),
    Field("ttc_temp", "TTC temperature", "°C", "temperature"),
    Field("bms_temp", "BMS temperature", "°C", "temperature"),
    # Gyroscope and accelerometer units aren't specified yet.
    Field("gyroscope_x", "Gyroscope X", "", "gyroscope"),
    Field("gyroscope_y", "Gyroscope Y", "", "gyroscope"),
    Field("gyroscope_z", "Gyroscope Z", "", "gyroscope"),
    Field("accelerometer_x", "Accelerometer X", "", "accelerometer"),
    Field("accelerometer_y", "Accelerometer Y", "", "accelerometer"),
    Field("accelerometer_z", "Accelerometer Z", "", "accelerometer"),
    Field("altitude", "Altitude", "km", "altitude"),
    Field("gps_ts", "GPS time", "hhmmss.sss", "gps_time"),
    Field("gps_lat", "Latitude", "ddmm.mmmm", "latitude"),
    Field("gps_long", "Longitude", "dddmm.mmmm", "longitude"),
    Field("gps_groundspeed", "Ground speed", "kn", "groundspeed"),
    Field("gps_course", "Course", "°", "course"),
)

COLUMNS: tuple[str, ...] = tuple(field.key for field in FIELDS)
FIELDS_BY_KEY: dict[str, Field] = {field.key: field for field in FIELDS}


def nmea_to_decimal(value):
    """
    Converts an NMEA-style coordinate to decimal degrees.

    Handles both latitude (ddmm.mmmm) and longitude (dddmm.mmmm), since the
    degrees are just everything above the last two integer digits. The sign is
    preserved, so negative values come out as south / west.

    Args:
        value: A float or numpy array in [d]ddmm.mmmm format.

    Example:
        >>> nmea_to_decimal(3723.2475)
        37.387458...
    """
    degrees = np.trunc(value / 100)
    minutes = value - degrees * 100
    return degrees + minutes / 60


def gps_time_to_seconds(value):
    """
    Converts a GPS hhmmss.sss UTC time to seconds since midnight UTC.

    The value is split arithmetically rather than as a string: a time like
    00:05:12.5 arrives as the float 512.5, with no leading zeros.

    Args:
        value: A float or numpy array in hhmmss.sss format.

    Example:
        >>> gps_time_to_seconds(161229.487)  # 16:12:29.487
        58349.487
    """
    hours = value // 10_000
    minutes = (value % 10_000) // 100
    seconds = value % 100
    return hours * 3600 + minutes * 60 + seconds


def format_gps_time(value: float) -> str:
    """
    Formats a GPS hhmmss.sss UTC time as "HH:MM:SS.sss".

    Example:
        >>> format_gps_time(161229.487)
        '16:12:29.487'
    """
    millis = round(gps_time_to_seconds(value) * 1000)
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    seconds, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


class TelemetryFormatError(ValueError):
    """Raised when the telemetry CSV header doesn't match the schema."""


class TelemetryLog:
    """
    Telemetry columns read incrementally from a CSV that another process appends to.

    Call poll() periodically; each call reads only the bytes added since the
    previous one. Columns are located by header name, so their order in the
    file doesn't matter and extra columns are ignored.

    Robustness rules:
      - A final line with no newline may still be being written, so it is
        held back until the file stops growing for one poll.
      - Rows that can't be parsed are skipped and counted in skipped_rows.
        Empty values become NaN, except the timestamp, which is required.
      - If the file shrinks or is replaced, it's re-read from the start.

    Example:
        >>> log = TelemetryLog("telemetry_data.csv")
        >>> log.poll()
        True
        >>> log.column("altitude")[-1]
        23.41
    """

    def __init__(self, path: Path | str = DEFAULT_CSV_PATH):
        self.path = Path(path)
        self._reset()

    def _reset(self) -> None:
        self._offset = 0  # bytes of the file consumed so far
        self._last_size = -1  # file size seen by the previous poll
        self._inode: int | None = None
        # Position in the CSV of each COLUMNS entry; None until the header is read
        self._column_index: list[int] | None = None
        self._data = np.empty((0, len(COLUMNS)))
        self.skipped_rows = 0

    def __len__(self) -> int:
        return len(self._data)

    def column(self, key: str) -> np.ndarray:
        """Returns every value of one column, as float64 (NaN where missing)."""
        return self._data[:, COLUMNS.index(key)]

    def times(self) -> np.ndarray:
        """Returns the timestamps as UTC numpy datetime64 values."""
        return self.column("ts").astype(np.int64).astype("datetime64[s]")

    def latest(self) -> dict[str, float] | None:
        """Returns the most recent row as {column: value}, or None if empty."""
        if not len(self):
            return None
        return dict(zip(COLUMNS, self._data[-1].tolist()))

    def poll(self) -> bool:
        """
        Reads any rows appended since the last poll.

        Returns:
            bool: True if the data changed (rows added, or the file was reset).

        Raises:
            TelemetryFormatError: If the header is missing schema columns.
        """
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            had_data = len(self) > 0
            self._reset()
            return had_data

        reset = stat.st_size < self._offset or (
            self._inode is not None and stat.st_ino != self._inode
        )
        if reset:
            self._reset()
        self._inode = stat.st_ino

        with open(self.path, "rb") as f:
            f.seek(self._offset)
            chunk = f.read(stat.st_size - self._offset)

        if not chunk.endswith(b"\n") and stat.st_size != self._last_size:
            # The writer may be partway through a row; leave it for next time.
            chunk = chunk[: chunk.rfind(b"\n") + 1]
        self._last_size = stat.st_size
        self._offset += len(chunk)

        rows = []
        lines = chunk.decode("utf-8", errors="replace").splitlines()
        for values in csv.reader(lines):
            if not values or not any(v.strip() for v in values):
                continue
            if self._column_index is None:
                self._read_header(values)
                continue
            row = self._parse_row(values, self._column_index)
            if row is None:
                self.skipped_rows += 1
            else:
                rows.append(row)

        if rows:
            self._data = np.vstack([self._data, rows])
        return reset or bool(rows)

    def _read_header(self, names: list[str]) -> None:
        names = [name.strip().lstrip("\ufeff") for name in names]
        missing = [key for key in COLUMNS if key not in names]
        if missing:
            # Start over next poll, so the error repeats until the file is fixed
            self._reset()
            raise TelemetryFormatError(
                f"{self.path.name} is missing columns: {', '.join(missing)}"
            )
        self._column_index = [names.index(key) for key in COLUMNS]

    def _parse_row(
        self, values: list[str], column_index: list[int]
    ) -> list[float] | None:
        try:
            row = [
                float(text) if (text := values[i].strip()) else math.nan
                for i in column_index
            ]
        except (ValueError, IndexError):
            return None
        if math.isnan(row[0]):  # no timestamp
            return None
        return row
