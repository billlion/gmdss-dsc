"""Optional NiceGUI front end. Audio and position I/O stay on the server.

SPDX-License-Identifier: GPL-3.0-or-later
"""
import asyncio
import argparse
import json
import os
import sys

from .cli import centre_argument, device_argument, position_arguments
from .live import AudioInputError, list_devices
from .receiver import ReceiverService, ReceiverSettings
from .web_access import AccessGuard, access_password
from .profiles import ProfileStore, StationProfile
from .web_rig import RigPanel
from .web_tx import TxPanel


def build_page(service, position_defaults, store=None, profile_name=None):
    # Imported only by 'serve'; the codec and CLI do not depend on NiceGUI.
    from nicegui import ui
    station = service.snapshot()["station"]
    if station:
        defaults = argparse.ArgumentParser()
        position_arguments(defaults)
        position_defaults = vars(defaults.parse_args([])) | station["profile"]["position"]

    ui.colors(primary="#153f50", secondary="#be891d", positive="#267663", negative="#aa3944")
    ui.add_css("""
        body { background: #f3f6f6; color: #18333e; }
        .nicegui-content { padding: 0; gap: 0; }
        .dsc-shell { width: 100%; max-width: 1152px; margin: auto; padding: 24px; }
        .dsc-grid { display: grid; grid-template-columns: minmax(280px, 340px) minmax(0, 1fr);
                    gap: 20px; align-items: start; }
        .dsc-card { width: 100%; border: 1px solid #dce5e7; border-radius: 12px;
                    box-shadow: none; padding: 20px; }
        .dsc-muted { color: #546b75; font-size: 13px; }
        .dsc-title { font-size: 19px; font-weight: 600; }
        .dsc-detail { white-space: pre-wrap; overflow-wrap: anywhere; font-family: monospace; }
        .dsc-table .q-table th, .dsc-table .q-table td { padding: 10px 8px; white-space: normal; }
        @media (max-width: 760px) {
            .dsc-shell { padding: 14px; }
            .dsc-grid { grid-template-columns: minmax(0, 1fr); gap: 14px; }
            .dsc-grid > :last-child { order: -1; }
            .dsc-card { padding: 16px; }
        }
    """)
    with ui.header().classes("items-center justify-between").style("background: #153f50; padding: 14px 24px"):
        with ui.column().classes("gap-0"):
            ui.label("DSC station").classes("text-xl font-semibold")
            ui.label("MF / HF · local station").classes("text-xs opacity-80")
        with ui.row().classes("items-center gap-3"):
            ui.label("RX / AMATEUR TX").classes("text-xs tracking-widest").style("color: #f1ce7c")
            status = ui.badge("Stopped", color="grey")
            header_abort = ui.button("Abort transmission", on_click=service.abort_tx,
                                     color="negative").props("no-caps")

    def notify_error(exc):
        ui.notify(str(exc), type="negative", timeout=8000)

    async def devices():
        try:
            text = await asyncio.to_thread(list_devices)
        except (AudioInputError, OSError) as exc:
            notify_error(exc)
            return
        with ui.dialog() as dialog, ui.card().classes("w-full max-w-2xl"):
            ui.label("Audio devices on the server").classes("dsc-title")
            ui.label("Enter the ID or unique name of the required input or output device.").classes("dsc-muted")
            ui.label(text or "No audio devices found.").classes("dsc-detail")
            ui.button("Close", on_click=dialog.close).props("flat")
        dialog.open()

    with ui.column().classes("dsc-shell gap-4"):
        ui.label("One receiver, shared across your browsers. Reception continues when this page closes.").classes("dsc-muted")
        with ui.element("div").classes("dsc-grid w-full"):
            with ui.column().classes("w-full gap-4"):
                with ui.card().classes("dsc-card"):
                    with ui.row().classes("w-full items-center justify-between"):
                        ui.label("Reception").classes("dsc-title")
                    active_settings = ui.label().classes("dsc-muted")
                    error = ui.label().classes("text-red-800 break-words w-full").props('role="alert"')
                    initial = service.snapshot()["settings"]
                    device = ui.input("Input device", value=str(initial["device"]) if initial["device"] is not None else "",
                                      placeholder="System default").classes("w-full")
                    ui.button("List audio devices", on_click=devices).props("flat dense no-caps")
                    with ui.row().classes("w-full flex-nowrap"):
                        rate = ui.number("Sample rate (Hz)", value=initial["sample_rate"], format="%.0f").classes("w-1/2")
                        channel = ui.number("Input channel", value=initial["channel"], format="%.0f").classes("w-1/2")
                    centre = ui.input("Audio centre (Hz or auto)",
                                      value=str(initial["centre_hz"]) if initial["centre_hz"] is not None else "auto").classes("w-full")
                    ui.label("Usually 1700 Hz for USB tuned 1.7 kHz below the DSC frequency.").classes("dsc-muted")

                    def audio_settings():
                        if rate.value is None or channel.value is None:
                            raise ValueError("Enter a sample rate and channel")
                        if int(rate.value) != rate.value or int(channel.value) != channel.value:
                            raise ValueError("Sample rate and channel must be integers")
                        settings = ReceiverSettings(
                            device=device_argument(device.value.strip()) if device.value.strip() else None,
                            sample_rate=int(rate.value), channel=int(channel.value),
                            centre_hz=centre_argument(centre.value.strip()))
                        settings.validate()
                        return settings

                    def set_audio(settings):
                        device.set_value(str(settings.device) if settings.device is not None else "")
                        rate.set_value(settings.sample_rate)
                        channel.set_value(settings.channel)
                        centre.set_value(str(settings.centre_hz) if settings.centre_hz is not None else "auto")

                    def start():
                        try:
                            service.start(audio_settings())
                        except (ValueError, TypeError, argparse.ArgumentTypeError) as exc:
                            notify_error(exc)
                        refresh()

                    def stop():
                        try:
                            service.stop()
                        except ValueError as exc:
                            notify_error(exc)
                        refresh()

                    with ui.row().classes("w-full"):
                        start_button = ui.button("Start reception", on_click=start).props("no-caps")
                        stop_button = ui.button("Stop", on_click=stop).props("outline no-caps")

                # Callbacks are evaluated after the position form below exists.
                rig_panel = RigPanel(service, store or ProfileStore(), profile_name,
                                     audio_settings, set_audio,
                                     lambda: position_settings(), lambda values: set_position(values),
                                     lambda: tx_panel.settings(), lambda values: tx_panel.apply(values))

                with ui.card().classes("dsc-card"):
                    ui.label("Own position").classes("dsc-title")
                    position_status = ui.label("No fix read").classes("font-semibold")
                    position_text = ui.label().classes("dsc-muted whitespace-pre-line")
                    with ui.expansion("Position source", icon="settings").classes("w-full"):
                        source = ui.select({"manual": "Manual", "serial": "Serial NMEA",
                                            "gpsd": "gpsd", "signalk": "Signal K"},
                                           value=position_defaults["position_source"], label="Source").classes("w-full")
                        fields = {}

                        def field(name, label, numeric=False):
                            factory = ui.number if numeric else ui.input
                            fields[name] = factory(label, value=position_defaults[name]).classes("w-full")

                        with ui.column().classes("w-full").bind_visibility_from(source, "value", backward=lambda s: s == "manual"):
                            field("latitude", "Latitude (north positive)", True)
                            field("longitude", "Longitude (east positive)", True)
                            field("position_time", "Fix UTC (blank = now)")
                        with ui.column().classes("w-full").bind_visibility_from(source, "value", backward=lambda s: s == "serial"):
                            field("serial_port", "Serial port (COM3, /dev/ttyUSB0, …)")
                            field("baud", "Baud rate", True)
                        with ui.column().classes("w-full").bind_visibility_from(source, "value", backward=lambda s: s == "gpsd"):
                            field("gpsd_host", "gpsd host")
                            field("gpsd_port", "gpsd port", True)
                        with ui.column().classes("w-full").bind_visibility_from(source, "value", backward=lambda s: s == "signalk"):
                            field("signalk_url", "Signal K server URL")
                        field("position_timeout", "Read timeout (seconds)", True)
                        field("max_fix_age", "Maximum fix age (seconds)", True)

                    def position_settings():
                        settings = {k: v.value for k, v in fields.items()}
                        settings["position_source"] = source.value
                        if source.value != "manual":
                            for key in ("latitude", "longitude", "position_time"):
                                settings[key] = None
                        settings["position_time"] = settings["position_time"] or None
                        for key in ("baud", "gpsd_port"):
                            if settings[key] is None or int(settings[key]) != settings[key]:
                                raise ValueError("Baud rate and port must be integers")
                            settings[key] = int(settings[key])
                        return settings

                    def set_position(values):
                        defaults = argparse.ArgumentParser()
                        position_arguments(defaults)
                        settings = vars(defaults.parse_args([])) | values
                        source.set_value(settings["position_source"])
                        for key, widget in fields.items():
                            widget.set_value(settings[key])

                    def read_position():
                        try:
                            service.read_position(position_settings())
                        except (ValueError, TypeError) as exc:
                            notify_error(exc)
                        refresh()

                    position_button = ui.button("Read position", on_click=read_position).props("outline no-caps")
                    ui.label("Reads one fix on the server. The displayed fix ages until you read again.").classes("dsc-muted")

            with ui.column().classes("w-full gap-4 min-w-0"):
                with ui.card().classes("dsc-card"):
                    with ui.row().classes("w-full items-center justify-between"):
                        ui.label("Received messages").classes("dsc-title")
                        count = ui.label("0 decoded").classes("dsc-muted")
                    ui.label("Complete frames passing ECC · latest 200 kept in memory").classes("dsc-muted")
                    table = ui.table(columns=[
                        {"name": "time", "label": "UTC", "field": "time", "align": "left"},
                        {"name": "message", "label": "Message", "field": "kind", "align": "left"},
                        {"name": "detail", "label": "", "field": "id"},
                    ], rows=[], row_key="id", pagination=10).classes("w-full dsc-table").props("flat")
                    table.add_slot("body-cell-message", """
                        <q-td :props="props">
                            <div class="text-weight-medium">{{ props.row.kind }}</div>
                            <div>{{ props.row.source }} → {{ props.row.destination }}</div>
                        </q-td>
                    """)
                    table.add_slot("body-cell-detail", """
                        <q-td :props="props">
                            <q-btn flat dense label="JSON" @click="$parent.$emit('details', props.row.id)" />
                        </q-td>
                    """)

                    def details(event):
                        record = next((r for r in service.snapshot()["messages"] if r["id"] == event.args), None)
                        if record is None:
                            ui.notify("This message has left the retained history.")
                            return
                        with ui.dialog() as dialog, ui.card().classes("w-full max-w-2xl"):
                            ui.label("Decoded message").classes("dsc-title")
                            ui.code(json.dumps(record, indent=2), language="json").classes("w-full")
                            ui.button("Close", on_click=dialog.close).props("flat")
                        dialog.open()

                    table.on("details", details)
                    ui.button("Download JSONL", on_click=lambda: ui.download.content(
                        service.jsonl(), "dsc-messages.jsonl", media_type="application/x-ndjson")).props("flat no-caps")
                    ui.label("History lasts until the server exits. Download it to keep a copy.").classes("dsc-muted")
                initial_profile = (StationProfile.from_dict(station["profile"]) if station else
                                   store.get(profile_name) if profile_name and store else StationProfile())
                tx_panel = TxPanel(service, initial_profile.tx, audio_settings, position_settings, devices)
                with ui.expansion("Receiver activity", icon="list").classes("w-full"):
                    activity = ui.label().classes("dsc-detail text-xs w-full")

    last_count = None

    def refresh():
        nonlocal last_count
        snapshot = service.snapshot()
        state = snapshot["state"]
        status.set_text("TX active" if snapshot["tx_active"] else
                        "PTT unconfirmed" if snapshot["ptt_uncertain"] else state)
        header_abort.set_visibility(snapshot["tx_active"])
        status.props("color=" + ("negative" if snapshot["tx_active"] or snapshot["ptt_uncertain"] else
                                {"Receiving": "positive", "Error": "negative",
                                 "Starting": "secondary", "Stopping": "secondary"}.get(state, "grey"))
                     )
        error.set_text(snapshot["error"])
        running = state in ("Starting", "Receiving", "Stopping")
        start_button.set_enabled(not running and not snapshot["rig_busy"] and not snapshot["ptt_uncertain"])
        stop_button.set_enabled(running and state != "Stopping" and not snapshot["rig_busy"])
        for widget in (device, rate, channel, centre):
            widget.set_enabled(not running and not snapshot["rig_busy"])
        current = snapshot["settings"]
        if running and not snapshot["rig_busy"]:
            set_audio(ReceiverSettings(**current))
        rig_panel.refresh(snapshot)
        tx_panel.refresh(snapshot, audio_settings)
        active_settings.set_text(f"Device: {current['device'] if current['device'] is not None else 'system default'} · "
                                 f"{current['sample_rate']} Hz · channel {current['channel']} · "
                                 f"centre {current['centre_hz'] if current['centre_hz'] is not None else 'auto'}")
        if last_count != snapshot["count"]:
            rows = []
            for record in reversed(snapshot["messages"]):
                call = record["test_call"] or record["position_report"] or {}
                kind = ("Test ACK" if call.get("acknowledgement") else "Test") if record["test_call"] else (
                    "Position" if record["position_report"] else "DSC")
                rows.append(dict(id=record["id"], time=record["decoded_at_utc"][11:19], kind=kind,
                                 source=call.get("source", "—"), destination=call.get("destination", "—")))
            table.rows = rows
            count.set_text(f"{snapshot['count']} decoded")
            last_count = snapshot["count"]
        activity.set_text("\n".join(snapshot["events"]) or "No receiver activity yet.")
        position_button.set_enabled(not snapshot["position_pending"] and not snapshot["rig_busy"])
        fix = snapshot["position"]
        if snapshot["position_pending"]:
            position_status.set_text("Reading position…")
            position_text.set_text("")
        elif fix:
            position_status.set_text(("Manual fix" if fix["source"] == "manual" else "Position fix") +
                                     (" · fresh" if fix["fresh"] else " · STALE"))
            position_text.set_text(f"{fix['latitude']:.5f}, {fix['longitude']:.5f} · {fix['locator']}\n"
                                   f"{fix['source']} · {max(0, fix['age_s']):.0f} s old\n{fix['time_utc']}")
        else:
            position_status.set_text("No usable fix" if snapshot["position_error"] else "No fix read")
            position_text.set_text(snapshot["position_error"])

    refresh()
    ui.timer(0.5, refresh)


def configure_app(service, position_defaults, password, store=None, profile_name=None):
    from nicegui import app, ui
    app.add_middleware(AccessGuard, password=password)
    if profile_name:
        profile = store.get(profile_name)
        saved = profile.to_dict()
        saved["position"] = {k: v for k, v in position_defaults.items() if k in (
            "position_source", "latitude", "longitude", "position_time", "serial_port", "baud",
            "gpsd_host", "gpsd_port", "signalk_url", "position_timeout", "max_fix_age")}
        service.configure(profile.audio, station=dict(name=profile_name, profile=saved))

    @ui.page("/")
    def index():
        build_page(service, position_defaults, store, profile_name)

    async def shutdown():
        await asyncio.to_thread(service.close)

    app.on_shutdown(shutdown)


def serve(args):
    try:
        from nicegui import ui
    except ImportError as exc:
        raise ValueError('Browser interface requires: python -m pip install -e ".[web]"') from exc
    if not 1 <= args.port <= 65535:
        raise ValueError("Web port must be from 1 to 65535")
    password, generated = access_password(args.host, os.environ.get("DSC_WEB_PASSWORD"))
    if password:
        print("Browser login username: dsc", file=sys.stderr)
        print(f"Password for this run: {password}" if generated else "Password: from DSC_WEB_PASSWORD",
              file=sys.stderr)
    print(f"DSC server listening on {args.host}:{args.port}. Ctrl+C stops the server.", file=sys.stderr)
    store = ProfileStore(args.config)
    profiles, default = store.read()
    name = args.profile or default
    profile = store.get(name) if name else None
    defaults = argparse.ArgumentParser()
    position_arguments(defaults)
    position_defaults = vars(defaults.parse_args([]))
    if profile:
        position_defaults.update(profile.position)
    position_defaults.update({k: v for k, v in vars(args).items() if k in position_defaults})
    service = ReceiverService(settings=profile.audio if profile else None)
    configure_app(service, position_defaults, password, store, name)
    ui.run(host=args.host, port=args.port, title="DSC receiver", reload=False, show=False,
           fastapi_docs=False, on_air=False, proxy_headers=False)
    return 0
