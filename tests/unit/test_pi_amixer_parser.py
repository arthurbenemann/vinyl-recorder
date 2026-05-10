"""Unit test for the `amixer contents` parser used by `pi/server.py`.

Pi-side code doesn't normally get unit-tested (no pip, stdlib only, runs
on real hardware). This file pulls in just the pure-function parser so
we can pin the parsing rules against a captured fixture — adding a
control or changing the format would otherwise only get caught by a
human eyeballing /info.
"""
import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def pi_mod():
    """Import `pi/server.py` as a module under a synthetic name so it
    doesn't shadow the `app/` import path. Skips if the file moved."""
    here = Path(__file__).resolve().parent.parent.parent
    src = here / "pi" / "server.py"
    if not src.exists():
        pytest.skip(f"pi/server.py not found at {src}")
    spec = importlib.util.spec_from_file_location("pi_server_under_test", src)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["pi_server_under_test"] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


# Captured from a real `amixer -c 0 contents` on a HiFiBerry DAC-ADC Pro.
# Trimmed to the five controls /info reports on, with realistic Item
# enumerations including the stereo `values=24,24` variant.
SAMPLE_CONTENTS = """\
numid=1,iface=MIXER,name='PGA Gain Left'
  ; type=ENUMERATED,access=rw,values=2,items=105
  ; Item #0 '-12.0dB'
  ; Item #1 '-11.5dB'
  ; Item #24 '0.0dB'
  ; Item #80 '28.0dB'
  : values=24,24
numid=2,iface=MIXER,name='PGA Gain Right'
  ; type=ENUMERATED,access=rw,values=2,items=105
  ; Item #0 '-12.0dB'
  ; Item #24 '0.0dB'
  ; Item #80 '28.0dB'
  : values=80,80
numid=3,iface=MIXER,name='ADC Mic Bias'
  ; type=ENUMERATED,access=rw,values=1,items=4
  ; Item #0 'Mic Bias off'
  ; Item #1 'Mic Bias on, pin pulled to AVDD'
  : values=0
numid=4,iface=MIXER,name='ADC Left Input'
  ; type=ENUMERATED,access=rw,values=1,items=8
  ; Item #0 'Off'
  ; Item #1 'VINL1[SE]'
  ; Item #2 'VINL2[SE]'
  : values=1
numid=5,iface=MIXER,name='ADC Right Input'
  ; type=ENUMERATED,access=rw,values=1,items=8
  ; Item #0 'Off'
  ; Item #1 'VINR1[SE]'
  ; Item #2 'VINR2[SE]'
  : values=2
numid=6,iface=MIXER,name='ADC Capture Volume'
  ; type=INTEGER,access=rw,values=2,min=0,max=40,step=0
  : values=24,24
"""


def test_parses_all_five_controls(pi_mod):
    out = pi_mod.parse_amixer_contents(SAMPLE_CONTENTS)
    for name in ("PGA Gain Left", "PGA Gain Right", "ADC Mic Bias",
                 "ADC Left Input", "ADC Right Input"):
        assert name in out, f"missing control: {name}"


def test_stereo_value_takes_first_int(pi_mod):
    """Stereo controls report `values=N,N`; we take the first int. Both
    PGA Gain L/R are stereo — pin that they extract their respective
    24 / 80 indices, not the literal trailing comma-pair."""
    out = pi_mod.parse_amixer_contents(SAMPLE_CONTENTS)
    assert out["PGA Gain Left"]["value"]  == 24
    assert out["PGA Gain Right"]["value"] == 80


def test_enum_label_resolved_from_items(pi_mod):
    """The `; Item #N '...'` line for the chosen value index is the
    human-readable label. Mic bias is at value 0 → 'Mic Bias off'."""
    out = pi_mod.parse_amixer_contents(SAMPLE_CONTENTS)
    assert out["ADC Mic Bias"]["label"]    == "Mic Bias off"
    assert out["ADC Left Input"]["label"]  == "VINL1[SE]"
    assert out["ADC Right Input"]["label"] == "VINR2[SE]"


def test_pga_gain_label_matches_items(pi_mod):
    """PGA Gain controls are also enumerated; the label is the dB string
    at the chosen index (24 → '0.0dB' for left, 80 → '28.0dB' for right)."""
    out = pi_mod.parse_amixer_contents(SAMPLE_CONTENTS)
    assert out["PGA Gain Left"]["label"]  == "0.0dB"
    assert out["PGA Gain Right"]["label"] == "28.0dB"


def test_integer_control_parsed_without_label(pi_mod):
    """`ADC Capture Volume` is INTEGER (no Item lines); the parser still
    extracts a value but `label` is None to distinguish it from enums."""
    out = pi_mod.parse_amixer_contents(SAMPLE_CONTENTS)
    cv = out["ADC Capture Volume"]
    assert cv["value"] == 24
    assert cv["label"] is None


def test_unknown_control_absent_from_map(pi_mod):
    """Sanity: only what the dump contains comes out — no defaults."""
    out = pi_mod.parse_amixer_contents(SAMPLE_CONTENTS)
    assert "Nonexistent Control" not in out


def test_empty_input_yields_empty_dict(pi_mod):
    assert pi_mod.parse_amixer_contents("") == {}


def test_label_falls_back_to_value_string_when_item_missing(pi_mod):
    """A control whose value index has no matching `; Item #N '...'`
    falls back to the stringified value rather than raising. Models
    a partially-truncated dump or an ALSA quirk."""
    text = (
        "numid=99,iface=MIXER,name='Weird Control'\n"
        "  ; type=ENUMERATED,access=rw,values=1,items=2\n"
        "  ; Item #0 'A'\n"
        "  ; Item #1 'B'\n"
        "  : values=7\n"
    )
    out = pi_mod.parse_amixer_contents(text)
    assert out["Weird Control"]["value"] == 7
    assert out["Weird Control"]["label"] == "7"
