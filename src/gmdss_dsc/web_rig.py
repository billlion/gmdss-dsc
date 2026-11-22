"""Browser controls for the one shared RX radio and named station profiles.

SPDX-License-Identifier: GPL-3.0-or-later
"""
import asyncio
from dataclasses import asdict

from .profiles import DSC_FREQUENCIES, StationProfile
from .rig import RigSettings, list_models, usb_dial


class RigPanel:
    def __init__(self, service, store, profile_name, get_audio, set_audio,
                 get_position, set_position):
        from nicegui import ui
        self.service, self.store = service, store
        self.get_audio, self.set_audio = get_audio, set_audio
        self.get_position, self.set_position = get_position, set_position
        self.presets = list(DSC_FREQUENCIES)
        self.fields = {}
        profiles, _ = store.read()
        initial = profiles[profile_name] if profile_name else StationProfile()
        station = service.snapshot()["station"]
        if station:
            profile_name = station["name"]
            initial = StationProfile.from_dict(station["profile"])
            profiles.setdefault(profile_name, initial)

        def error(exc):
            ui.notify(str(exc), type="negative", timeout=10000)

        def load():
            try:
                profile = store.get(self.names.value)
                service.configure(profile.audio, station=dict(name=self.names.value, profile=profile.to_dict()))
                self.apply(profile)
                self.name.set_value(self.names.value)
                ui.notify("Station loaded. Connect the radio when ready.")
            except Exception as exc:
                error(exc)

        def save():
            try:
                profile = StationProfile(radio=self.radio.value, rig=self.settings(),
                    audio=get_audio(), frequency_hz=self.frequency.value * 1000,
                    presets_hz=self.presets, position=get_position())
                store.save(self.name.value.strip(), profile)
                self.names.set_options(list(store.read()[0]), value=self.name.value.strip())
                ui.notify("Station saved")
            except Exception as exc:
                error(exc)

        with ui.card().classes("dsc-card"):
            ui.label("Station profile").classes("dsc-title")
            self.names = ui.select(list(profiles), value=profile_name, label="Saved station").classes("w-full")
            self.load_button = ui.button("Load station", on_click=load).props("outline no-caps")
            with ui.expansion("Save station settings", icon="save").classes("w-full"):
                self.name = ui.input("Profile name", value=profile_name or "home").classes("w-full")
                self.save_button = ui.button("Save station", on_click=save).props("no-caps")
                ui.label(str(store.path)).classes("dsc-muted break-all")

        with ui.card().classes("dsc-card"):
            ui.label("Radio control").classes("dsc-title")
            self.status = ui.label("Disconnected").classes("font-semibold")
            self.detail = ui.label().classes("dsc-muted whitespace-pre-line")
            self.error = ui.label().classes("text-red-800 break-words")
            self.radio = ui.input("Radio description", value=initial.radio).classes("w-full")
            with ui.expansion("Radio connection", icon="settings").classes("w-full"):
                self.connection = ui.select({"managed": "Start a local rigctld", "external": "Use an existing rigctld"},
                    value=initial.rig.connection, label="Connection").classes("w-full")

                def field(name, label, numeric=False):
                    factory = ui.number if numeric else ui.input
                    value = getattr(initial.rig, name)
                    if name == "civ_address":
                        value = f"0x{value:02x}" if value is not None else ""
                    elif value is None:
                        value = ""
                    widget = factory(label, value=value).classes("w-full")
                    self.fields[name] = widget

                with ui.column().classes("w-full").bind_visibility_from(
                        self.connection, "value", backward=lambda v: v == "managed"):
                    field("model", "Hamlib model number", True)
                    field("device", "Radio serial port (COM3, /dev/ttyUSB0, …)")
                    field("baud", "Radio baud rate", True)
                    field("remote_id", "Marine Remote ID (decimal; optional)")
                    field("civ_address", "CI-V address (e.g. 0x88; optional)")
                    field("executable", "rigctld executable or full path")

                    async def models():
                        try:
                            text = await asyncio.to_thread(list_models, self.fields["executable"].value)
                            with ui.dialog() as dialog, ui.card().classes("w-full max-w-3xl"):
                                ui.label("Installed Hamlib radio models").classes("dsc-title")
                                ui.label(text).classes("dsc-detail text-xs max-h-96 overflow-auto w-full")
                                ui.button("Close", on_click=dialog.close)
                            dialog.open()
                        except Exception as exc:
                            error(exc)
                    ui.button("List radio models", on_click=models).props("flat no-caps")
                    ui.label("IC-7100: 3070 · IC-M802: 30002 · Dummy: 1").classes("dsc-muted")

                    async def ports():
                        from .position import serial_ports
                        try:
                            values = await asyncio.to_thread(serial_ports)
                            with ui.dialog() as dialog, ui.card().classes("w-full max-w-2xl"):
                                ui.label("Serial ports on the server").classes("dsc-title")
                                import json
                                ui.label(json.dumps(values, indent=2)).classes("dsc-detail break-all")
                                ui.button("Close", on_click=dialog.close)
                            dialog.open()
                        except Exception as exc:
                            error(exc)
                    ui.button("List serial ports", on_click=ports).props("flat no-caps")
                with ui.column().classes("w-full").bind_visibility_from(
                        self.connection, "value", backward=lambda v: v == "external"):
                    field("host", "rigctld host")
                    field("port", "rigctld port", True)
                    ui.label("The existing daemon owns the radio model, serial port and address.").classes("dsc-muted")
                field("timeout_s", "Radio timeout (seconds)", True)
                field("settle_s", "Settling time (seconds)", True)

            async def operation(action):
                try:
                    kwargs = {}
                    if action == "connect":
                        kwargs["settings"] = self.settings()
                    if action == "tune":
                        kwargs.update(frequency_hz=self.frequency.value * 1000,
                                      centre_hz=get_audio().centre_hz)
                    await asyncio.to_thread(service.radio, action, **kwargs)
                except Exception as exc:
                    error(exc)
                self.refresh(service.snapshot())

            with ui.row().classes("w-full"):
                self.connect_button = ui.button("Connect radio", on_click=lambda: operation("connect")).props("no-caps")
                self.disconnect_button = ui.button("Disconnect radio", on_click=lambda: operation("disconnect")).props("outline no-caps")
                self.read_button = ui.button("Read radio", on_click=lambda: operation("read")).props("flat no-caps")
            self.frequency = ui.number("DSC frequency (kHz)", value=initial.frequency_hz / 1000,
                                       format="%.3f", on_change=lambda _: self.preview()).classes("w-full")
            self.preset = ui.select({f / 1000: f"{f/1000:g} kHz" for f in initial.presets_hz},
                label="Frequency preset", on_change=lambda e: self.frequency.set_value(e.value)).classes("w-full")
            self.dial = ui.label().classes("dsc-muted")
            self.tune_button = ui.button("Tune USB", on_click=lambda: operation("tune")).props("no-caps")
            ui.label("Retuning pauses reception and starts a fresh decoder window. Readback is checked before reception resumes.").classes("dsc-muted")
        self.presets = list(initial.presets_hz)

    def settings(self):
        values = {name: widget.value for name, widget in self.fields.items()}
        for name in ("model", "baud", "port"):
            if values[name] is None or int(values[name]) != values[name]:
                raise ValueError(f"{name} must be an integer")
            values[name] = int(values[name])
        values["remote_id"] = int(str(values["remote_id"]).strip()) if str(values["remote_id"]).strip() else None
        address = str(values["civ_address"]).strip()
        values["civ_address"] = int(address, 16 if address.lower().startswith("0x") else 10) if address else None
        settings = RigSettings(connection=self.connection.value, **values)
        settings.validate()
        return settings

    def apply(self, profile):
        self.radio.set_value(profile.radio)
        self.connection.set_value(profile.rig.connection)
        for name, value in asdict(profile.rig).items():
            if name in self.fields:
                if name == "civ_address" and value is not None:
                    value = f"0x{value:02x}"
                self.fields[name].set_value(value if value is not None else "")
        self.presets = list(profile.presets_hz)
        self.preset.set_options({f/1000: f"{f/1000:g} kHz" for f in self.presets}, value=None)
        self.frequency.set_value(profile.frequency_hz / 1000)
        self.set_audio(profile.audio)
        self.set_position(profile.position)

    def preview(self):
        if not hasattr(self, "dial"):
            return
        try:
            centre = self.get_audio().centre_hz
            dial = usb_dial(self.frequency.value * 1000, centre)
            self.dial.set_text(f"USB dial: {dial/1000:g} kHz · audio centre: {centre:g} Hz")
        except (ValueError, TypeError):
            self.dial.set_text("Enter a DSC frequency and an explicit audio centre to tune.")

    def refresh(self, snapshot):
        connected, busy = snapshot["rig_connected"], snapshot["rig_busy"]
        self.status.set_text("Working…" if busy else "Connected" if connected else "Disconnected")
        state = snapshot["rig"]
        self.detail.set_text((f"Last readback: {state['dial_hz']/1000:g} kHz {state['mode']}\n"
                              f"{state['confirmed_at_utc']}") if state else "")
        self.error.set_text(snapshot["rig_error"])
        self.connect_button.set_enabled(not connected and not busy)
        for button in (self.disconnect_button, self.read_button, self.tune_button):
            button.set_enabled(connected and not busy)
        for widget in (self.connection, self.radio, *self.fields.values()):
            widget.set_enabled(not connected and not busy)
        self.load_button.set_enabled(not connected and not busy and snapshot["state"] not in ("Receiving", "Starting", "Stopping"))
        self.save_button.set_enabled(not busy)
        self.preview()
