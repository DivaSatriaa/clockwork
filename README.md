# hzctl

Windows-style **sample rate control for Linux (PipeWire)**. Windows lets you pick
"CD quality / DVD quality / Studio quality" per device. On Ubuntu there is no GUI
for that, even though PipeWire supports it. `hzctl` fills the gap with a CLI and
a small GTK4/libadwaita app.

> Status: early (v0.1). Tested on Ubuntu with PipeWire 1.6 + WirePlumber.

## How it works

PipeWire runs a **single global clock**; every device is resampled to it. So
there are two levers, and `hzctl` drives both:

| What | Mechanism | Scope |
|------|-----------|-------|
| Graph rate + allowed rates | `~/.config/pipewire/pipewire.conf.d/10-hzctl.conf` | global, persistent |
| Try a rate right now | `pw-metadata -n settings 0 clock.force-rate N` | global, until restart |
| Per-device rate/format | WirePlumber rule in `~/.config/wireplumber/wireplumber.conf.d/51-hzctl.conf` | per `node.name`, persistent |

Why `allowed-rates` matters: by default it is `[ 48000 ]`, so a 44.1 kHz stream
(e.g. a game or music player) is resampled to 48 kHz. With
`[ 44100 48000 96000 ]` PipeWire can switch the graph to the source's rate and
skip the resampling.

Nothing system-wide is modified. Existing files are backed up to
`~/.local/share/hzctl/backups/` before they are overwritten, and `hzctl reset`
removes everything it wrote.

## Install

```bash
sudo apt install pipewire pipewire-pulse wireplumber python3-gi gir1.2-adw-1 gir1.2-gtk-4.0
pip install --user --break-system-packages .
```

## Usage

```bash
hzctl status                       # clock settings + sinks/sources
hzctl list                         # node ids and node.name (for per-device rules)

hzctl force 44100 --seconds 15     # try a rate, auto-revert (watch `pw-top`)
hzctl set 48000 --allowed 44100 48000 96000
hzctl device alsa_output.usb-... --rate 96000 --format S24LE
hzctl reset

hzctl-gui                          # GTK4 app
```

Verify with `pw-top` (RATE column) while audio is playing; an idle graph
suspends and shows no rate.

## Limitations

- The global clock is shared. A per-device rule sets what that device is opened
  at, but the graph still resamples between rates unless `allowed-rates`
  contains them.
- Active format/rate shown in `hzctl status` is read from `pw-dump` on a
  best-effort basis and may be missing for suspended nodes.
- Per-device rules use the WirePlumber 0.5+ config format.

## Development

```bash
pip install pytest
pytest
```

## License

MIT
