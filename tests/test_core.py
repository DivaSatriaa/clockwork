import json

import pytest

from clockwork import core

DUMP = [
    {"type": "PipeWire:Interface:Core", "id": 0},
    {
        "type": "PipeWire:Interface:Node",
        "id": 45,
        "info": {
            "props": {
                "media.class": "Audio/Sink",
                "node.name": "alsa_output.pci-0000_04_00.6.analog-stereo",
                "node.description": "Ryzen HD Audio Controller Analog Stereo",
                "device.api": "alsa",
            },
            "params": {
                "Format": [{"mediaType": "audio", "format": "S32LE", "rate": 48000}],
                "EnumFormat": [{
                    "mediaType": "audio", "mediaSubtype": "raw",
                    "format": {"default": "S32LE", "alt1": "S32LE", "alt2": "S16LE", "alt3": "U8"},
                    "rate": {"default": 48000, "min": 44100, "max": 96000},
                }],
            },
        },
    },
    {"type": "PipeWire:Interface:Node", "id": 71, "info": {"props": {"media.class": "Video/Source", "node.name": "cam"}}},
    {"type": "PipeWire:Interface:Node", "id": 58, "info": {"props": {"media.class": "Audio/Source", "node.name": "mic"}}},
    {
        "type": "PipeWire:Interface:Node",
        "id": 80,
        "info": {"props": {"media.class": "Audio/Sink", "node.name": "bluez_output.AA_BB.1", "device.api": "bluez5"}},
    },
]

META = """Found "settings" metadata 32
update: id:0 key:'clock.rate' value:'48000' type:''
update: id:0 key:'clock.allowed-rates' value:'[ 44100 48000 ]' type:''
update: id:0 key:'clock.force-rate' value:'0' type:''
"""


def test_parse_nodes_filters_and_formats():
    nodes = core.parse_nodes(DUMP)
    assert [n.id for n in nodes] == [45, 80, 58]  # sinks first, video dropped
    assert nodes[0].active_rate == 48000 and nodes[0].active_format == "S32LE"
    assert nodes[0].kind == "sink" and nodes[2].kind == "source"


def test_parse_settings_and_rates():
    s = core.parse_settings(META)
    assert s["clock.rate"] == "48000"
    assert core.parse_rate_list(s["clock.allowed-rates"]) == [44100, 48000]


def test_render_global_conf_includes_default_in_allowed():
    out = core.render_global_conf(96000, [44100])
    assert "default.clock.rate          = 96000" in out
    assert "[ 44100 96000 ]" in out


def test_render_device_conf():
    out = core.render_device_conf({"alsa_output.x": {"rate": 96000, "format": "S24LE"}})
    assert 'node.name = "alsa_output.x"' in out
    assert "audio.rate = 96000" in out and 'audio.format = "S24LE"' in out


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(core, "STATE_FILE", tmp_path / "state/devices.json")
    monkeypatch.setattr(core, "WP_CONF", tmp_path / "wp/51-clockwork.conf")
    monkeypatch.setattr(core, "PW_CONF", tmp_path / "pw/10-clockwork.conf")
    monkeypatch.setattr(core, "BACKUP_DIR", tmp_path / "bak")
    return tmp_path


def test_set_device_roundtrip(paths):
    core.set_device("dev", rate=44100)
    assert json.loads(core.STATE_FILE.read_text()) == {"dev": {"rate": 44100}}
    assert core.WP_CONF.exists()
    core.set_device("dev", rate=0)
    assert core.load_devices() == {} and not core.WP_CONF.exists()


def test_set_device_keeps_other_field(paths):
    core.set_device("dev", rate=96000, fmt="S24LE")
    core.set_device("dev", rate=48000)
    assert core.load_devices() == {"dev": {"rate": 48000, "format": "S24LE"}}


def test_enum_formats_and_configurable():
    sink, bt, mic = core.parse_nodes(DUMP)
    assert sink.supported_rates == [44100, 48000, 88200, 96000]
    assert sink.supported_formats == ["S32LE", "S16LE"]  # U8 is not something we offer
    assert sink.configurable and not bt.configurable
    assert mic.supported_rates == [] and mic.configurable is False  # no api, no alsa_ prefix


def test_quality_labels_match_windows():
    assert core.quality_label(44100, 16) == "16 bit, 44100 Hz (CD Quality)"
    assert core.quality_label(48000, 16) == "16 bit, 48000 Hz (DVD Quality)"
    assert core.quality_label(48000, 24) == "24 bit, 48000 Hz (Studio Quality)"
    assert core.format_bits("S24_32LE") == 24 and core.format_bits("F32P") == 32


def test_parse_defaults():
    text = """update: id:0 key:'default.audio.sink' value:'{"name":"alsa_output.x"}' type:'Spa:String:JSON'
update: id:0 key:'default.audio.source' value:'{"name":"alsa_input.y"}' type:'Spa:String:JSON'
update: id:0 key:'default.configured.audio.sink' value:'{"name":"old"}' type:'Spa:String:JSON'
"""
    assert core.parse_defaults(text) == {"sink": "alsa_output.x", "source": "alsa_input.y"}


@pytest.mark.parametrize("bad", ['evil" } ]\nmonitor.alsa.rules = [', "", "a\\b"])
def test_node_name_cannot_break_config(paths, bad):
    with pytest.raises(core.ClockworkError):
        core.set_device(bad, rate=48000)
    assert not core.WP_CONF.exists()


def test_rate_validation(paths):
    with pytest.raises(core.ClockworkError):
        core.write_global(48000, [44100, 5])
    assert not core.PW_CONF.exists()


def test_write_global_backup_and_readback(paths):
    core.write_global(48000, [44100])
    core.write_global(96000, [44100, 48000])
    core.write_global(44100, [])
    assert core.saved_global() == {"rate": 44100, "allowed": [44100]}
    assert len(list(core.BACKUP_DIR.iterdir())) == 2  # same-second backups don't overwrite each other
