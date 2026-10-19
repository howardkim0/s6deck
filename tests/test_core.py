import json

import pytest

from s6deck import protocol
from s6deck.actions import parse_hotkey
from s6deck.config import Action, Binding, Config, Page, Profile, from_dict, load, save, to_dict
from s6deck.controls import ControlKind, Gesture, decode
from s6deck.models import STREONOR_S6

PACKET = STREONOR_S6.packet_size


def test_command_is_report_id_plus_padded_payload():
    report = protocol.set_brightness(42, PACKET)
    assert len(report) == PACKET + 1
    assert report[:12] == b"\x00CRT\x00\x00LIG\x00\x00\x2a"
    assert set(report[12:]) == {0}


def test_image_header_and_chunks():
    image = bytes(range(256)) * 9  # 2304 bytes -> 3 chunks
    header = protocol.image_header(3, len(image), PACKET)
    assert header[1:14] == b"CRT\x00\x00BAT\x00\x00" + len(image).to_bytes(2, "big") + b"\x03"
    chunks = protocol.image_chunks(image, PACKET)
    assert len(chunks) == 3 and all(len(c) == PACKET + 1 and c[0] == 0 for c in chunks)
    assert b"".join(c[1:] for c in chunks)[:len(image)] == image


def test_image_header_rejects_oversized_image():
    with pytest.raises(ValueError):
        protocol.image_header(1, 0x10000, PACKET)


@pytest.mark.parametrize("code,state,expected", [
    (0x01, 1, (ControlKind.KEY, 0, Gesture.PRESS)),
    (0x06, 0, (ControlKind.KEY, 5, Gesture.RELEASE)),
    (0x30, 1, (ControlKind.BUTTON, 1, Gesture.PRESS)),
    (0x90, 0, (ControlKind.KNOB, 0, Gesture.ROTATE_LEFT)),
    (0x61, 0, (ControlKind.KNOB, 1, Gesture.ROTATE_RIGHT)),
    (0x35, 1, (ControlKind.KNOB, 2, Gesture.PRESS)),
])
def test_decode_s6_inputs(code, state, expected):
    event = decode(STREONOR_S6, code, state)
    assert (event.kind, event.index, event.gesture) == expected


def test_decode_unknown_code():
    assert decode(STREONOR_S6, 0x7F, 1) is None


def test_knob_binding_ids():
    assert decode(STREONOR_S6, 0x51, 0).binding_id == "knob:2:right"
    assert decode(STREONOR_S6, 0x02, 1).binding_id == "key:1"


def test_config_round_trip_drops_empty_bindings(tmp_path):
    config = Config(profiles=[Profile(name="Default", pages=[Page(bindings={
        "key:0": Binding(title="Mute", action=Action("hotkey", "cmd+shift+m")),
        "key:1": Binding(),
    })])])
    path = tmp_path / "config.json"
    save(config, path)
    stored = json.loads(path.read_text())
    assert list(stored["profiles"][0]["pages"][0]["bindings"]) == ["key:0"]
    assert load(path) == from_dict(to_dict(config))
    assert load(path).profiles[0].pages[0].bindings["key:0"].action.value == "cmd+shift+m"


def test_profile_for_app_falls_back_to_first():
    config = Config(profiles=[Profile("Default"), Profile("Browser", apps=["Safari", "Google Chrome"])])
    assert config.profile_for_app("safari").name == "Browser"
    assert config.profile_for_app("Finder").name == "Default"
    assert config.profile_for_app(None).name == "Default"


def test_parse_hotkey():
    assert parse_hotkey("Cmd+Shift+4") == (["cmd", "shift"], "4")
    assert parse_hotkey("option+return") == (["alt"], "enter")
    with pytest.raises(ValueError):
        parse_hotkey("hyper+x")


def test_change_volume_rejects_unknown_command():
    from s6deck.actions import change_volume

    with pytest.raises(ValueError):
        change_volume("louder")


def test_change_system_brightness_rejects_unknown_command():
    from s6deck.actions import change_system_brightness

    with pytest.raises(ValueError):
        change_system_brightness("max")


def test_cycle_audio_output_rejects_unknown_command():
    from s6deck.audio_output import cycle_audio_output

    with pytest.raises(ValueError):
        cycle_audio_output("first")


def test_next_audio_output_wraps_and_skips_virtual_devices():
    from s6deck.audio_output import _next_device

    devices = [("a", "Speakers"), ("cable", "CABLE Input (VB-Audio Virtual Cable)"), ("b", "Headset")]
    assert _next_device(devices, "a", 1) == ("b", "Headset")
    assert _next_device(devices, "b", 1) == ("a", "Speakers")  # wraps, skipping the cable
    assert _next_device(devices, "a", -1) == ("b", "Headset")
    assert _next_device(devices, "cable", 1) == ("a", "Speakers")  # on a virtual device: first real one
    assert _next_device([("a", "Speakers")], "a", 1) is None  # nothing else to switch to


def test_config_round_trips_soundboard_settings(tmp_path):
    config = Config(virtual_mic=True, sound_volume=45, profiles=[Profile(pages=[Page(bindings={
        "key:2": Binding(title="Stop", action=Action("stop_sounds", "")),
    })])])
    save(config, tmp_path / "c.json")
    loaded = load(tmp_path / "c.json")
    assert (loaded.virtual_mic, loaded.sound_volume) == (True, 45)
    assert loaded.profiles[0].pages[0].bindings["key:2"].action.type == "stop_sounds"


@pytest.mark.parametrize("rms_dbfs, expected_dbfs", [(-5, -20), (-30, -30)])
def test_loud_clips_are_turned_down_to_target_and_quiet_ones_left_alone(rms_dbfs, expected_dbfs):
    np = pytest.importorskip("numpy")
    from s6deck.audio import _normalization_gain

    samples = np.full((1000, 2), 10 ** (rms_dbfs / 20), dtype="float32")
    gain = _normalization_gain(samples)
    assert 20 * np.log10(np.sqrt(np.mean((samples * gain) ** 2))) == pytest.approx(expected_dbfs, abs=0.01)


def test_silent_clip_is_not_scaled():
    np = pytest.importorskip("numpy")
    from s6deck.audio import _normalization_gain

    assert _normalization_gain(np.zeros((1000, 2), dtype="float32")) == 1.0


def test_clip_buffer_keeps_most_recent_frames_across_wraparound():
    import numpy as np

    from s6deck.chat_clip import ClipBuffer

    buffer = ClipBuffer(seconds=1, rate=10)  # 10 frames
    for start in (0, 6, 12):
        buffer.append(np.arange(start, start + 6, dtype=np.int16).repeat(2).reshape(-1, 2))
    assert buffer.last(4)[:, 0].tolist() == [14, 15, 16, 17]
    assert buffer.last(100)[:, 0].tolist() == list(range(8, 18))
    buffer.append_silence(3)
    assert buffer.last(4)[:, 0].tolist() == [17, 0, 0, 0]
    buffer.clear()
    assert len(buffer.last(5)) == 0


def test_top_discord_process_is_the_one_without_a_discord_parent():
    from s6deck.chat_clip import DISCORD_PROCESS_NAMES, _top_process

    processes = {1: ("explorer.exe", 0), 10: ("discord.exe", 1), 11: ("discord.exe", 10),
                 12: ("discord.exe", 10)}
    assert _top_process(processes, DISCORD_PROCESS_NAMES) == 10
    assert _top_process({1: ("explorer.exe", 0)}, DISCORD_PROCESS_NAMES) is None


def test_microphone_choice_round_trips_and_defaults_to_system_default():
    assert from_dict({}).microphone == ""
    assert from_dict(to_dict(Config(microphone="Microphone (Arctis)"))).microphone == "Microphone (Arctis)"
