"""GTK4 + libadwaita front-end for hzctl."""
from __future__ import annotations

import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402

from . import core  # noqa: E402

RATES = sorted(core.PRESETS)
RATE_LABELS = [core.PRESETS[r] for r in RATES]


class Window(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application):
        super().__init__(application=app, title="hzctl", default_width=560, default_height=760)
        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()
        view.add_top_bar(Adw.HeaderBar())
        self.page = Adw.PreferencesPage()
        view.set_content(self.page)
        self.toasts.set_child(view)
        self.set_content(self.toasts)

        self.allowed_rows: dict[int, Adw.SwitchRow] = {}
        self.device_rows: dict[str, Adw.ComboRow] = {}
        self._build()

    # ------------------------------------------------------------- helpers
    def toast(self, msg: str) -> None:
        self.toasts.add_toast(Adw.Toast.new(msg))

    def _safe(self, fn, ok_msg: str) -> None:
        try:
            fn()
            self.toast(ok_msg)
        except core.HzctlError as e:
            self.toast(f"Error: {e}")

    # --------------------------------------------------------------- build
    def _build(self) -> None:
        try:
            clock = core.current_clock()
            nodes = core.list_nodes(("Audio/Sink",))
        except core.HzctlError as e:
            g = Adw.PreferencesGroup(title="PipeWire not reachable", description=str(e))
            self.page.add(g)
            return
        saved = core.load_devices()

        g1 = Adw.PreferencesGroup(
            title="Graph clock",
            description="PipeWire runs one global clock; devices are resampled to it.",
        )
        self.rate_row = Adw.ComboRow(title="Default sample rate", model=Gtk.StringList.new(RATE_LABELS))
        cur = clock["rate"]
        if cur in RATES:
            self.rate_row.set_selected(RATES.index(cur))
        g1.add(self.rate_row)
        for r in RATES:
            row = Adw.SwitchRow(title=f"Allow {r} Hz", subtitle="Graph may switch to this rate to match the source")
            row.set_active(r in clock["allowed"])
            self.allowed_rows[r] = row
            g1.add(row)
        self.page.add(g1)

        g2 = Adw.PreferencesGroup(title="Output devices", description="Per-device rule (WirePlumber)")
        for n in nodes:
            labels = ["Follow graph"] + RATE_LABELS
            row = Adw.ComboRow(title=n.description, subtitle=n.name, model=Gtk.StringList.new(labels))
            sr = saved.get(n.name, {}).get("rate")
            row.set_selected(RATES.index(sr) + 1 if sr in RATES else 0)
            self.device_rows[n.name] = row
            g2.add(row)
        self.page.add(g2)

        g3 = Adw.PreferencesGroup(title="Apply")
        box = Gtk.Box(spacing=8, homogeneous=True, margin_top=8)
        for label, handler, css in (
            ("Try now (temporary)", self.on_try, []),
            ("Save (permanent)", self.on_save, ["suggested-action"]),
            ("Reset", self.on_reset, ["destructive-action"]),
        ):
            b = Gtk.Button(label=label)
            for c in css:
                b.add_css_class(c)
            b.connect("clicked", handler)
            box.append(b)
        g3.add(box)
        self.page.add(g3)

    # ------------------------------------------------------------ handlers
    def _selected_rate(self) -> int:
        return RATES[self.rate_row.get_selected()]

    def on_try(self, _btn) -> None:
        self._safe(lambda: core.force_rate(self._selected_rate()), "Forced until restart. Reset to undo.")

    def on_save(self, _btn) -> None:
        def do() -> None:
            rate = self._selected_rate()
            allowed = [r for r, row in self.allowed_rows.items() if row.get_active()]
            core.write_global(rate, allowed)
            devs = {}
            for name, row in self.device_rows.items():
                i = row.get_selected()
                if i > 0:
                    devs[name] = {"rate": RATES[i - 1]}
            core.save_devices(devs)
            core.write_device_conf(devs)
            core.restart_services()

        self._safe(do, "Saved and audio services restarted")

    def on_reset(self, _btn) -> None:
        self._safe(core.reset_all, "Reset. Reopen hzctl to refresh.")


class App(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id="io.github.hzctl")

    def do_activate(self) -> None:
        (self.props.active_window or Window(self)).present()


def main() -> int:
    return App().run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
