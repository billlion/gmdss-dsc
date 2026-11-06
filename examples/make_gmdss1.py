"""Regenerate the clean audio and an inspectable wire trace after pip install -e ."""
import json
from pathlib import Path
from gmdss_dsc import GMDSS_1, encode_frame, frame_symbols
from gmdss_dsc.modem import modulate, write_wav

folder = Path(__file__).resolve().parent
frame = GMDSS_1.to_frame()
bits = encode_frame(frame,dot_bits=GMDSS_1.dot_bits)
audio = modulate(bits,centre_hz=975,sample_rate=8000)
write_wav(folder/"gmdss1_clean.wav",audio,8000)
trace = {
    "description": "New clean synthesis of the GMDSS_1 reference message",
    "source_mmsi": GMDSS_1.source,
    "destination_mmsi": GMDSS_1.destination,
    "information_symbols_format_once": list(frame.information),
    "eos": frame.eos,
    "ecc": frame.ecc,
    "dot_bits": GMDSS_1.dot_bits,
    "physical_symbols_including_phasing": list(frame_symbols(frame)),
    "bits_in_transmission_order": "".join(map(str,bits)),
    "bit_rate": 100,
    "sample_rate": 8000,
    "centre_hz": 975,
    "shift_hz": 170,
    "leading_silence_s": 0.25,
    "trailing_silence_s": 0.25,
    "duration_s": len(audio)/8000,
}
(folder/"gmdss1_trace.json").write_text(json.dumps(trace,indent=2)+"\n")
print(folder/"gmdss1_clean.wav")
