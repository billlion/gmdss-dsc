"""WAV and live soundcard interfaces. SPDX-License-Identifier: GPL-3.0-or-later"""
import argparse
from dataclasses import asdict, replace
import json
from pathlib import Path
import sys
from .calls import TestCall, PositionReport
from .framing import encode_frame
from .modem import decode_wav, modulate, write_wav
from .live import AudioInputError
from .records import result_dict
from .position import get_fix, maidenhead
from .transmit import identification, with_identification, play_audio
from .rig import RigController, RigError, list_models


def device_argument(value):
    try:
        return int(value)
    except ValueError:
        return value


def centre_argument(value):
    if value.lower() == "auto":
        return None
    try:
        return float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Centre must be a frequency in Hz or 'auto'") from exc


def position_arguments(parser, *, suppress_defaults=False):
    before = len(parser._actions)
    parser.add_argument("--position-source", choices=("manual", "serial", "gpsd", "signalk"),
                        default="manual", help="Position provider (default: manual)")
    parser.add_argument("--latitude", type=float, help="Manual decimal degrees, north positive")
    parser.add_argument("--longitude", type=float, help="Manual decimal degrees, east positive")
    parser.add_argument("--position-time", help="Manual fix ISO UTC time; default: now")
    parser.add_argument("--serial-port", help="NMEA device, e.g. COM3 or /dev/ttyUSB0")
    parser.add_argument("--baud", type=int, default=4800, help="NMEA baud rate (default: 4800)")
    parser.add_argument("--gpsd-host", default="localhost")
    parser.add_argument("--gpsd-port", type=int, default=2947)
    parser.add_argument("--signalk-url", default="http://localhost:3000", help="Signal K server root URL")
    parser.add_argument("--position-timeout", type=float, default=10.0, help="Source timeout in seconds")
    parser.add_argument("--max-fix-age", type=float, default=30.0, help="Maximum position age in seconds")
    if suppress_defaults:
        for action in parser._actions[before:]:
            action.default = argparse.SUPPRESS


def profile_arguments(parser):
    parser.add_argument("--config", type=Path, help="Station TOML path (default: per-user configuration)")
    parser.add_argument("--profile", help="Saved station profile, e.g. home or tui")


def receive(args):
    from .live import listen
    from .profiles import ProfileStore
    from .receiver import ReceiverSettings
    profile = ProfileStore(args.config).get(args.profile) if args.profile else None
    values = asdict(profile.audio if profile else ReceiverSettings())
    for option, key in (("device", "device"), ("sample_rate", "sample_rate"),
                        ("channel", "channel"), ("centre", "centre_hz")):
        if hasattr(args, option):
            values[key] = getattr(args, option)
    ReceiverSettings(**values).validate()
    if args.frequency_khz is not None and profile is None:
        raise ValueError("--frequency-khz requires a station --profile for rig control")
    rig = RigController()
    status = None
    try:
        if profile:
            rig.connect(profile.rig)
            frequency = args.frequency_khz * 1000 if args.frequency_khz is not None else profile.frequency_hz
            status = rig.tune(frequency, values["centre_hz"])
            print(f"Radio confirmed: {status['dial_hz']/1000:.3f} kHz USB; "
                  f"DSC centre {status['frequency_hz']/1000:.3f} kHz", file=sys.stderr)

        def emit(result, segment, decoded_at):
            record = result_dict(result)
            record.update(capture_segment=segment, decoded_at_utc=decoded_at)
            if status is not None:
                record["rig"] = status
            print(json.dumps(record, allow_nan=False), flush=True)

        listen(emit=emit, log=lambda message: print(message, file=sys.stderr, flush=True),
               **values, window_s=args.window, interval_s=args.interval)
    finally:
        rig.close()
    return 0


def rig_command(args):
    from .profiles import ProfileStore
    if not args.profile:
        raise ValueError("Choose a station with --profile")
    profile = ProfileStore(args.config).get(args.profile)
    if args.action == "status" and args.frequency_khz is not None:
        raise ValueError("--frequency-khz is only used with 'rig tune'")
    rig = RigController()
    try:
        status = rig.connect(profile.rig)
        if args.action == "tune":
            frequency = args.frequency_khz * 1000 if args.frequency_khz is not None else profile.frequency_hz
            status = rig.tune(frequency, profile.audio.centre_hz)
        print(json.dumps(status, indent=2, allow_nan=False))
    finally:
        rig.close()
    return 0


def encoder_arguments(parser):
    parser.add_argument("output", type=Path, nargs="?", help="WAV path; optional with --play")
    parser.add_argument("--source", required=True, help="Nine-digit MMSI")
    parser.add_argument("--destination", required=True, help="Nine-digit MMSI")
    parser.add_argument("--centre", type=float, default=1700.0, help="Audio centre frequency in Hz")
    parser.add_argument("--sample-rate", type=int, default=8000, help="Synthesis/WAV rate (default: 8000)")
    parser.add_argument("--callsign", help="Prepend this amateur callsign in CW")
    parser.add_argument("--locator", help="CW Maidenhead locator, or 'auto' with encode-position")
    parser.add_argument("--cw-wpm", type=float, default=18.0, help="CW words per minute (default: 18)")
    parser.add_argument("--play", action="store_true", help="Also play once to a soundcard (no PTT)")
    parser.add_argument("--device", type=device_argument, help="Output device ID or unique name substring")
    parser.add_argument("--output-channel", type=int, default=1, help="One-based output channel (default: 1)")


def encode(args):
    if args.output is None and not args.play:
        raise ValueError("Specify a WAV output path, --play, or both")
    if not args.play and (args.device is not None or args.output_channel != 1):
        raise ValueError("--device/--output-channel require --play")
    # Validate identification before waiting on a position source.
    auto_locator = args.locator is not None and args.locator.lower() == "auto"
    if auto_locator and args.command != "encode-position":
        raise ValueError("--locator auto requires encode-position; use an explicit locator for encode-test")
    if auto_locator and args.callsign is None:
        raise ValueError("--locator requires --callsign")
    cw = identification(args.callsign, None if auto_locator else args.locator)
    fix = None
    if args.command == "encode-position":
        fix = get_fix(args)
        call = PositionReport(args.source, args.destination, fix.latitude, fix.longitude,
                              fix.time_utc.hour, fix.time_utc.minute)
        key = "position_report"
        if auto_locator:
            cw = identification(args.callsign, maidenhead(fix.latitude, fix.longitude))
    else:
        call = TestCall(args.source, args.destination, args.ack)
        key = "test_call"
    frame = call.to_frame()
    bits = encode_frame(frame, dot_bits=call.dot_bits)
    audio = modulate(bits, sample_rate=args.sample_rate, centre_hz=args.centre)
    audio = with_identification(audio, cw, sample_rate=args.sample_rate,
                                centre_hz=args.centre, wpm=args.cw_wpm)
    if fix is not None:
        fix.require_fresh(args.max_fix_age)
    if args.output is not None:
        write_wav(args.output, audio, args.sample_rate)
    if args.play:
        if fix is not None:
            fix.require_fresh(args.max_fix_age)
        print("Playing once; Ctrl+C stops playback. Radio PTT is not controlled.", file=sys.stderr)
        play_audio(audio, args.sample_rate, device=args.device, channel=args.output_channel)
    # Report the actual minute-resolution position encoded, as well as the fix.
    encoded_call = PositionReport.from_frame(frame) if fix is not None else call
    record = {"wav": str(args.output) if args.output else None, key: asdict(encoded_call),
              "ecc": frame.ecc, "dot_bits": call.dot_bits, "cw_text": cw,
              "played": args.play, "duration_s": len(audio)/args.sample_rate}
    if fix is not None:
        record["position_fix"] = fix.as_dict()
    print(json.dumps(record, indent=2, allow_nan=False))
    return 0


def transmit_command(args):
    from .profiles import ProfileStore
    from .ham_tx import TxRequest, transmit_once, POSITION_DEFAULTS
    if not args.profile:
        raise ValueError("Choose a station with --profile")
    profile = ProfileStore(args.config).get(args.profile)
    overrides = {key: getattr(args, option) for option, key in (
        ("band_plan", "band_plan"), ("callsign", "callsign"), ("device", "output_device"),
        ("output_channel", "output_channel"), ("level", "level")) if getattr(args, option) is not None}
    settings = replace(profile.tx, **overrides)
    position = profile.position | {k: v for k, v in vars(args).items() if k in POSITION_DEFAULTS}
    request = TxRequest(kind=args.command.removeprefix("transmit-"), source=args.source,
        destination=args.destination, frequency_hz=args.frequency_khz*1000, settings=settings,
        centre_hz=args.centre if args.centre is not None else profile.audio.centre_hz,
        locator=args.locator, cw_wpm=args.cw_wpm, acknowledgement=getattr(args, "ack", False),
        position=position)
    message = request.prepare()
    if args.dry_run:
        print(json.dumps(message.record | dict(transmitted=False, dry_run=True), indent=2, allow_nan=False))
        return 0
    if not profile.rig.allow_ptt:
        raise ValueError("Enable allow_ptt in this station's rig settings before transmitting")
    rig = RigController()
    try:
        rig.connect(profile.rig)
        result = transmit_once(rig, message,
            progress=lambda text: print(text, file=sys.stderr, flush=True))
        print(json.dumps(result, indent=2, allow_nan=False))
    finally:
        rig.close()
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="MF/HF DSC audio experiments")
    commands = parser.add_subparsers(dest="command", required=True)
    encoder = commands.add_parser("encode-test", help="Write an individual safety test call WAV")
    encoder_arguments(encoder)
    encoder.add_argument("--ack", action="store_true", help="Encode an acknowledgement (EOS 122)")
    report = commands.add_parser("encode-position", help="Encode a safety position acknowledgement (EOS 122)")
    encoder_arguments(report)
    position_arguments(report)
    for kind in ("test", "position"):
        tx = commands.add_parser("transmit-" + kind, help="Send one amateur CW + DSC burst with Hamlib PTT")
        profile_arguments(tx)
        tx.add_argument("--frequency-khz", required=True, type=float, help="DSC RF centre, NOT the USB dial")
        tx.add_argument("--source", required=True)
        tx.add_argument("--destination", required=True)
        tx.add_argument("--band-plan", choices=("uk",), help="Allocation boundaries; defaults to saved TX plan")
        tx.add_argument("--callsign", help="Required CW ID; defaults to saved TX callsign")
        tx.add_argument("--device", type=device_argument, help="Explicit radio audio output; defaults to saved TX device")
        tx.add_argument("--output-channel", type=int)
        tx.add_argument("--level", type=float, help="Audio amplitude multiplier, 0.001–1 (default: 0.25)")
        tx.add_argument("--centre", type=float)
        tx.add_argument("--locator", default="", help="CW locator; 'auto' for transmit-position")
        tx.add_argument("--cw-wpm", type=float, default=18)
        tx.add_argument("--dry-run", action="store_true", help="Validate and print the plan without opening radio/audio")
        if kind == "test":
            tx.add_argument("--ack", action="store_true")
        else:
            position_arguments(tx, suppress_defaults=True)
    position = commands.add_parser("position", help="Read one fresh position and print JSON; no audio")
    position_arguments(position)
    commands.add_parser("ports", help="List serial ports for GPS or radio control (gps extra)")
    decoder = commands.add_parser("decode", help="Find complete ECC-passing calls in a mono WAV")
    decoder.add_argument("input", type=Path)
    decoder.add_argument("--centre", type=float, default=None, help="Audio centre in Hz; omit to estimate")
    commands.add_parser("devices", help="List soundcard devices and their numeric IDs")
    listener = commands.add_parser("listen", help="Decode soundcard input to one JSON object per line")
    listener.add_argument("--device", type=device_argument, default=argparse.SUPPRESS,
                          help="Input device ID or unique name substring; default: system input")
    listener.add_argument("--sample-rate", type=int, default=argparse.SUPPRESS,
                          help="Soundcard sample rate (default: 48000)")
    listener.add_argument("--channel", type=int, default=argparse.SUPPRESS,
                          help="One-based input channel; 1=left/mono, 2=right (default: 1)")
    listener.add_argument("--centre", type=centre_argument, default=argparse.SUPPRESS,
                          help="Audio centre in Hz, or 'auto' to estimate (default: 1700)")
    listener.add_argument("--window", type=float, default=30.0,
                          help="Retained audio window in seconds (default: 30)")
    listener.add_argument("--interval", type=float, default=1.0,
                          help="Seconds between decode attempts (default: 1)")
    profile_arguments(listener)
    listener.add_argument("--frequency-khz", type=float, help="DSC RF centre in kHz; requires --profile")
    rigs = commands.add_parser("rigs", help="List models supported by the installed Hamlib")
    rigs.add_argument("--rigctld", default="rigctld", help="rigctld executable or full path")
    rig = commands.add_parser("rig", help="Read status or tune USB using a station profile; no audio")
    rig.add_argument("action", choices=("status", "tune"))
    profile_arguments(rig)
    rig.add_argument("--frequency-khz", type=float, help="DSC RF centre in kHz (default: profile)")
    profiles = commands.add_parser("profiles", help="List saved stations or create an example configuration")
    profiles.add_argument("action", nargs="?", choices=("list", "init"), default="list")
    profiles.add_argument("--config", type=Path)
    server = commands.add_parser("serve", help="Local browser receiver (optional web extra)")
    server.add_argument("--host", default="127.0.0.1",
                        help="Bind address; use 0.0.0.0 for LAN access with a password")
    server.add_argument("--port", type=int, default=8080)
    profile_arguments(server)
    position_arguments(server, suppress_defaults=True)
    args = parser.parse_args(argv)
    try:
        if args.command in ("transmit-test", "transmit-position"):
            return transmit_command(args)
        if args.command == "rigs":
            print(list_models(args.rigctld), end="")
            return 0
        if args.command == "rig":
            return rig_command(args)
        if args.command == "profiles":
            from .profiles import ProfileStore
            store = ProfileStore(args.config)
            if args.action == "init":
                store.initialize()
            profiles, default = store.read()
            print(json.dumps(dict(config=str(store.path), default_profile=default,
                                  profiles={name: p.radio for name, p in profiles.items()}), indent=2))
            return 0
        if args.command == "serve":
            from .web import serve
            return serve(args)
        if args.command == "ports":
            from .position import serial_ports
            print(json.dumps(serial_ports(), indent=2))
            return 0
        if args.command == "position":
            print(json.dumps(get_fix(args).as_dict(), indent=2, allow_nan=False))
            return 0
        if args.command == "devices":
            from .live import list_devices
            print(list_devices())
            return 0
        if args.command == "listen":
            return receive(args)
        if args.command in ("encode-test", "encode-position"):
            return encode(args)
        results = decode_wav(args.input, centre_hz=args.centre)
        print(json.dumps([result_dict(r) for r in results], indent=2))
        return 0 if results else 1
    except (ValueError, OSError, AudioInputError, RigError) as exc:
        parser.exit(2, f"dsc: {exc}\n")
    except KeyboardInterrupt:
        parser.exit(130, "dsc: stopped\n")


if __name__ == "__main__":
    raise SystemExit(main())

