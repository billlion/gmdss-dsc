"""WAV and live soundcard interfaces. SPDX-License-Identifier: GPL-3.0-or-later"""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sys
from .calls import TestCall
from .framing import encode_frame
from .modem import decode_wav, modulate, write_wav
from .live import AudioInputError


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


def result_dict(result):
    frame = result.decoded.frame
    record = {
        "information_symbols": list(frame.information),
        "eos": frame.eos,
        "ecc": frame.ecc,
        "ecc_passed": True,
        "data_start_s": result.data_start_s,
        "end_s": result.end_s,
        "centre_hz": result.centre_hz,
        "inverted": result.inverted,
        "recovered_characters": result.decoded.recovered_characters,
        "tone_margin": result.tone_margin,
    }
    try:
        record["test_call"] = asdict(TestCall.from_frame(frame))
    except ValueError:
        record["test_call"] = None
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description="MF/HF DSC audio experiments")
    commands = parser.add_subparsers(dest="command", required=True)
    encoder = commands.add_parser("encode-test", help="Write an individual safety test call WAV")
    encoder.add_argument("output", type=Path)
    encoder.add_argument("--source", required=True, help="Nine-digit MMSI")
    encoder.add_argument("--destination", required=True, help="Nine-digit MMSI")
    encoder.add_argument("--ack", action="store_true", help="Encode an acknowledgement (EOS 122)")
    encoder.add_argument("--centre", type=float, default=1700.0, help="Audio centre frequency in Hz")
    encoder.add_argument("--sample-rate", type=int, default=8000)
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
    args = parser.parse_args(argv)
    try:
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
        if args.command == "encode-test":
            call = TestCall(args.source, args.destination, args.ack)
            frame = call.to_frame()
            bits = encode_frame(frame, dot_bits=call.dot_bits)
            audio = modulate(bits, sample_rate=args.sample_rate, centre_hz=args.centre)
            write_wav(args.output, audio, args.sample_rate)
            print(json.dumps({"wav": str(args.output), "test_call": asdict(call),
                              "ecc": frame.ecc, "dot_bits": call.dot_bits,
                              "duration_s": len(audio)/args.sample_rate}, indent=2))
            return 0
        results = decode_wav(args.input, centre_hz=args.centre)
        print(json.dumps([result_dict(r) for r in results], indent=2))
        return 0 if results else 1
    except (ValueError, OSError, AudioInputError) as exc:
        parser.exit(2, f"dsc: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
