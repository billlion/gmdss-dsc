"""WAV and live soundcard interfaces. SPDX-License-Identifier: GPL-3.0-or-later"""
import argparse
from dataclasses import asdict
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


def position_arguments(parser):
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


def main(argv=None):
    parser = argparse.ArgumentParser(description="MF/HF DSC audio experiments")
    commands = parser.add_subparsers(dest="command", required=True)
    encoder = commands.add_parser("encode-test", help="Write an individual safety test call WAV")
    encoder_arguments(encoder)
    encoder.add_argument("--ack", action="store_true", help="Encode an acknowledgement (EOS 122)")
    report = commands.add_parser("encode-position", help="Encode a safety position acknowledgement (EOS 122)")
    encoder_arguments(report)
    position_arguments(report)
    position = commands.add_parser("position", help="Read one fresh position and print JSON; no audio")
    position_arguments(position)
    commands.add_parser("ports", help="List serial ports for NMEA GPS input")
    decoder = commands.add_parser("decode", help="Find complete ECC-passing calls in a mono WAV")
    decoder.add_argument("input", type=Path)
    decoder.add_argument("--centre", type=float, default=None, help="Audio centre in Hz; omit to estimate")
    commands.add_parser("devices", help="List soundcard devices and their numeric IDs")
    listener = commands.add_parser("listen", help="Decode soundcard input to one JSON object per line")
    listener.add_argument("--device", type=device_argument, default=None,
                          help="Input device ID or unique name substring; default: system input")
    listener.add_argument("--sample-rate", type=int, default=48000,
                          help="Soundcard sample rate (default: 48000)")
    listener.add_argument("--channel", type=int, default=1,
                          help="One-based input channel; 1=left/mono, 2=right (default: 1)")
    listener.add_argument("--centre", type=centre_argument, default=1700.0,
                          help="Audio centre in Hz, or 'auto' to estimate (default: 1700)")
    listener.add_argument("--window", type=float, default=30.0,
                          help="Retained audio window in seconds (default: 30)")
    listener.add_argument("--interval", type=float, default=1.0,
                          help="Seconds between decode attempts (default: 1)")
    server = commands.add_parser("serve", help="Local browser receiver (optional web extra)")
    server.add_argument("--host", default="127.0.0.1",
                        help="Bind address; use 0.0.0.0 for LAN access with a password")
    server.add_argument("--port", type=int, default=8080)
    position_arguments(server)
    args = parser.parse_args(argv)
    try:
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
            from .live import listen

            def emit(result, segment, decoded_at):
                record = result_dict(result)
                record.update(capture_segment=segment, decoded_at_utc=decoded_at)
                print(json.dumps(record, allow_nan=False), flush=True)

            listen(emit=emit, log=lambda message: print(message, file=sys.stderr, flush=True),
                   device=args.device, sample_rate=args.sample_rate, channel=args.channel,
                   centre_hz=args.centre, window_s=args.window, interval_s=args.interval)
            return 0
        if args.command in ("encode-test", "encode-position"):
            return encode(args)
        results = decode_wav(args.input, centre_hz=args.centre)
        print(json.dumps([result_dict(r) for r in results], indent=2))
        return 0 if results else 1
    except (ValueError, OSError, AudioInputError) as exc:
        parser.exit(2, f"dsc: {exc}\n")
    except KeyboardInterrupt:
        parser.exit(130, "dsc: stopped\n")


if __name__ == "__main__":
    raise SystemExit(main())

