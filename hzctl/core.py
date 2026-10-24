"""hzctl core: talk to PipeWire/WirePlumber via their CLI tools.

Everything here shells out to `pw-dump`, `pw-metadata` and `systemctl --user`,
so there are no compiled dependencies. Config is written as drop-in files so
we never touch the system-wide defaults.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

# Windows-style labels for the common rates.
PRESETS: dict[int, str] = {
    44100: "CD quality (44.1 kHz)",
    48000: "DVD quality (48 kHz)",
    88200: "88.2 kHz",
    96000: "Studio quality (96 kHz)",
    192000: "Studio master (192 kHz)",
}
SUPPORTED_FORMATS = ["S16LE", "S24LE", "S32LE", "F32LE"]

HOME = Path.home()
PW_CONF = HOME / ".config/pipewire/pipewire.conf.d/10-hzctl.conf"
WP_CONF = HOME / ".config/wireplumber/wireplumber.conf.d/51-hzctl.conf"
STATE_DIR = HOME / ".config/hzctl"
STATE_FILE = STATE_DIR / "devices.json"
BACKUP_DIR = HOME / ".local/share/hzctl/backups"

SERVICES = ["pipewire", "pipewire-pulse", "wireplumber"]


class HzctlError(RuntimeError):
    pass


def run(cmd: list[str], timeout: int = 15) -> str:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        raise HzctlError(f"Command not found: {cmd[0]} (is PipeWire installed?)") from e
    except subprocess.TimeoutExpired as e:
        raise HzctlError(f"Timed out: {' '.join(cmd)}") from e
    if res.returncode != 0:
        raise HzctlError(res.stderr.strip() or f"{cmd[0]} exited with {res.returncode}")
    return res.stdout


# ---------------------------------------------------------------- devices ---

@dataclass
class AudioNode:
    id: int
    name: str
    description: str
    media_class: str
    active_rate: int | None = None
    active_format: str | None = None
    props: dict = field(default_factory=dict, repr=False)

    @property
    def kind(self) -> str:
        return "sink" if self.media_class == "Audio/Sink" else "source"


def _active_format(info: dict) -> tuple[int | None, str | None]:
    """Best effort: pull negotiated rate/format out of pw-dump params."""
    params = info.get("params") or {}
    for key in ("Format", "PortConfig"):
        for p in params.get(key) or []:
            fmt = p.get("format", p) if key == "PortConfig" else p
            if not isinstance(fmt, dict):
                continue
            rate, sfmt = fmt.get("rate"), fmt.get("format")
            if rate and sfmt:
                return rate, sfmt
    return None, None


def parse_nodes(dump: list[dict], kinds: tuple[str, ...] = ("Audio/Sink", "Audio/Source")) -> list[AudioNode]:
    nodes = []
    for obj in dump:
        if obj.get("type") != "PipeWire:Interface:Node":
            continue
        info = obj.get("info") or {}
        props = info.get("props") or {}
        mc = props.get("media.class")
        if mc not in kinds:
            continue
        rate, sfmt = _active_format(info)
        nodes.append(
            AudioNode(
                id=obj.get("id", -1),
                name=props.get("node.name", "?"),
                description=props.get("node.description") or props.get("node.nick") or props.get("node.name", "?"),
                media_class=mc,
                active_rate=rate,
                active_format=sfmt,
                props=props,
            )
        )
    return sorted(nodes, key=lambda n: (n.kind != "sink", n.id))


def list_nodes(kinds: tuple[str, ...] = ("Audio/Sink", "Audio/Source")) -> list[AudioNode]:
    return parse_nodes(json.loads(run(["pw-dump"])), kinds)


# --------------------------------------------------------------- settings ---

_META_RE = re.compile(r"key:'([^']*)' value:'([^']*)'")


def parse_settings(text: str) -> dict[str, str]:
    return {k: v for k, v in _META_RE.findall(text)}


def get_settings() -> dict[str, str]:
    return parse_settings(run(["pw-metadata", "-n", "settings"]))


def parse_rate_list(value: str) -> list[int]:
    return [int(x) for x in re.findall(r"\d+", value or "")]


def current_clock() -> dict:
    s = get_settings()
    return {
        "rate": int(s.get("clock.rate", 0) or 0),
        "allowed": parse_rate_list(s.get("clock.allowed-rates", "")),
        "force_rate": int(s.get("clock.force-rate", 0) or 0),
        "quantum": int(s.get("clock.quantum", 0) or 0),
    }


def force_rate(rate: int) -> None:
    """Temporary override. rate=0 removes it. Gone after a PipeWire restart."""
    run(["pw-metadata", "-n", "settings", "0", "clock.force-rate", str(int(rate))])


# ----------------------------------------------------------------- config ---

def backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dest = BACKUP_DIR / f"{path.name}.{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(path, dest)
    return dest


def render_global_conf(rate: int, allowed: list[int]) -> str:
    allowed = sorted(set(allowed) | {rate})
    return (
        "# Managed by hzctl. Edit via `hzctl`, or delete this file to reset.\n"
        "context.properties = {\n"
        f"    default.clock.rate          = {rate}\n"
        f"    default.clock.allowed-rates = [ {' '.join(map(str, allowed))} ]\n"
        "}\n"
    )


def write_global(rate: int, allowed: list[int]) -> Path:
    backup(PW_CONF)
    PW_CONF.parent.mkdir(parents=True, exist_ok=True)
    PW_CONF.write_text(render_global_conf(rate, allowed))
    return PW_CONF


def load_devices() -> dict[str, dict]:
    try:
        return json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_devices(devs: dict[str, dict]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(devs, indent=2, sort_keys=True))


def render_device_conf(devs: dict[str, dict]) -> str:
    """WirePlumber 0.5+ rules, one per device. Sinks/sources share the alsa monitor."""
    rules = []
    for name, cfg in sorted(devs.items()):
        upd = []
        if cfg.get("rate"):
            upd.append(f"audio.rate = {int(cfg['rate'])}")
        if cfg.get("format"):
            upd.append(f'audio.format = "{cfg["format"]}"')
        if not upd:
            continue
        rules.append(
            "  {\n"
            f'    matches = [ {{ node.name = "{name}" }} ]\n'
            f"    actions = {{ update-props = {{ {', '.join(upd)} }} }}\n"
            "  }"
        )
    return (
        "# Managed by hzctl.\n"
        "monitor.alsa.rules = [\n" + "\n".join(rules) + "\n]\n"
    )


def set_device(name: str, rate: int | None = None, fmt: str | None = None) -> Path:
    if fmt and fmt not in SUPPORTED_FORMATS:
        raise HzctlError(f"Format must be one of {SUPPORTED_FORMATS}")
    devs = load_devices()
    cfg = devs.get(name, {})
    if rate is not None:
        cfg["rate"] = int(rate) or None
    if fmt is not None:
        cfg["format"] = fmt or None
    if cfg.get("rate") or cfg.get("format"):
        devs[name] = {k: v for k, v in cfg.items() if v}
    else:
        devs.pop(name, None)
    save_devices(devs)
    return write_device_conf(devs)


def write_device_conf(devs: dict[str, dict]) -> Path:
    backup(WP_CONF)
    if not devs:
        WP_CONF.unlink(missing_ok=True)
        return WP_CONF
    WP_CONF.parent.mkdir(parents=True, exist_ok=True)
    WP_CONF.write_text(render_device_conf(devs))
    return WP_CONF


def restart_services() -> None:
    run(["systemctl", "--user", "restart", *SERVICES], timeout=30)


def reset_all() -> None:
    """Remove everything hzctl wrote, drop the force-rate override, restart."""
    for p in (PW_CONF, WP_CONF):
        backup(p)
        p.unlink(missing_ok=True)
    STATE_FILE.unlink(missing_ok=True)
    try:
        force_rate(0)
    except HzctlError:
        pass
    restart_services()
