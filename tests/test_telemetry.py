"""
Tests for ground_station/telemetry.py: the telemetry CSV schema, unit
conversions, and the incremental CSV reader.

No Qt is involved, so these run headless.
"""

import numpy as np
import pytest

from telemetry import (
    COLUMNS,
    FIELDS,
    TelemetryFormatError,
    TelemetryLog,
    format_gps_time,
    gps_time_to_seconds,
    nmea_to_decimal,
)

HEADER = ",".join(COLUMNS) + "\n"


def row(ts: int, **values: float) -> str:
    """A CSV line with the given timestamp; unspecified columns are 0."""
    cells = [str(ts)] + [str(values.get(key, 0)) for key in COLUMNS[1:]]
    return ",".join(cells) + "\n"


@pytest.fixture
def csv_path(tmp_path):
    return tmp_path / "telemetry_data.csv"


def append(path, text: str) -> None:
    with open(path, "a") as f:
        f.write(text)


# ── Schema ───────────────────────────────────────────────────────────────────


def test_columns_match_the_work_package_schema_in_order():
    assert COLUMNS == (
        "ts",
        "battery_voltage", "battery_current", "battery_temp", "battery_charge",
        "obc_temp", "ttc_temp", "bms_temp",
        "gyroscope_x", "gyroscope_y", "gyroscope_z",
        "accelerometer_x", "accelerometer_y", "accelerometer_z",
        "altitude",
        "gps_ts", "gps_lat", "gps_long", "gps_groundspeed", "gps_course",
    )  # fmt: skip


def test_fields_drawn_on_the_same_plot_share_a_unit():
    units_by_group: dict[str, set[str]] = {}
    for field in FIELDS:
        units_by_group.setdefault(field.group, set()).add(field.unit)
    assert all(len(units) == 1 for units in units_by_group.values()), units_by_group


# ── Conversions ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("nmea", "degrees"),
    [
        (3723.2475, 37 + 23.2475 / 60),  # latitude example from the work package
        (12158.3416, 121 + 58.3416 / 60),  # longitude example (three degree digits)
        (-12318.7, -(123 + 18.7 / 60)),  # negative = west / south
        (0.0, 0.0),
    ],
)
def test_nmea_to_decimal(nmea, degrees):
    assert nmea_to_decimal(nmea) == pytest.approx(degrees)


def test_nmea_to_decimal_works_on_arrays():
    result = nmea_to_decimal(np.array([3723.2475, 4827.8]))
    assert result == pytest.approx([37.387458, 48.463333], abs=1e-6)


@pytest.mark.parametrize(
    ("gps_ts", "seconds"),
    [
        (161229.487, 16 * 3600 + 12 * 60 + 29.487),  # work package example
        (512.5, 5 * 60 + 12.5),  # 00:05:12.5 has no leading zeros as a float
        (0.0, 0.0),
        (235959.999, 86399.999),
    ],
)
def test_gps_time_to_seconds(gps_ts, seconds):
    assert gps_time_to_seconds(gps_ts) == pytest.approx(seconds)


@pytest.mark.parametrize(
    ("gps_ts", "text"),
    [(161229.487, "16:12:29.487"), (512.5, "00:05:12.500"), (0.0, "00:00:00.000")],
)
def test_format_gps_time(gps_ts, text):
    assert format_gps_time(gps_ts) == text


# ── TelemetryLog ─────────────────────────────────────────────────────────────


def test_missing_file_is_empty_not_an_error(csv_path):
    log = TelemetryLog(csv_path)
    assert log.poll() is False
    assert len(log) == 0
    assert log.latest() is None


def test_reads_rows_by_column_name(csv_path):
    append(csv_path, HEADER + row(1000, altitude=1.5) + row(1001, altitude=2.5))
    log = TelemetryLog(csv_path)
    assert log.poll() is True
    assert len(log) == 2
    assert log.column("altitude").tolist() == [1.5, 2.5]
    assert log.latest()["ts"] == 1001


def test_column_order_in_file_does_not_matter_and_extras_are_ignored(csv_path):
    columns = ["extra", *reversed(COLUMNS)]
    values = ["99", *(str(i) for i in reversed(range(len(COLUMNS))))]
    append(csv_path, ",".join(columns) + "\n" + ",".join(values) + "\n")
    log = TelemetryLog(csv_path)
    log.poll()
    assert [log.column(key)[0] for key in COLUMNS] == list(range(len(COLUMNS)))


def test_header_missing_columns_raises_naming_them(csv_path):
    header = ",".join(c for c in COLUMNS if c not in ("altitude", "gps_course"))
    append(csv_path, header + "\n")
    with pytest.raises(TelemetryFormatError, match="altitude, gps_course"):
        TelemetryLog(csv_path).poll()


def test_header_error_repeats_on_every_poll(csv_path):
    """
    The error must not go quiet after the first poll (the page would just show
    no data), and rows that arrive later must not be mistaken for a header.
    """
    append(csv_path, "ts,altitude\n" + "1000,1.0\n")
    log = TelemetryLog(csv_path)
    errors = []
    for new_rows in ("", "", "1001,2.0\n"):
        append(csv_path, new_rows)
        with pytest.raises(TelemetryFormatError) as error:
            log.poll()
        errors.append(str(error.value))
    assert len(set(errors)) == 1


def test_header_with_byte_order_mark_is_accepted(csv_path):
    """Excel saves UTF-8 CSVs with a BOM, which would otherwise corrupt the name "ts"."""
    csv_path.write_text("\ufeff" + HEADER + row(1000), encoding="utf-8")
    log = TelemetryLog(csv_path)
    log.poll()
    assert log.column("ts").tolist() == [1000]


def test_poll_only_adds_new_rows(csv_path):
    append(csv_path, HEADER + row(1000, altitude=1.0))
    log = TelemetryLog(csv_path)
    log.poll()
    assert log.poll() is False  # nothing new

    append(csv_path, row(1001, altitude=2.0) + row(1002, altitude=3.0))
    assert log.poll() is True
    assert log.column("ts").tolist() == [1000, 1001, 1002]
    assert log.column("altitude").tolist() == [1.0, 2.0, 3.0]


def test_partial_line_is_held_back_until_complete(csv_path):
    """A row the logger is still writing must not be parsed half-finished."""
    full = row(1001, altitude=2.0)
    append(csv_path, HEADER + row(1000) + full[:10])
    log = TelemetryLog(csv_path)
    log.poll()
    assert len(log) == 1

    append(csv_path, full[10:])
    log.poll()
    assert len(log) == 2
    assert log.column("altitude")[-1] == 2.0


def test_unterminated_last_line_is_read_once_the_file_stops_growing(csv_path):
    append(csv_path, HEADER + row(1000) + row(1001).rstrip("\n"))
    log = TelemetryLog(csv_path)
    log.poll()
    assert len(log) == 1  # might still be mid-write
    log.poll()
    assert len(log) == 2  # size unchanged, so the line is complete


def test_bad_rows_are_skipped_and_counted(csv_path):
    append(
        csv_path,
        HEADER
        + row(1000)
        + "1001,not-a-number" + ",0" * (len(COLUMNS) - 2) + "\n"
        + "1002,1.0,2.0\n"  # too few columns
        + row(1003),
    )  # fmt: skip
    log = TelemetryLog(csv_path)
    log.poll()
    assert log.column("ts").tolist() == [1000, 1003]
    assert log.skipped_rows == 2


def test_empty_values_become_nan_but_a_timestamp_is_required(csv_path):
    gps_lat = COLUMNS.index("gps_lat")
    no_fix = row(1000).split(",")
    no_fix[gps_lat] = ""
    no_ts = row(1001).split(",")
    no_ts[0] = ""
    append(csv_path, HEADER + ",".join(no_fix) + ",".join(no_ts))
    log = TelemetryLog(csv_path)
    log.poll()
    assert len(log) == 1
    assert np.isnan(log.column("gps_lat")[0])
    assert log.skipped_rows == 1


def test_truncated_file_is_reread_from_the_start(csv_path):
    append(csv_path, HEADER + row(1000) + row(1001) + row(1002))
    log = TelemetryLog(csv_path)
    log.poll()

    csv_path.write_text(HEADER + row(2000))
    assert log.poll() is True
    assert log.column("ts").tolist() == [2000]


def test_deleted_file_clears_the_log(csv_path):
    append(csv_path, HEADER + row(1000))
    log = TelemetryLog(csv_path)
    log.poll()

    csv_path.unlink()
    assert log.poll() is True
    assert len(log) == 0


def test_times_are_utc_datetimes(csv_path):
    append(csv_path, HEADER + row(1_759_500_000))
    log = TelemetryLog(csv_path)
    log.poll()
    assert log.times()[0] == np.datetime64("2025-10-03T14:00:00")
