"""Shared JSON representation of received frames. SPDX-License-Identifier: GPL-3.0-or-later"""
from dataclasses import asdict
from .calls import TestCall, PositionReport


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
    try:
        record["position_report"] = asdict(PositionReport.from_frame(frame))
    except ValueError:
        record["position_report"] = None
    return record

