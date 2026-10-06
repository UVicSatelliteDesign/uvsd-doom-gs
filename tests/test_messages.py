"""
Tests for ground_station/messages.py: the keystroke packets the ground station
sends up to the DOOM payload.

Wire format under test (see the docstrings in messages.py):
  - DOOMKeystroke      -> 4 bytes: [modifier bitmask][key 1][key 2][key 3]
  - DOOMKeystrokeList  -> 1 count byte N, then N * 4 keystroke bytes (N <= 255)

The Spacecraft Command Format spec ("DOOM Command Body: Keyboard", 0x02) is
stricter: an ORCA-forwarded body is at most 245 bytes, so a packet holds at
most 60 keystrokes (2.0 s), behind a batch # / sequence # / sequence count /
size header. messages.py doesn't do that yet; the xfail(strict) tests below
pin the spec limit and will start failing (XPASS) once it is implemented.

Only QtCore is used (for Qt.Key constants), so these run headless with no
display or QApplication.
"""

import struct

import pytest
from PyQt6.QtCore import Qt

from hid import QT_TO_HID, QT_TO_HID_MODIFIERS
from messages import DOOMKeystroke, DOOMKeystrokeList

IDLE = DOOMKeystroke(0, (0, 0, 0))


def key(hid_code: int) -> DOOMKeystroke:
    """A keystroke with one plain key held."""
    return DOOMKeystroke(0, (hid_code, 0, 0))


# ── DOOMKeystroke ────────────────────────────────────────────────────────────


def test_keystroke_serialises_to_four_bytes_in_order():
    """Modifier byte first, then the three key slots in order."""
    assert bytes(DOOMKeystroke(0x02, (0x04, 0x05, 0x00))) == b"\x02\x04\x05\x00"


def test_keystroke_rejects_values_that_do_not_fit_in_a_byte():
    """Every field is one unsigned byte on the wire."""
    with pytest.raises(struct.error):
        bytes(DOOMKeystroke(0x100, (0, 0, 0)))


def test_idle_only_when_no_modifier_and_no_keys():
    assert IDLE.is_idle()
    assert not DOOMKeystroke(0x01, (0, 0, 0)).is_idle()
    assert not DOOMKeystroke(0, (0, 0, 0x04)).is_idle()


def test_from_qt_keys_maps_letter_and_modifier():
    """Shift + A -> left-shift bit (0x02) and HID usage 0x04 (the docstring example)."""
    ks = DOOMKeystroke.from_qt_keys([Qt.Key.Key_A, Qt.Key.Key_Shift])
    assert ks.modifiers == 0x02
    assert ks.keys == (0x04, 0x00, 0x00)


def test_from_qt_keys_combines_multiple_modifiers():
    ks = DOOMKeystroke.from_qt_keys([Qt.Key.Key_Control, Qt.Key.Key_Shift, Qt.Key.Key_Alt])
    expected = (
        QT_TO_HID_MODIFIERS[Qt.Key.Key_Control]
        | QT_TO_HID_MODIFIERS[Qt.Key.Key_Shift]
        | QT_TO_HID_MODIFIERS[Qt.Key.Key_Alt]
    )
    assert ks.modifiers == expected
    assert ks.keys == (0, 0, 0)


def test_from_qt_keys_keeps_only_first_three_keys():
    """HID boot-style reports carry 3 key slots here; extra keys are dropped."""
    qt_keys = [Qt.Key.Key_W, Qt.Key.Key_A, Qt.Key.Key_S, Qt.Key.Key_D]
    ks = DOOMKeystroke.from_qt_keys(qt_keys)
    assert ks.keys == tuple(QT_TO_HID[k] for k in qt_keys[:3])


def test_from_qt_keys_ignores_unmapped_keys():
    """A key with no HID mapping must not take up a slot."""
    unmapped = next(k for k in Qt.Key if k not in QT_TO_HID and k not in QT_TO_HID_MODIFIERS)
    ks = DOOMKeystroke.from_qt_keys([unmapped, Qt.Key.Key_A])
    assert ks.keys == (QT_TO_HID[Qt.Key.Key_A], 0, 0)


def test_from_qt_keys_empty_is_idle():
    assert DOOMKeystroke.from_qt_keys([]).is_idle()


def test_hid_mappings_fit_in_a_byte():
    """Every mapped key code and modifier bit must fit the 1-byte wire fields."""
    assert all(0 < code <= 0xFF for code in QT_TO_HID.values())
    assert all(0 < bit <= 0xFF for bit in QT_TO_HID_MODIFIERS.values())


# ── DOOMKeystrokeList ────────────────────────────────────────────────────────


def test_empty_list_serialises_to_zero_count():
    assert bytes(DOOMKeystrokeList()) == b"\x00"


def test_list_serialises_count_then_keystrokes():
    ks_list = DOOMKeystrokeList([key(0x04), DOOMKeystroke(0x02, (0x05, 0x06, 0x07))])
    assert bytes(ks_list) == b"\x02" + b"\x00\x04\x00\x00" + b"\x02\x05\x06\x07"


def test_size_in_bytes_excludes_count_header():
    ks_list = DOOMKeystrokeList([key(0x04)] * 3)
    assert ks_list.size_in_bytes == 12
    assert len(bytes(ks_list)) == ks_list.size_in_bytes + 1


def test_list_behaves_like_a_mutable_sequence():
    ks_list = DOOMKeystrokeList()
    ks_list.append(key(0x04))
    ks_list.insert(0, key(0x05))
    assert [k.keys[0] for k in ks_list] == [0x05, 0x04]
    ks_list[1] = key(0x06)
    del ks_list[0]
    assert len(ks_list) == 1 and ks_list[0].keys[0] == 0x06


def test_remove_trailing_idles_keeps_inner_idles():
    """Only idles at the end are dropped; an idle between presses is a real key release."""
    ks_list = DOOMKeystrokeList([key(0x04), IDLE, key(0x05), IDLE, IDLE])
    ks_list.remove_trailing_idles()
    assert len(ks_list) == 3
    assert ks_list[1].is_idle()


def test_remove_trailing_idles_on_all_idle_list_empties_it():
    ks_list = DOOMKeystrokeList([IDLE, IDLE])
    ks_list.remove_trailing_idles()
    assert len(ks_list) == 0


def test_count_byte_caps_the_current_format_at_255():
    """Current code only; the spec limit is 60 (see the xfail tests below)."""
    assert bytes(DOOMKeystrokeList([key(0x04)] * 255))[0] == 255
    with pytest.raises(ValueError):
        bytes(DOOMKeystrokeList([key(0x04)] * 256))


@pytest.mark.parametrize(("count", "chunks"), [(0, [0]), (255, [255]), (256, [255, 1]), (600, [255, 255, 90])])
def test_split_to_serialise_chunks_at_255(count, chunks):
    """Every chunk must serialise, and together they keep every keystroke in order."""
    original = DOOMKeystrokeList([key(i % 200 + 1) for i in range(count)])
    parts = original.split_to_serialise()
    assert [len(p) for p in parts] == chunks
    for p in parts:
        bytes(p)  # must not raise
    assert [k.keys for p in parts for k in p] == [k.keys for k in original]


# ── Spacecraft Command Format spec (not implemented yet) ────────────────────

SPEC_MAX_KEYSTROKES = 60  # 245-byte ORCA body: 1 cmd + 4 header + 60 * 4


@pytest.mark.xfail(strict=True, reason="spec caps a keyboard packet at 60 entries; code allows 255")
def test_spec_more_than_60_keystrokes_is_rejected():
    with pytest.raises(ValueError):
        bytes(DOOMKeystrokeList([key(0x04)] * (SPEC_MAX_KEYSTROKES + 1)))


@pytest.mark.xfail(strict=True, reason="spec caps a keyboard packet at 60 entries; code splits at 255")
def test_spec_split_to_serialise_chunks_at_60():
    parts = DOOMKeystrokeList([key(0x04)] * 150).split_to_serialise()
    assert [len(p) for p in parts] == [60, 60, 30]
