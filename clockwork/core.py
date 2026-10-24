"""Clockwork core: talk to PipeWire/WirePlumber via their CLI tools.

Everything here shells out to `pw-dump`, `pw-metadata` and `systemctl --user`,
so there are no compiled dependencies. Config is written as drop-in files so
we never touch the system-wide defaults.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

# Windows-style labels for the common rates.
PRESETS: dict[int, str] = {
    44100: "CD quality (44.1 kHz)",
    48000: "DVD quality (48 kHz)",
    88200: "88.2 kHz",
    96000: "Studio quality (96 kHz)",
    176400: "176.4 kHz",
    192000: "Studio master (192 kHz)",
}
RATES = sorted(PRESETS)

# Sample formats a per-device rule may force, and the bit depth they stand for.
FORMAT_BITS: dict[str, int] = {
    "S16LE": 16,
    "S24LE": 24,      # 24-bit packed in 3 bytes
    "S24_32LE": 24,   # 24-bit in a 32-bit container
    "S32LE": 32,
    "F32LE": 32,
}
SUPPORTED_FORMATS = list(FORMAT_BITS)
# Preferred format per bit depth when the device does not tell us what it takes.
DEFAULT_FORMAT_FOR_BITS = {16: "S16LE", 24: "S24LE", 32: "S32LE"}

MIN_RATE, MAX_RATE = 8000, 768000

HOME = Path.home()
PW_CONF = HOME / ".config/pipewire/pipewire.conf.d/10-clockwork.conf"
WP_CONF = HOME / ".config/wireplumber/wireplumber.conf.d/51-clockwork.conf"
STATE_DIR = HOME / ".config/clockwork"
STATE_FILE = STATE_DIR / "devices.json"
BACKUP_DIR = HOME / ".local/share/clockwork/backups"

SERVICES = ["pipewire", "pipewire-pulse", "wireplumber"]


class ClockworkError(RuntimeError):
    pass


def run(cmd: list[str], timeout: int = 15) -> str:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as e:
        raise ClockworkError(f"Command not found: {cmd[0]} (is PipeWire installed?)") from e
    except subprocess.TimeoutExpired as e:
        raise ClockworkError(f"Timed out: {' '.join(cmd)}") from e
    if res.returncode != 0:
        raise ClockworkError(res.stderr.strip() or f"{cmd[0]} exited with {res.returncode}")
    return res.stdout


def validate_rate(rate: int) -> int:
    rate = int(rate)
    if not MIN_RATE <= rate <= MAX_RATE:
        raise ClockworkError(f"Sample rate {rate} Hz is outside {MIN_RATE}-{MAX_RATE} Hz")
    return rate


def format_bits(fmt: str | None) -> int | None:
    """Bit depth of a SPA format name such as S24_32LE or F32P."""
    if not fmt:
        return None
    if fmt in FORMAT_BITS:
        return FORMAT_BITS[fmt]
    m = re.match(r"[SUF](\d+)", fmt)
    return int(m.group(1)) if m else None


def quality_label(rate: int, bits: int | None = None) -> str:
    """Windows-style label, e.g. '24 bit, 48000 Hz (Studio Quality)'."""
    if bits is None:
        return PRESETS.get(rate, f"{rate} Hz")
    if bits == 16 and rate == 44100:
        tier = "CD Quality"
    elif bits == 16 and rate == 48000:
        tier = "DVD Quality"
    elif bits >= 24 or rate > 48000:
        tier = "Studio Quality"
    else:
        tier = None
    text = f"{bits} bit, {rate} Hz"
    return f"{text} ({tier})" if tier else text


def short_rate(rate: int) -> str:
    khz = rate / 1000
    return f"{khz:g} kHz"


# ---------------------------------------------------------------- devices ---

@dataclass
class AudioNode:
    id: int
    name: str
    description: str
    media_class: str
    active_rate: int | None = None
    active_format: str | None = None
    # What the hardware advertises (EnumFormat). Empty means unknown.
    supported_rates: list[int] = field(default_factory=list)
    supported_formats: list[str] = field(default_factory=list)
    state: str | None = None
    props: dict = field(default_factory=dict, repr=False)

    @property
    def kind(self) -> str:
        return "sink" if self.media_class == "Audio/Sink" else "source"

    @property
    def api(self) -> str:
        api = self.props.get("device.api") or ""
        if not api:
            if self.name.startswith("alsa_"):
                api = "alsa"
            elif self.name.startswith("bluez_"):
                api = "bluez5"
        return api

    @property
    def configurable(self) -> bool:
        """Per-device rules are written as monitor.alsa.rules, so only ALSA nodes apply."""
        return self.api == "alsa"

    @property
    def bus(self) -> str:
        return self.props.get("device.bus") or ("bluetooth" if self.api.startswith("bluez") else "")

    @property
    def icon_name(self) -> str:
        hint = " ".join(
            str(self.props.get(k, "")) for k in ("device.form-factor", "device.icon-name", "node.name", "node.description")
        ).lower()
        if "headset" in hint:
            return "audio-headset-symbolic"
        if "headphone" in hint:
            return "audio-headphones-symbolic"
        if self.kind == "source":
            return "audio-input-microphone-symbolic"
        return "audio-speakers-symbolic"


def _choice_values(v) -> tuple[list, tuple | None]:
    """Decode a pw-dump param value: scalar, enum {default, alt1, ...} or range {min, max}."""
    if isinstance(v, dict):
        if "min" in v and "max" in v:
            return [], (v["min"], v["max"])
        vals = []
        for x in v.values():
            if x not in vals:
                vals.append(x)
        return vals, None
    if isinstance(v, list):
        return list(v), None
    return ([v] if v is not None else []), None


def _active_format(info: dict) -> tuple[int | None, str | None]:
    """Best effort: pull negotiated rate/format out of pw-dump params."""
    params = info.get("params") or {}
    for key in ("Format", "PortConfig"):
        for p in params.get(key) or []:
            fmt = p.get("format", p) if key == "PortConfig" else p
            if not isinstance(fmt, dict):
                continue
            rate, sfmt = fmt.get("rate"), fmt.get("format")
            if isinstance(rate, int) and isinstance(sfmt, str):
                return rate, sfmt
    return None, None


def _enum_formats(info: dict) -> tuple[list[int], list[str]]:
    """Rates (filtered to our presets) and formats the device advertises."""
    rates: set[int] = set()
    formats: list[str] = []
    for p in (info.get("params") or {}).get("EnumFormat") or []:
        if not isinstance(p, dict) or p.get("mediaSubtype", "raw") != "raw":
            continue
        fvals, _ = _choice_values(p.get("format"))
        for f in fvals:
            if isinstance(f, str) and f in FORMAT_BITS and f not in formats:
                formats.append(f)
        rvals, rng = _choice_values(p.get("rate"))
        if rng:
            rates |= {r for r in RATES if rng[0] <= r <= rng[1]}
        rates |= {r for r in rvals if isinstance(r, int) and r in PRESETS}
    return sorted(rates), formats


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
        rates, formats = _enum_formats(info)
        nodes.append(
            AudioNode(
                id=obj.get("id", -1),
                name=props.get("node.name", "?"),
                description=props.get("node.description") or props.get("node.nick") or props.get("node.name", "?"),
                media_class=mc,
                active_rate=rate,
                active_format=sfmt,
                supported_rates=rates,
                supported_formats=formats,
                state=info.get("state"),
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


def parse_defaults(text: str) -> dict[str, str]:
    """`pw-metadata -n default` -> {'sink': node.name, 'source': node.name}."""
    out = {}
    for key, value in _META_RE.findall(text):
        kind = {"default.audio.sink": "sink", "default.audio.source": "source"}.get(key)
        if not kind:
            continue
        try:
            out[kind] = json.loads(value)["name"]
        except (ValueError, KeyError, TypeError):
            out[kind] = value
    return out


def default_devices() -> dict[str, str]:
    try:
        return parse_defaults(run(["pw-metadata", "-n", "default"]))
    except ClockworkError:
        return {}


def force_rate(rate: int) -> None:
    """Temporary override. rate=0 removes it. Gone after a PipeWire restart."""
    rate = int(rate)
    if rate:
        validate_rate(rate)
    run(["pw-metadata", "-n", "settings", "0", "clock.force-rate", str(rate)])


# ----------------------------------------------------------------- config ---

def backup(path: Path) -> Path | None:
    if not path.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"{path.name}.{time.strftime('%Y%m%d-%H%M%S')}"
    dest, n = BACKUP_DIR / stem, 1
    while dest.exists():
        dest, n = BACKUP_DIR / f"{stem}.{n}", n + 1
    shutil.copy2(path, dest)
    return dest


def _write_atomic(path: Path, text: str) -> None:
    """Write via a temp file + rename so a crash never leaves a half-written config."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def render_global_conf(rate: int, allowed: list[int]) -> str:
    allowed = sorted(set(allowed) | {rate})
    return (
        "# Managed by Clockwork. Edit via the app or `clockwork`, or delete this file to reset.\n"
        "context.properties = {\n"
        f"    default.clock.rate          = {rate}\n"
        f"    default.clock.allowed-rates = [ {' '.join(map(str, allowed))} ]\n"
        "}\n"
    )


def write_global(rate: int, allowed: list[int]) -> Path:
    rate = validate_rate(rate)
    allowed = [validate_rate(r) for r in allowed]
    backup(PW_CONF)
    _write_atomic(PW_CONF, render_global_conf(rate, allowed))
    return PW_CONF


def saved_global() -> dict | None:
    """Rate/allowed-rates from our own drop-in, or None if Clockwork never wrote one."""
    try:
        text = PW_CONF.read_text()
    except FileNotFoundError:
        return None
    rate = re.search(r"default\.clock\.rate\s*=\s*(\d+)", text)
    allowed = re.search(r"default\.clock\.allowed-rates\s*=\s*\[([^\]]*)\]", text)
    return {
        "rate": int(rate.group(1)) if rate else 0,
        "allowed": parse_rate_list(allowed.group(1)) if allowed else [],
    }


def load_devices() -> dict[str, dict]:
    try:
        data = json.loads(STATE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_devices(devs: dict[str, dict]) -> None:
    _write_atomic(STATE_FILE, json.dumps(devs, indent=2, sort_keys=True))


_NAME_RE = re.compile(r'^[^"\\\n\r]+$')


def validate_node_name(name: str) -> str:
    # The name is pasted into a SPA-JSON string; quotes or newlines would break the file.
    if not name or not _NAME_RE.match(name):
        raise ClockworkError(f"Invalid node.name: {name!r}")
    return name


def render_device_conf(devs: dict[str, dict]) -> str:
    """WirePlumber 0.5+ rules, one per device. Sinks/sources share the alsa monitor."""
    rules = []
    for name, cfg in sorted(devs.items()):
        validate_node_name(name)
        upd = []
        if cfg.get("rate"):
            upd.append(f"audio.rate = {validate_rate(cfg['rate'])}")
        if cfg.get("format"):
            if cfg["format"] not in FORMAT_BITS:
                raise ClockworkError(f"Format must be one of {SUPPORTED_FORMATS}")
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
        "# Managed by Clockwork.\n"
        "monitor.alsa.rules = [\n" + "\n".join(rules) + "\n]\n"
    )


def set_device(name: str, rate: int | None = None, fmt: str | None = None) -> Path:
    validate_node_name(name)
    if fmt and fmt not in SUPPORTED_FORMATS:
        raise ClockworkError(f"Format must be one of {SUPPORTED_FORMATS}")
    if rate:
        validate_rate(rate)
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
    return write_devices(devs)


def write_devices(devs: dict[str, dict]) -> Path:
    """Persist the full set of per-device rules (state file + WirePlumber drop-in)."""
    devs = {name: {k: v for k, v in cfg.items() if v} for name, cfg in devs.items()}
    devs = {name: cfg for name, cfg in devs.items() if cfg}
    conf = render_device_conf(devs)  # validates before anything touches disk
    backup(WP_CONF)
    if devs:
        _write_atomic(WP_CONF, conf)
    else:
        WP_CONF.unlink(missing_ok=True)
    save_devices(devs)
    return WP_CONF


def restart_services() -> None:
    run(["systemctl", "--user", "restart", *SERVICES], timeout=30)


def reset_all() -> None:
    """Remove everything Clockwork wrote, drop the force-rate override, restart."""
    for p in (PW_CONF, WP_CONF, STATE_FILE):
        backup(p)
        p.unlink(missing_ok=True)
    try:
        force_rate(0)
    except ClockworkError:
        pass
    restart_services()
