# Clockwork

**Pick the sample rate and bit depth of every speaker, headset and microphone on
Linux (PipeWire)** — the "Default Format" dropdown Windows has
(*16 bit, 44100 Hz (CD Quality)*, *24 bit, 48000 Hz (Studio Quality)*…), which
Linux desktops don't ship. Clockwork is a GTK4/libadwaita app plus a CLI.

| Playback | Clock (dark) |
|---|---|
| ![Playback tab](docs/playback.png) | ![Clock tab](docs/clock-dark.png) |

> Status: early (v0.2). Tested on Ubuntu 24.04 with PipeWire + WirePlumber 0.5.
> Linux only — on Windows the built-in Sound settings already do this.

## Features

- **Playback / Recording tabs** — one row per device with a Windows-style format
  dropdown. Only rates and bit depths the hardware reports are offered.
- **Live status** — see what each device is running at right now, and which one
  is the default.
- **Clock tab** — default sample rate, automatic rate switching (play 44.1 kHz
  music without resampling) and buffer size at a glance.
- **Try before you save** — switch the clock for 15 seconds, then it reverts on
  its own.
- **Safe** — changes are staged until you press *Apply*; nothing system-wide is
  touched, every overwritten file is backed up, and *Reset Everything* undoes it all.

## How it works

PipeWire runs a **single global clock**; every device is resampled to it. So
there are two levers, and Clockwork drives both:

| What | Mechanism | Scope |
|------|-----------|-------|
| Default rate + allowed rates | `~/.config/pipewire/pipewire.conf.d/10-clockwork.conf` | global, persistent |
| Try a rate right now | `pw-metadata -n settings 0 clock.force-rate N` | global, until restart |
| Per-device rate/format | WirePlumber rule in `~/.config/wireplumber/wireplumber.conf.d/51-clockwork.conf` | per `node.name`, persistent |

Why `allowed-rates` matters: by default it is `[ 48000 ]`, so a 44.1 kHz stream
(e.g. a game or music player) is resampled to 48 kHz. With
`[ 44100 48000 96000 ]` PipeWire can switch the graph to the source's rate and
skip the resampling.

Applying restarts `pipewire`, `pipewire-pulse` and `wireplumber` (user
services), so sound cuts out for about a second. Backups go to
`~/.local/share/clockwork/backups/`.

## Install

```bash
sudo apt install pipewire pipewire-pulse wireplumber python3-gi gir1.2-adw-1 gir1.2-gtk-4.0
pip install --user --break-system-packages .

# App launcher entry + icon
install -Dm644 io.github.divasatriaa.Clockwork.desktop ~/.local/share/applications/io.github.divasatriaa.Clockwork.desktop
install -Dm644 clockwork/data/icons/hicolor/scalable/apps/io.github.divasatriaa.Clockwork.svg \
  ~/.local/share/icons/hicolor/scalable/apps/io.github.divasatriaa.Clockwork.svg
```

The GUI needs libadwaita 1.5+ (Ubuntu 24.04, Fedora 40, or newer).

## Usage

```bash
clockwork-gui                          # the app

clockwork status                       # clock settings + devices
clockwork list                         # node ids and node.name (for per-device rules)
clockwork force 44100 --seconds 15     # try a rate, auto-revert (watch `pw-top`)
clockwork set 48000 --allowed 44100 48000 96000
clockwork device alsa_output.usb-... --rate 96000 --format S24LE
clockwork reset
```

Verify with `pw-top` (RATE column) while audio is playing; an idle graph
suspends and shows no rate.

## Limitations

- The global clock is shared. A per-device rule sets what that device is opened
  at, but the graph still resamples between rates unless `allowed-rates`
  contains them.
- Bluetooth devices can't be set: the codec (SBC, AAC, LDAC…) decides the rate.
- Live format and the list of supported formats are read from `pw-dump` on a
  best-effort basis and may be missing for some drivers; the full list is
  offered then.
- Per-device rules use the WirePlumber 0.5+ config format.

## Development

```bash
pip install pytest
pytest
```

## License

MIT
