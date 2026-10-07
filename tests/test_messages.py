"""
Tests for ground_station/messages.py: the keystroke packets the ground station
sends up to the DOOM payload.

Wire format under test, from the Spacecraft Command Format spec:
  - DOOMKeystroke      -> 4 bytes: [modifier bitmask][key 1][key 2][key 3]
  - DOOMKeystrokeList  -> 1 size byte N, then N * 4 keystroke bytes (N <= 60)
  - to_packets()       -> 0x02 [batch][seq][seq count] + the list bytes, one
                          per 60 keystrokes, then 0x04 [batch] (EOF)

Only QtCore is used (for Qt.Key constants), so these run headless with no
display or QApplication.
"""

import struct

import pytest
from PyQt6.QtCore import Qt

from hid import HID_TO_DESCRIPTION, QT_TO_HID, QT_TO_HID_MODIFIERS
from messages import MAX_KEYSTROKES_PER_PACKET, DOOMKeystroke, DOOMKeystrokeList

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


def test_arrow_key_labels_match_their_hid_codes():
    labels = {name: HID_TO_DESCRIPTION[QT_TO_HID[k]] for name, k in [
        ("right", Qt.Key.Key_Right), ("left", Qt.Key.Key_Left), ("down", Qt.Key.Key_Down), ("up", Qt.Key.Key_Up)]}
    assert labels == {"right": "\u2192", "left": "\u2190", "down": "\u2193", "up": "\u2191"}


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


def test_60_keystrokes_is_the_largest_serialisable_list():
    assert MAX_KEYSTROKES_PER_PACKET == 60
    assert bytes(DOOMKeystrokeList([key(0x04)] * 60))[0] == 60
    with pytest.raises(ValueError):
        bytes(DOOMKeystrokeList([key(0x04)] * 61))


@pytest.mark.parametrize(("count", "chunks"), [(0, [0]), (60, [60]), (61, [60, 1]), (150, [60, 60, 30])])
def test_split_to_serialise_chunks_at_60(count, chunks):
    """Every chunk must serialise, and together they keep every keystroke in order."""
    original = DOOMKeystrokeList([key(i % 200 + 1) for i in range(count)])
    parts = original.split_to_serialise()
    assert [len(p) for p in parts] == chunks
    for p in parts:
        bytes(p)  # must not raise
    assert [k.keys for p in parts for k in p] == [k.keys for k in original]


# ── Uplink packets (DOOM Command: Keyboard 0x02 + EOF 0x04) ─────────────────


def test_spec_example_a_then_b():
    """The spec's example: "A" then "b" -> 0x02040000, 0x00050000."""
    a = DOOMKeystroke.from_qt_keys([Qt.Key.Key_Shift, Qt.Key.Key_A])
    b = DOOMKeystroke.from_qt_keys([Qt.Key.Key_B])
    assert bytes(a) + bytes(b) == bytes.fromhex("02040000 00050000")


def test_spec_example_ctrl_shift_a_f_up():
    """The spec's example: ctrl-shift-A-F-Up Arrow -> 0x03040952."""
    ks = DOOMKeystroke.from_qt_keys(
        [Qt.Key.Key_Control, Qt.Key.Key_Shift, Qt.Key.Key_A, Qt.Key.Key_F, Qt.Key.Key_Up]
    )
    assert bytes(ks) == bytes.fromhex("03040952")


def test_short_recording_is_one_keyboard_packet_then_eof():
    packets = DOOMKeystrokeList([key(0x04), key(0x05)]).to_packets(batch=7)
    assert packets == [
        bytes([0x02, 7, 0, 1, 2]) + bytes.fromhex("00040000 00050000"),
        bytes([0x04, 7]),
    ]


def test_long_recording_is_split_with_sequence_numbers():
    packets = DOOMKeystrokeList([key(0x04)] * 150).to_packets(batch=3)
    *keyboard, eof = packets
    # [cmd][batch][seq][seq count][size]
    assert [p[:5] for p in keyboard] == [
        bytes([0x02, 3, 0, 3, 60]),
        bytes([0x02, 3, 1, 3, 60]),
        bytes([0x02, 3, 2, 3, 30]),
    ]
    assert eof == bytes([0x04, 3])


def test_full_packet_fits_the_orca_payload_limit():
    """ORCA forwards at most 246 bytes (command byte + 245-byte body)."""
    packets = DOOMKeystrokeList([key(0x04)] * 60).to_packets(batch=0)
    assert len(packets[0]) == 1 + 4 + 60 * 4 == 245
    assert len(packets[0]) <= 246


def test_batch_must_fit_in_a_byte():
    with pytest.raises(ValueError):
        DOOMKeystrokeList([key(0x04)]).to_packets(batch=256)


def test_more_than_255_packets_is_rejected():
    with pytest.raises(ValueError):
        DOOMKeystrokeList([key(0x04)] * (255 * 60 + 1)).to_packets(batch=0)
