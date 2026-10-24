"""Clockwork command line interface."""
from __future__ import annotations

import argparse
import sys
import time

from . import core


def _label(rate: int) -> str:
    return core.quality_label(rate)


def cmd_status(_: argparse.Namespace) -> None:
    c = core.current_clock()
    print(f"Graph rate     : {_label(c['rate'])}")
    print("Allowed rates  : " + ", ".join(str(r) for r in c["allowed"]))
    print(f"Force-rate     : {c['force_rate'] or 'off'}")
    print(f"Quantum        : {c['quantum']}")
    print()
    for n in core.list_nodes():
        extra = ""
        if n.active_rate or n.active_format:
            extra = f"  [{n.active_format or '?'} @ {n.active_rate or '?'}]"
        note = "" if n.configurable else "  (not adjustable: not an ALSA device)"
        print(f"{n.id:>4}  {n.kind:<6} {n.description}{extra}{note}")
        print(f"      {n.name}")
    devs = core.load_devices()
    if devs:
        print("\nPer-device rules saved by Clockwork:")
        for name, cfg in devs.items():
            print(f"  {name}: {cfg}")


def cmd_list(_: argparse.Namespace) -> None:
    for n in core.list_nodes():
        print(f"{n.id}\t{n.kind}\t{n.name}\t{n.description}")


def cmd_force(a: argparse.Namespace) -> None:
    core.force_rate(a.rate)
    print(f"force-rate = {a.rate or 'off'} (temporary)")
    if a.seconds:
        print(f"Reverting in {a.seconds}s... (Ctrl+C to revert now)")
        try:
            time.sleep(a.seconds)
        except KeyboardInterrupt:
            pass
        core.force_rate(0)
        print("force-rate reverted")


def cmd_set(a: argparse.Namespace) -> None:
    allowed = a.allowed or [a.rate]
    path = core.write_global(a.rate, allowed)
    print(f"Wrote {path}")
    if not a.no_restart:
        core.restart_services()
        print("Restarted pipewire, pipewire-pulse, wireplumber")


def cmd_device(a: argparse.Namespace) -> None:
    try:
        node = next((n for n in core.list_nodes() if n.name == a.node), None)
    except core.ClockworkError:
        node = None  # PipeWire not running; still allow writing the rule
    if node is None:
        print(f"warning: no device named {a.node!r} is connected right now", file=sys.stderr)
    elif not node.configurable:
        print(f"warning: {a.node} is a {node.api or 'non-ALSA'} device; the rule will have no effect", file=sys.stderr)
    path = core.set_device(a.node, rate=a.rate, fmt=a.format)
    print(f"Wrote {path}")
    if not a.no_restart:
        core.restart_services()
        print("Restarted audio services")


def cmd_reset(_: argparse.Namespace) -> None:
    core.reset_all()
    print("Removed Clockwork config and restarted audio services (backups kept)")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="clockwork", description="Clockwork: sample-rate control for PipeWire")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="show clock settings and devices").set_defaults(fn=cmd_status)
    sub.add_parser("list", help="list sinks/sources (tab separated)").set_defaults(fn=cmd_list)

    f = sub.add_parser("force", help="temporarily force a rate (0 = off)")
    f.add_argument("rate", type=int)
    f.add_argument("-s", "--seconds", type=int, default=0, help="auto-revert after N seconds")
    f.set_defaults(fn=cmd_force)

    s = sub.add_parser("set", help="persist default rate + allowed rates")
    s.add_argument("rate", type=int)
    s.add_argument("--allowed", type=int, nargs="+", help="e.g. --allowed 44100 48000 96000")
    s.add_argument("--no-restart", action="store_true")
    s.set_defaults(fn=cmd_set)

    d = sub.add_parser("device", help="persist a per-device rule (WirePlumber)")
    d.add_argument("node", help="node.name, see `clockwork list`")
    d.add_argument("--rate", type=int)
    d.add_argument("--format", choices=core.SUPPORTED_FORMATS)
    d.add_argument("--no-restart", action="store_true")
    d.set_defaults(fn=cmd_device)

    sub.add_parser("reset", help="remove everything Clockwork wrote").set_defaults(fn=cmd_reset)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except core.ClockworkError as e:
        print(f"clockwork: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
