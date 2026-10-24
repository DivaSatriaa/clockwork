"""GTK4 + libadwaita front-end for Clockwork."""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from . import __version__, core  # noqa: E402

APP_ID = "io.github.divasatriaa.Clockwork"
ICON_DIR = Path(__file__).parent / "data" / "icons"
POLL_SECONDS = 5
TEST_SECONDS = 15
RESTART_SETTLE = 1.5  # PipeWire needs a moment before pw-dump sees devices again

CSS = """
.cw-hero { padding: 18px 12px 6px 12px; }
.cw-hero-rate { font-size: 44px; font-weight: 800; font-feature-settings: "tnum"; }
"""


def fetch_state() -> dict:
    """Everything the UI shows. Runs in a worker thread."""
    return {
        "clock": core.current_clock(),
        "nodes": core.list_nodes(),
        "defaults": core.default_devices(),
        "saved_devs": core.load_devices(),
    }


def device_options(node: core.AudioNode) -> list[tuple[str, dict]]:
    """Windows-style 'Default Format' choices, limited to what the hardware advertises."""
    opts: list[tuple[str, dict]] = [("Follow system clock", {})]
    rates = node.supported_rates or core.RATES
    for bits in (16, 24, 32):
        if node.supported_formats:
            fmts = [f for f in node.supported_formats if core.FORMAT_BITS[f] == bits]
            # Prefer integer formats; F32 only if it's all the device takes.
            fmts.sort(key=lambda f: f.startswith("F"))
            if not fmts:
                continue
            fmt = fmts[0]
        else:
            fmt = core.DEFAULT_FORMAT_FOR_BITS[bits]
        for r in rates:
            opts.append((core.quality_label(r, bits), {"rate": r, "format": fmt}))
    return opts


def describe_rule(cfg: dict) -> str:
    rate, fmt = cfg.get("rate"), cfg.get("format")
    if rate and fmt:
        return core.quality_label(rate, core.format_bits(fmt))
    if rate:
        return f"{rate} Hz, format unchanged"
    return f"{core.format_bits(fmt)} bit, rate follows clock"


def node_status(node: core.AudioNode, is_default: bool) -> str:
    parts = ["Default"] if is_default else []
    if node.state == "running" and node.active_rate:
        bits = core.format_bits(node.active_format)
        now = core.short_rate(node.active_rate)
        verb = "Playing" if node.kind == "sink" else "Recording"
        parts.append(f"{verb} at {bits}-bit · {now}" if bits else f"{verb} at {now}")
    elif node.state in ("idle", "suspended"):
        parts.append("Idle")
    if not node.configurable:
        parts.append("Bluetooth · format set by the codec" if node.bus == "bluetooth" else "Not adjustable")
    return "  ·  ".join(parts) if parts else node.name


def short_rule(cfg: dict) -> str:
    """Compact text for the closed dropdown, so long Windows labels don't get cut off."""
    if not cfg:
        return "System clock"
    rate, bits = cfg.get("rate"), core.format_bits(cfg.get("format"))
    if rate and bits:
        return f"{bits}-bit · {core.short_rate(rate)}"
    return f"{core.short_rate(rate)}" if rate else f"{bits}-bit"


def compact_factories(shorts: dict[str, str]) -> tuple[Gtk.ListItemFactory, Gtk.ListItemFactory]:
    """(factory, list_factory) for a ComboRow: short text when closed, full label in the popup."""
    def setup(_f, item):
        item.set_child(Gtk.Label(xalign=0))

    closed = Gtk.SignalListItemFactory()
    closed.connect("setup", setup)
    # The closed button binds a one-item model, so look up by label, not position.
    closed.connect("bind", lambda _f, item: item.get_child().set_label(
        shorts.get(item.get_item().get_string(), item.get_item().get_string())))
    popup = Gtk.SignalListItemFactory()
    popup.connect("setup", setup)
    popup.connect("bind", lambda _f, item: item.get_child().set_label(item.get_item().get_string()))
    return closed, popup


class DevicePage(Gtk.Box):
    """Playback or Recording tab: one row per device."""

    def __init__(self, win: "Window", kind: str):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win, self.kind = win, kind
        self.rows: dict[str, Adw.PreferencesRow] = {}
        self.options: dict[str, list[tuple[str, dict]]] = {}
        self.names: tuple[str, ...] = ()
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE, vexpand=True)
        self.append(self.stack)

        noun = "playback" if kind == "sink" else "recording"
        empty = Adw.StatusPage(
            icon_name="audio-speakers-symbolic" if kind == "sink" else "audio-input-microphone-symbolic",
            title=f"No {noun} devices",
            description="Plug in a device and it will show up here.",
        )
        self.stack.add_named(empty, "empty")
        self.page = Adw.PreferencesPage()
        self.stack.add_named(self.page, "list")
        self.group: Adw.PreferencesGroup | None = None

    def rebuild(self, nodes: list[core.AudioNode], defaults: dict[str, str]) -> None:
        if self.group:
            self.page.remove(self.group)
        self.rows.clear()
        self.options.clear()
        nodes = [n for n in nodes if n.kind == self.kind]
        self.names = tuple(n.name for n in nodes)
        if not nodes:
            self.group = None
            self.stack.set_visible_child_name("empty")
            return
        self.stack.set_visible_child_name("list")

        title = "Output devices" if self.kind == "sink" else "Input devices"
        self.group = Adw.PreferencesGroup(
            title=title,
            description="The format each device is opened at — like “Default Format” in Windows "
                        "sound settings. Options are limited to what the device reports.",
        )
        # Default device first, then by name.
        nodes.sort(key=lambda n: (n.name != defaults.get(self.kind), n.description.lower()))
        for n in nodes:
            self.group.add(self._make_row(n, n.name == defaults.get(self.kind)))
        self.page.add(self.group)

    def _make_row(self, node: core.AudioNode, is_default: bool) -> Adw.PreferencesRow:
        icon = Gtk.Image(icon_name=node.icon_name)
        if not node.configurable:
            row = Adw.ActionRow(title=node.description, subtitle=node_status(node, is_default),
                                use_markup=False, tooltip_text=node.name)
            row.add_prefix(icon)
            auto = Gtk.Label(label="Automatic", valign=Gtk.Align.CENTER)
            auto.add_css_class("dim-label")
            row.add_suffix(auto)
            self.rows[node.name] = row
            return row

        opts = device_options(node)
        want = self.win.pending_devs.get(node.name, {})
        if want and want not in [cfg for _, cfg in opts]:
            opts.append((describe_rule(want), want))
        self.options[node.name] = opts

        row = Adw.ComboRow(
            title=node.description,
            subtitle=node_status(node, is_default),
            model=Gtk.StringList.new([label for label, _ in opts]),
            tooltip_text=node.name,
            use_markup=False,
        )
        closed, popup = compact_factories({label: short_rule(cfg) for label, cfg in opts})
        row.set_factory(closed)
        row.set_list_factory(popup)
        row.add_prefix(icon)
        row.set_selected(self._index_of(node.name, want))
        row.connect("notify::selected", self._on_selected, node.name)
        self.rows[node.name] = row
        return row

    def _index_of(self, name: str, cfg: dict) -> int:
        for i, (_, c) in enumerate(self.options.get(name, [])):
            if c == cfg:
                return i
        return 0

    def _on_selected(self, row: Adw.ComboRow, _pspec, name: str) -> None:
        if self.win.syncing:
            return
        cfg = self.options[name][row.get_selected()][1]
        if cfg:
            self.win.pending_devs[name] = dict(cfg)
        else:
            self.win.pending_devs.pop(name, None)
        self.win.update_dirty()

    def sync_selection(self) -> None:
        for name, row in self.rows.items():
            if isinstance(row, Adw.ComboRow):
                row.set_selected(self._index_of(name, self.win.pending_devs.get(name, {})))

    def update_status(self, nodes: list[core.AudioNode], defaults: dict[str, str]) -> None:
        for n in nodes:
            row = self.rows.get(n.name)
            if row is not None:
                row.set_subtitle(node_status(n, n.name == defaults.get(self.kind)))


class ClockPage(Adw.PreferencesPage):
    """Global graph clock: default rate, allowed rates, temporary test."""

    def __init__(self, win: "Window"):
        super().__init__()
        self.win = win
        self.rates: list[int] = list(core.RATES)

        # Hero: what the clock is doing right now.
        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, halign=Gtk.Align.CENTER)
        hero.add_css_class("cw-hero")
        self.hero_rate = Gtk.Label(label="—")
        self.hero_rate.add_css_class("cw-hero-rate")
        self.hero_caption = Gtk.Label()
        self.hero_caption.add_css_class("dim-label")
        hero.append(self.hero_rate)
        hero.append(self.hero_caption)
        hg = Adw.PreferencesGroup()
        hg.add(hero)
        self.add(hg)

        g1 = Adw.PreferencesGroup(
            title="System clock",
            description="PipeWire runs every device on one shared clock and resamples anything "
                        "that doesn't match it.",
        )
        self.rate_row = Adw.ComboRow(title="Default sample rate", subtitle="Used for most apps and when idle")
        self.rate_row.add_prefix(Gtk.Image(icon_name="preferences-system-time-symbolic"))
        self.rate_row.connect("notify::selected", self._on_rate)
        g1.add(self.rate_row)
        self.add(g1)

        g2 = Adw.PreferencesGroup(
            title="Automatic rate switching",
            description="Let the clock follow the source, so 44.1 kHz music or 96 kHz files "
                        "play without resampling. Switching only happens when nothing else is playing.",
        )
        self.allowed_rows: dict[int, Adw.SwitchRow] = {}
        for r in core.RATES:
            row = Adw.SwitchRow(title=core.quality_label(r))
            row.connect("notify::active", self._on_allowed, r)
            self.allowed_rows[r] = row
            g2.add(row)
        self.add(g2)

        g3 = Adw.PreferencesGroup(
            title="Try a rate",
            description=f"Switch the clock for {TEST_SECONDS} seconds to hear the difference. "
                        "Nothing is saved.",
        )
        self.test_row = Adw.ComboRow(title="Rate to try", model=Gtk.StringList.new([core.quality_label(r) for r in core.RATES]))
        self.test_row.set_selected(core.RATES.index(96000))
        self.test_btn = Gtk.Button(label="Start test", valign=Gtk.Align.CENTER)
        self.test_btn.add_css_class("pill")
        self.test_btn.connect("clicked", lambda _b: self.win.start_test(core.RATES[self.test_row.get_selected()]))
        self.test_row.add_suffix(self.test_btn)
        g3.add(self.test_row)
        self.add(g3)

    def sync_selection(self) -> None:
        rate = self.win.want_rate
        if rate and rate not in self.rates:
            self.rates = sorted(set(core.RATES) | {rate})
        self.rate_row.set_model(Gtk.StringList.new([core.quality_label(r) for r in self.rates]))
        if rate in self.rates:
            self.rate_row.set_selected(self.rates.index(rate))
        for r, row in self.allowed_rows.items():
            locked = r == rate
            row.set_active(locked or r in self.win.want_allowed)
            row.set_sensitive(not locked)
            row.set_subtitle("Default rate — always allowed" if locked else "")

    def update_status(self, clock: dict, testing: int | None) -> None:
        live = testing or clock.get("force_rate") or clock.get("rate")
        self.hero_rate.set_label(core.short_rate(live) if live else "—")
        bits = []
        if testing:
            bits.append("Test running")
        elif clock.get("force_rate"):
            bits.append("Forced until restart")
        else:
            bits.append("System clock")
        if clock.get("quantum"):
            q, r = clock["quantum"], live or 48000
            bits.append(f"buffer {q} samples ({q / r * 1000:.1f} ms)")
        self.hero_caption.set_label("  ·  ".join(bits))
        self.test_btn.set_sensitive(not testing)

    def _on_rate(self, row: Adw.ComboRow, _pspec) -> None:
        if self.win.syncing:
            return
        self.win.want_rate = self.rates[row.get_selected()]
        self.win.syncing = True
        try:
            self.sync_selection()
        finally:
            self.win.syncing = False
        self.win.update_dirty()

    def _on_allowed(self, row: Adw.SwitchRow, _pspec, rate: int) -> None:
        if self.win.syncing:
            return
        if row.get_active():
            self.win.want_allowed.add(rate)
        else:
            self.win.want_allowed.discard(rate)
        self.win.update_dirty()


class Window(Adw.ApplicationWindow):
    def __init__(self, app: Adw.Application):
        super().__init__(application=app, title="Clockwork", default_width=760, default_height=720)
        self.set_size_request(360, 480)
        self.syncing = False
        self.busy = False
        self.testing: int | None = None
        self.test_left = 0
        self.state: dict = {}
        # Saved (on disk / live) vs. wanted (what the UI shows).
        self.saved_rate = 0
        self.saved_allowed: set[int] = set()
        self.saved_devs: dict[str, dict] = {}
        self.want_rate = 0
        self.want_allowed: set[int] = set()
        self.pending_devs: dict[str, dict] = {}

        self._build()
        self.connect("close-request", self._on_close)
        self.refresh(initial=True)
        GLib.timeout_add_seconds(POLL_SECONDS, self._poll)

    # --------------------------------------------------------------- build
    def _build(self) -> None:
        self.toasts = Adw.ToastOverlay()
        view = Adw.ToolbarView()

        header = Adw.HeaderBar()
        self.switcher = Adw.ViewSwitcher(policy=Adw.ViewSwitcherPolicy.WIDE)
        self.narrow_title = Adw.WindowTitle(title="Clockwork", visible=False)
        title_box = Gtk.Box()
        title_box.append(self.switcher)
        title_box.append(self.narrow_title)
        header.set_title_widget(title_box)

        menu = Gio.Menu()
        menu.append("Refresh", "win.refresh")
        menu.append("Discard Changes", "win.discard")
        menu.append("Reset Everything…", "win.reset")
        about = Gio.Menu()
        about.append("About Clockwork", "win.about")
        menu.append_section(None, about)
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Main Menu"))

        self.spinner = Gtk.Spinner(visible=False)
        header.pack_start(self.spinner)
        view.add_top_bar(header)

        self.dirty_banner = Adw.Banner(
            title="Unsaved changes — audio pauses briefly when you apply",
            button_label="Apply",
        )
        self.dirty_banner.connect("button-clicked", lambda _b: self.apply())
        view.add_top_bar(self.dirty_banner)
        self.test_banner = Adw.Banner(button_label="Stop Test")
        self.test_banner.connect("button-clicked", lambda _b: self.stop_test())
        view.add_top_bar(self.test_banner)

        self.views = Adw.ViewStack(vexpand=True)
        self.playback = DevicePage(self, "sink")
        self.recording = DevicePage(self, "source")
        self.clock = ClockPage(self)
        self.views.add_titled_with_icon(self.playback, "playback", "Playback", "audio-speakers-symbolic")
        self.views.add_titled_with_icon(self.recording, "recording", "Recording", "audio-input-microphone-symbolic")
        self.views.add_titled_with_icon(self.clock, "clock", "Clock", "preferences-system-time-symbolic")
        self.switcher.set_stack(self.views)

        # Loading / error / main.
        self.body = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        loading = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, valign=Gtk.Align.CENTER, spacing=12)
        sp = Gtk.Spinner(spinning=True, width_request=32, height_request=32)
        loading.append(sp)
        loading.append(Gtk.Label(label="Reading audio devices…", css_classes=["dim-label"]))
        self.body.add_named(loading, "loading")
        self.error_page = Adw.StatusPage(icon_name="dialog-warning-symbolic", title="Can't reach PipeWire")
        retry = Gtk.Button(label="Try Again", halign=Gtk.Align.CENTER, css_classes=["pill", "suggested-action"])
        retry.connect("clicked", lambda _b: self.refresh(initial=True))
        self.error_page.set_child(retry)
        self.body.add_named(self.error_page, "error")
        self.body.add_named(self.views, "main")
        view.set_content(self.body)

        bottom = Adw.ViewSwitcherBar(stack=self.views)
        view.add_bottom_bar(bottom)

        self.toasts.set_child(view)
        self.set_content(self.toasts)

        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 560sp"))
        bp.add_setter(bottom, "reveal", True)
        bp.add_setter(self.switcher, "visible", False)
        bp.add_setter(self.narrow_title, "visible", True)
        self.add_breakpoint(bp)

        for name, fn, accel in (
            ("refresh", lambda *_: self.refresh(), "<Ctrl>r"),
            ("apply", lambda *_: self.apply(), "<Ctrl>s"),
            ("discard", lambda *_: self.discard(), None),
            ("reset", lambda *_: self.confirm_reset(), None),
            ("about", lambda *_: self.show_about(), None),
        ):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", fn)
            self.add_action(act)
            if accel:
                self.get_application().set_accels_for_action(f"win.{name}", [accel])
        self.get_application().set_accels_for_action("window.close", ["<Ctrl>w"])
        self.get_application().set_accels_for_action("app.quit", ["<Ctrl>q"])

    # ------------------------------------------------------------- helpers
    def toast(self, msg: str, timeout: int = 3) -> None:
        self.toasts.add_toast(Adw.Toast(title=msg, timeout=timeout))

    def run_bg(self, work, done, busy: bool = True) -> None:
        """Run `work()` in a thread; call `done(result, error)` on the main loop."""
        if busy:
            self.set_busy(True)

        def target():
            try:
                res, err = work(), None
            except core.ClockworkError as e:
                res, err = None, e
            except Exception as e:  # don't let a worker crash silently
                res, err = None, core.ClockworkError(str(e))

            def finish():
                if busy:
                    self.set_busy(False)
                done(res, err)
                return False

            GLib.idle_add(finish)

        threading.Thread(target=target, daemon=True).start()

    def set_busy(self, busy: bool) -> None:
        self.busy = busy
        self.spinner.set_visible(busy)
        self.spinner.set_spinning(busy)
        self.views.set_sensitive(not busy)
        self.dirty_banner.set_sensitive(not busy)

    @property
    def dirty(self) -> bool:
        norm = lambda rate, allowed: (rate, frozenset(allowed | {rate}))  # noqa: E731
        return (
            norm(self.want_rate, self.want_allowed) != norm(self.saved_rate, self.saved_allowed)
            or self.pending_devs != self.saved_devs
        )

    def update_dirty(self) -> None:
        d = self.dirty
        self.dirty_banner.set_revealed(d)

    # -------------------------------------------------------------- state
    def refresh(self, initial: bool = False) -> None:
        if initial:
            self.body.set_visible_child_name("loading")
        self.run_bg(fetch_state, lambda s, e: self._on_state(s, e, reset_wanted=initial), busy=not initial)

    def _poll(self) -> bool:
        if not self.busy and self.body.get_visible_child_name() == "main":
            self.run_bg(fetch_state, lambda s, e: self._on_state(s, e, reset_wanted=False), busy=False)
        return True

    def _on_state(self, state: dict | None, err: Exception | None, reset_wanted: bool) -> None:
        if err:
            if self.body.get_visible_child_name() != "main":
                self.error_page.set_description(
                    f"{err}\n\nClockwork needs PipeWire and WirePlumber running in your session."
                )
                self.body.set_visible_child_name("error")
            return
        first = not self.state
        self.state = state
        clock = state["clock"]
        if first or reset_wanted:
            self.saved_rate = clock["rate"]
            self.saved_allowed = set(clock["allowed"])
            self.saved_devs = state["saved_devs"]
            self.want_rate = self.saved_rate
            self.want_allowed = set(self.saved_allowed)
            self.pending_devs = {k: dict(v) for k, v in self.saved_devs.items()}

        nodes, defaults = state["nodes"], state["defaults"]
        self.syncing = True
        try:
            for page in (self.playback, self.recording):
                names = tuple(sorted(n.name for n in nodes if n.kind == page.kind))
                if first or reset_wanted or names != tuple(sorted(page.names)):
                    page.rebuild(nodes, defaults)
                else:
                    page.update_status(nodes, defaults)
            if first or reset_wanted:
                self.clock.sync_selection()
        finally:
            self.syncing = False
        self.clock.update_status(clock, self.testing)
        self.body.set_visible_child_name("main")
        self.update_dirty()

    def discard(self) -> None:
        self.want_rate = self.saved_rate
        self.want_allowed = set(self.saved_allowed)
        self.pending_devs = {k: dict(v) for k, v in self.saved_devs.items()}
        self.syncing = True
        try:
            self.playback.sync_selection()
            self.recording.sync_selection()
            self.clock.sync_selection()
        finally:
            self.syncing = False
        self.update_dirty()
        self.toast("Changes discarded")

    def apply(self) -> None:
        if self.busy or not self.dirty:
            return
        rate, allowed = self.want_rate, set(self.want_allowed)
        devs = {k: dict(v) for k, v in self.pending_devs.items()}
        global_changed = (rate, allowed | {rate}) != (self.saved_rate, self.saved_allowed | {self.saved_rate})
        devs_changed = devs != self.saved_devs

        def work():
            if global_changed:
                core.write_global(rate, sorted(allowed))
            if devs_changed:
                core.write_devices(devs)
            core.restart_services()
            time.sleep(RESTART_SETTLE)
            return fetch_state()

        def done(state, err):
            if err:
                self._error_dialog("Couldn't apply changes", str(err))
                return
            self.testing = None
            self.test_banner.set_revealed(False)
            self._on_state(state, None, reset_wanted=True)
            self.toast("Applied — audio restarted")

        self.run_bg(work, done)

    def confirm_reset(self) -> None:
        dlg = Adw.AlertDialog(
            heading="Reset Clockwork?",
            body="This removes every setting Clockwork created and restarts the audio service. "
                 f"Previous files are backed up to {str(core.BACKUP_DIR).replace(str(Path.home()), '~', 1)}.",
        )
        dlg.add_response("cancel", "Cancel")
        dlg.add_response("reset", "Reset")
        dlg.set_response_appearance("reset", Adw.ResponseAppearance.DESTRUCTIVE)
        dlg.set_default_response("cancel")
        dlg.set_close_response("cancel")

        def on_response(_d, resp):
            if resp != "reset":
                return

            def work():
                core.reset_all()
                time.sleep(RESTART_SETTLE)
                return fetch_state()

            def done(state, err):
                if err:
                    self._error_dialog("Reset failed", str(err))
                    return
                self.testing = None
                self.test_banner.set_revealed(False)
                self._on_state(state, None, reset_wanted=True)
                self.toast("Back to system defaults")

            self.run_bg(work, done)

        dlg.connect("response", on_response)
        dlg.present(self)

    def _error_dialog(self, heading: str, body: str) -> None:
        dlg = Adw.AlertDialog(heading=heading, body=body)
        dlg.add_response("ok", "OK")
        dlg.present(self)

    # --------------------------------------------------------------- test
    def start_test(self, rate: int) -> None:
        if self.testing:
            return

        def done(_res, err):
            if err:
                self.toast(f"Couldn't switch: {err}")
                return
            self.testing, self.test_left = rate, TEST_SECONDS
            self._update_test_banner()
            self.test_banner.set_revealed(True)
            self.clock.update_status(self.state.get("clock", {}), rate)
            GLib.timeout_add_seconds(1, self._tick_test)

        self.run_bg(lambda: core.force_rate(rate), done, busy=False)

    def _update_test_banner(self) -> None:
        self.test_banner.set_title(
            f"Trying {core.short_rate(self.testing)} — back to normal in {self.test_left} s"
        )

    def _tick_test(self) -> bool:
        if not self.testing:
            return False
        self.test_left -= 1
        if self.test_left <= 0:
            self.stop_test()
            return False
        self._update_test_banner()
        return True

    def stop_test(self) -> None:
        if not self.testing:
            return
        self.testing = None
        self.test_banner.set_revealed(False)

        def done(_res, err):
            self.toast(f"Couldn't revert: {err}" if err else "Test finished — clock restored")
            self.clock.update_status(self.state.get("clock", {}), None)

        self.run_bg(lambda: core.force_rate(0), done, busy=False)

    def _on_close(self, _win) -> bool:
        if self.testing:
            try:
                core.force_rate(0)
            except core.ClockworkError:
                pass
        return False

    # -------------------------------------------------------------- about
    def show_about(self) -> None:
        Adw.AboutDialog(
            application_name="Clockwork",
            application_icon=APP_ID,
            version=__version__,
            developer_name="Diva Satria",
            comments="Pick the sample rate and bit depth of every speaker, headset and "
                     "microphone — the “Default Format” setting Windows has, for PipeWire.",
            license_type=Gtk.License.MIT_X11,
            website="https://github.com/divasatriaa/clockwork",
            issue_url="https://github.com/divasatriaa/clockwork/issues",
        ).present(self)


class App(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        GLib.set_application_name("Clockwork")
        quit_act = Gio.SimpleAction.new("quit", None)
        quit_act.connect("activate", lambda *_: self.quit())
        self.add_action(quit_act)

    def do_startup(self) -> None:
        Adw.Application.do_startup(self)
        display = Gdk.Display.get_default()
        Gtk.IconTheme.get_for_display(display).add_search_path(str(ICON_DIR))
        css = Gtk.CssProvider()
        css.load_from_string(CSS) if hasattr(css, "load_from_string") else css.load_from_data(CSS.encode())
        Gtk.StyleContext.add_provider_for_display(display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        Gtk.Window.set_default_icon_name(APP_ID)

    def do_activate(self) -> None:
        (self.props.active_window or Window(self)).present()


def main() -> int:
    return App().run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
