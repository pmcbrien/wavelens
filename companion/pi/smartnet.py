#!/usr/bin/env python3
"""
Motorola Type II (SmartNet / SmartZone) control-channel decoder for the Wavelens bridge.

A Type II system carries analog FM voice; only its control channel is digital: 3600 baud FSK, 84-bit
frames, each one an "outbound signalling word" (OSW) of a 16-bit address, a group flag and a 10-bit
command, protected by a parity code and a 10-bit CRC. dsd-fme has no decoder for it, so this one reads
rtl_fm's discriminator audio, recovers the bits, and turns the OSWs into voice grants:
(talkgroup, voice frequency). The bridge then retunes rtl_fm to the voice channel for the call.

What it follows: group voice grants and updates (clear and encrypted-flagged). What it reports but does
not tune: private and interconnect calls (phone patches — not for a scanner to play). Nothing here
transmits.

Bit-level design from the published protocol description and the GPL reference decoders gr-smartnet
(Nick Foster) and trunk-recorder's smartnet_parser; re-implemented in Python for a Pi with no GNU Radio.

Standalone test:  rtl_fm -f 866.1375M -M fm -s 24k -g 40 - | python3 smartnet.py --rate 24000
"""
import argparse
import math
import struct
import sys
import time

BAUD = 3600.0
SYNC = [1, 0, 1, 0, 1, 1, 0, 0]          # the 8-bit frame preamble
FRAME_BITS = 84                           # preamble is followed by 76 interleaved bits
# 800 MHz bandplan (standard / rebanded / splinter), channel number → MHz (receive side)
_BAND = {"800", "800r", "800s", "900", "obt"}


def chan_to_mhz(chan: int, band: str = "800") -> float:
    """Channel number from an OSW → voice frequency in MHz. band: 800 (standard), 800r (rebanded),
    800s (splinter), 900, or obt:<base>:<spacing>:<offset> for 400/500 MHz systems."""
    if band.startswith("obt"):
        try:
            _, base, spacing, offset = band.split(":")
            base, spacing, offset = float(base), float(spacing), int(offset)
        except ValueError:
            return 0.0
        return round(base + spacing * (chan - offset), 5) if chan >= offset else 0.0
    if band == "900":
        return round(935.0125 + 0.0125 * chan, 5) if chan <= 0x1DE else 0.0
    f = 0.0
    if band == "800r":
        if chan <= 0x1B7:
            f = 851.0125 + 0.025 * chan
        elif 0x1B8 <= chan <= 0x22F:
            f = 851.0250 + 0.025 * (chan - 0x1B8)
    elif band == "800s":
        if chan <= 0x257:
            f = 851.0000 + 0.025 * chan
        elif 0x258 <= chan <= 0x2CF:
            f = 866.0125 + 0.025 * (chan - 0x258)
    else:
        if chan <= 0x2CF:
            f = 851.0125 + 0.025 * chan
    if 0x2D0 <= chan <= 0x2F7:
        f = 866.0000 + 0.025 * (chan - 0x2D0)
    elif 0x32F <= chan <= 0x33F:
        f = 867.0000 + 0.025 * (chan - 0x32F)
    elif 0x3C1 <= chan <= 0x3FE:
        f = 867.4250 + 0.025 * (chan - 0x3C1)
    elif chan == 0x3BE:
        f = 868.9750
    return round(f, 5)


def is_chan(cmd: int, band: str = "800") -> bool:
    """Is this command field a channel number (a grant / update) rather than an opcode?"""
    if band.startswith("obt"):
        try:
            _, base, spacing, offset = band.split(":")
            return int(offset) <= cmd < int(offset) + 380
        except ValueError:
            return False
    if band == "900":
        return cmd <= 0x1DE
    if (0x2D0 <= cmd <= 0x2F7) or (0x32F <= cmd <= 0x33F) or (0x3C1 <= cmd <= 0x3FE) or cmd == 0x3BE:
        return True
    return cmd <= (0x22F if band == "800r" else 0x2CF)


class Demod:
    """3600-baud FSK bit recovery from rtl_fm's 16-bit discriminator audio. The discriminator output is the
    instantaneous frequency: positive for one tone, negative for the other. A DC-blocked slicer and a
    Gardner-style clock on the zero crossings give the bit stream."""

    def __init__(self, rate: int):
        self.rate = rate
        self.spb = rate / BAUD               # samples per bit (6.67 at 24 kHz)
        self.dc = 0.0
        self.phase = 0.0
        self.last = 0
        self.bits = []
        self.lp = 0.0

    def feed(self, pcm: bytes) -> list:
        n = len(pcm) // 2
        out = []
        samples = struct.unpack(f"<{n}h", pcm[:2 * n])
        for s in samples:
            # slow DC tracker (carrier offset), fast low-pass (noise)
            self.dc += (s - self.dc) * 0.0005
            self.lp += ((s - self.dc) - self.lp) * 0.6
            bit = 1 if self.lp > 0 else 0
            # clock: a transition pulls the sampling phase toward mid-bit
            if bit != self.last:
                err = self.phase - 0.5
                self.phase -= err * 0.3
                self.last = bit
            self.phase += 1.0 / self.spb
            if self.phase >= 1.0:
                self.phase -= 1.0
                out.append(bit)
        return out


class Framer:
    """Finds the 8-bit preamble in the bit stream, takes the 76 bits after it, deinterleaves (4 × 19),
    corrects single errors with the parity bits, checks the CRC, and yields (address, group, command)."""

    def __init__(self):
        self.buf = []

    def feed(self, bits: list):
        self.buf.extend(bits)
        out = []
        i = 0
        while i + FRAME_BITS <= len(self.buf):
            if self.buf[i:i + 8] == SYNC:
                frame = self.buf[i + 8:i + 8 + 76]
                osw = decode_frame(frame)
                if osw is not None:
                    out.append(osw)
                    i += FRAME_BITS
                    continue
            i += 1
        del self.buf[:max(0, i - 8)]
        return out


def decode_frame(inter: list):
    # deinterleave: out[k*4 + l] = in[k + l*19]
    de = [0] * 76
    for k in range(19):
        for l in range(4):
            de[k * 4 + l] = inter[k + l * 19]
    # parity: pairs (info, parity) with parity = info ^ previous info; two bad parities in a row flip the bit
    expected = [0] * 76
    expected[0] = de[0]
    expected[1] = de[0]
    for k in range(2, 76, 2):
        expected[k] = de[k]
        expected[k + 1] = de[k] ^ de[k - 2]
    syn = [expected[k] ^ de[k] for k in range(76)]
    data = [0] * 38
    for k in range(37):
        if syn[2 * k + 1] and syn[2 * k + 3]:
            data[k] = 0 if de[2 * k] else 1
        else:
            data[k] = de[2 * k]
    data[37] = de[74]
    # CRC over the first 27 bits, compared with the next 10 (bits are inverted on the air)
    acc, op = 0x0393, 0x036E
    for j in range(27):
        op = (op >> 1) ^ 0x0225 if op & 1 else op >> 1
        if data[j]:
            acc ^= op
    given = 0
    for j in range(10):
        given = (given << 1) | (0 if data[j + 27] else 1)
    if given != acc:
        return None
    addr = 0
    for k in range(16):
        addr = (addr << 1) | (0 if data[k] else 1)
    group = not data[16]
    cmd = 0
    for k in range(10):
        cmd = (cmd << 1) | (0 if data[17 + k] else 1)
    return (addr ^ 0x33C7, group, cmd ^ 0x032A)


class Parser:
    """OSW sequences → events. Each event is a dict: {"ev": "grant"|"update"|"idle"|"cc"|"sysid"|"private",
    "tg": talkgroup, "src": radio id (grants only; the bridge drops it before anything is shown),
    "mhz": voice frequency, "enc": encrypted flag, "emerg": emergency}."""

    def __init__(self, band: str = "800"):
        self.band = band
        self.q = []
        self.sysid = None
        self.cc_mhz = 0.0

    def feed(self, osws: list) -> list:
        self.q.extend(osws)
        events = []
        while len(self.q) >= 2:
            a, g, c = self.q[0]
            ch = is_chan(c, self.band)
            if ch and g:
                # one-OSW group update: the call on this channel continues
                events.append({"ev": "update", "tg": a & 0xFFF0, "mhz": chan_to_mhz(c, self.band),
                               "enc": bool(a & 0x8), "emerg": (a & 7) in (2, 4, 5)})
                self.q.pop(0)
            elif ch and not g and (a & 0xFF00) == 0x1F00:
                self.cc_mhz = chan_to_mhz(c, self.band)
                events.append({"ev": "cc", "mhz": self.cc_mhz})
                self.q.pop(0)
            elif c == 0x2F8 and not g:
                events.append({"ev": "idle"})
                self.q.pop(0)
            elif c == 0x308:
                a1, g1, c1 = self.q[1]
                if is_chan(c1, self.band) and not g1 and (a1 & 0xFF00) == 0x1F00:
                    self.sysid = a
                    self.cc_mhz = chan_to_mhz(c1, self.band)
                    events.append({"ev": "sysid", "sysid": a, "mhz": self.cc_mhz})
                elif is_chan(c1, self.band) and g1 and a1 and a:
                    events.append({"ev": "grant", "tg": a1 & 0xFFF0, "src": a, "mhz": chan_to_mhz(c1, self.band),
                                   "enc": bool(a1 & 0x8), "emerg": (a1 & 7) in (2, 4, 5)})
                elif is_chan(c1, self.band) and not g1 and a1 and a:
                    events.append({"ev": "private", "mhz": chan_to_mhz(c1, self.band)})
                del self.q[:2]
            else:
                self.q.pop(0)                # an opcode this decoder doesn't need (affiliations, status, …)
        return events


def main() -> None:
    ap = argparse.ArgumentParser(description="SmartNet control-channel decoder: rtl_fm s16 audio on stdin → OSW events on stdout")
    ap.add_argument("--rate", type=int, default=24000, help="sample rate of the audio on stdin (rtl_fm -s)")
    ap.add_argument("--band", default="800", help="800 | 800r (rebanded) | 800s (splinter) | 900 | obt:base:spacing:offset")
    a = ap.parse_args()
    demod, framer, parser = Demod(a.rate), Framer(), Parser(a.band)
    n_osw = 0
    t0 = time.time()
    while True:
        pcm = sys.stdin.buffer.read(4096)
        if not pcm:
            break
        osws = framer.feed(demod.feed(pcm))
        n_osw += len(osws)
        for ev in parser.feed(osws):
            print(json_line(ev), flush=True)
        if n_osw and time.time() - t0 > 5.0:
            print(json_line({"ev": "stats", "osw": n_osw}), flush=True)
            t0 = time.time()


def json_line(ev: dict) -> str:
    import json
    return json.dumps(ev, separators=(",", ":"))


if __name__ == "__main__":
    main()
