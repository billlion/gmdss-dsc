"""Explicit one-shot TX controls for the shared station.
SPDX-License-Identifier: GPL-3.0-or-later
"""
import asyncio
import argparse
import json
from dataclasses import asdict

from .ham_tx import TxRequest, TxSettings, check_allocation


class TxPanel:
    def __init__(self, service, initial, get_audio, get_position, devices):
        from nicegui import ui
        from .cli import device_argument
        self.fields = {}
        with ui.card().classes("dsc-card"):
            ui.label("Amateur transmission").classes("dsc-title")
            self.status = ui.label("Idle").classes("font-semibold")
            self.error = ui.label().classes("text-red-800 break-words").props('role="alert"')
            ui.label("One CW + DSC burst. PTT must be enabled in Radio connection before connecting.").classes("dsc-muted")
            self.fields["band_plan"] = ui.select({"": "TX disabled", "uk": "UK HF / 6 m allocations"},
                value=initial.band_plan, label="TX allocation plan").classes("w-full")
            self.fields["callsign"] = ui.input("TX amateur callsign", value=initial.callsign).classes("w-full")
            self.fields["output_device"] = ui.input("Radio output device",
                value=str(initial.output_device) if initial.output_device is not None else "").classes("w-full")
            ui.button("List output devices", on_click=devices).props("flat no-caps")
            with ui.expansion("TX audio settings").classes("w-full"):
                for name, label in (("output_channel", "Output channel"), ("level", "Audio level (0.001–1)"),
                                    ("lead_s", "PTT lead (seconds)"), ("tail_s", "PTT tail (seconds)")):
                    self.fields[name] = ui.number(label, value=getattr(initial, name)).classes("w-full")
                ui.label("Output channel is within the selected audio device: usually 1 for mono/left or 2 for right. It is not the device number.").classes("dsc-muted")
            self.kind = ui.select({"test": "Test call", "position": "Position acknowledgement"},
                                  value="test", label="TX message").classes("w-full")
            self.source = ui.input("TX source address (9 digits)").classes("w-full")
            self.destination = ui.input("TX destination address (9 digits)").classes("w-full")
            self.frequency = ui.number("TX DSC centre (kHz)", value=None, format="%.3f").classes("w-full")
            self.locator = ui.input("TX CW locator (optional; auto for position)").classes("w-full")
            self.preview = ui.label("Enter an amateur frequency; the RX preset is not used.").classes("dsc-muted")
            ui.label("Position messages read a fresh fix using Own position settings. Transmission stays on the selected amateur frequency.").classes("dsc-muted")

            def request():
                if self.frequency.value is None:
                    raise ValueError("Enter a TX DSC centre frequency in kHz")
                return TxRequest(kind=self.kind.value, source=self.source.value.strip(),
                    destination=self.destination.value.strip(), frequency_hz=self.frequency.value*1000,
                    centre_hz=get_audio().centre_hz, settings=self.settings(),
                    locator=self.locator.value.strip(), position=get_position())

            async def send():
                try:
                    job = request()
                    await asyncio.to_thread(service.transmit, job)
                except Exception as exc:
                    ui.notify(str(exc), type="negative", timeout=10000)
                self.refresh(service.snapshot(), get_audio)

            self.request = request
            self.readiness = ui.label().classes("whitespace-pre-line break-words w-full").props(
                'role="status" aria-live="polite"')
            with ui.row().classes("w-full"):
                self.send = ui.button("Transmit once", on_click=send, color="negative").props("no-caps")
                self.abort = ui.button("Abort TX", on_click=service.abort_tx, color="negative").props("outline no-caps")

            def details():
                result = service.snapshot()["tx_result"]
                with ui.dialog() as dialog, ui.card().classes("w-full max-w-2xl"):
                    ui.code(json.dumps(result, indent=2), language="json").classes("w-full")
                    ui.button("Close", on_click=dialog.close)
                dialog.open()
            self.result = ui.button("Last TX JSON", on_click=details).props("flat no-caps")
        self.device_argument = device_argument

    def settings(self):
        values = {name: widget.value for name, widget in self.fields.items()}
        channel = values["output_channel"]
        if channel is None or int(channel) != channel:
            raise ValueError("Output channel must be an integer")
        values["output_channel"] = int(channel)
        device = values["output_device"].strip()
        values["output_device"] = self.device_argument(device) if device else None
        settings = TxSettings(**values)
        settings.validate()
        return settings

    def apply(self, settings):
        for name, value in asdict(settings).items():
            if name == "output_device":
                value = str(value) if value is not None else ""
            self.fields[name].set_value(value)

    def refresh(self, snapshot, get_audio):
        busy = snapshot["rig_busy"]
        self.status.set_text(snapshot["tx_state"])
        self.error.set_text(snapshot["tx_error"])
        reasons = [snapshot["tx_block_reason"]] if snapshot["tx_block_reason"] else []
        try:
            settings = self.settings()
            if self.frequency.value is None:
                raise ValueError("Enter a TX DSC centre frequency in kHz")
            plan = check_allocation(self.frequency.value*1000, get_audio().centre_hz, settings.band_plan)
            self.preview.set_text(f"USB dial: {plan['dial_hz']/1000:.3f} kHz · "
                                  f"checked window to {plan['checked_window_hz'][1]/1000:.3f} kHz")
        except (ValueError, TypeError, argparse.ArgumentTypeError) as exc:
            self.preview.set_text(str(exc) if self.frequency.value is not None else "Enter an amateur TX frequency.")
        try:
            self.request().validate()
        except (ValueError, TypeError, argparse.ArgumentTypeError) as exc:
            reasons.append(str(exc))
        self.readiness.set_text("TX unavailable:\n" + "\n".join(reasons) if reasons else
                                "Ready. Audio and radio checks run when you press Transmit once.")
        self.send.set_enabled(not reasons)
        self.abort.set_enabled(snapshot["tx_active"])
        self.result.set_enabled(snapshot["tx_result"] is not None)
        for widget in (*self.fields.values(), self.kind, self.source, self.destination, self.frequency, self.locator):
            widget.set_enabled(not busy)
