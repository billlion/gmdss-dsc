"""Small offline WAV interface. SPDX-License-Identifier: GPL-3.0-or-later"""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
from .calls import TestCall
from .framing import encode_frame
from .modem import decode_wav, modulate, write_wav


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
    parser = argparse.ArgumentParser(description="Offline MF/HF DSC protocol experiments")
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
    args = parser.parse_args(argv)
    try:
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
    except (ValueError, OSError) as exc:
        parser.exit(2, f"dsc: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
