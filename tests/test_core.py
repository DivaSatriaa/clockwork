import json

from hzctl import core

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
            },
            "params": {"Format": [{"mediaType": "audio", "format": "S32LE", "rate": 48000}]},
        },
    },
    {"type": "PipeWire:Interface:Node", "id": 71, "info": {"props": {"media.class": "Video/Source", "node.name": "cam"}}},
    {"type": "PipeWire:Interface:Node", "id": 58, "info": {"props": {"media.class": "Audio/Source", "node.name": "mic"}}},
]

META = """Found "settings" metadata 32
update: id:0 key:'clock.rate' value:'48000' type:''
update: id:0 key:'clock.allowed-rates' value:'[ 44100 48000 ]' type:''
update: id:0 key:'clock.force-rate' value:'0' type:''
"""


def test_parse_nodes_filters_and_formats():
    nodes = core.parse_nodes(DUMP)
    assert [n.id for n in nodes] == [45, 58]  # sinks first, video dropped
    assert nodes[0].active_rate == 48000 and nodes[0].active_format == "S32LE"
    assert nodes[0].kind == "sink" and nodes[1].kind == "source"


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


def test_set_device_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(core, "STATE_FILE", tmp_path / "state/devices.json")
    monkeypatch.setattr(core, "WP_CONF", tmp_path / "wp/51-hzctl.conf")
    monkeypatch.setattr(core, "BACKUP_DIR", tmp_path / "bak")
    core.set_device("dev", rate=44100)
    assert json.loads(core.STATE_FILE.read_text()) == {"dev": {"rate": 44100}}
    assert core.WP_CONF.exists()
    core.set_device("dev", rate=0)
    assert core.load_devices() == {} and not core.WP_CONF.exists()
