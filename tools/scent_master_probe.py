#!/usr/bin/env python3
"""Standalone bench test for Scent Master Pro diffusers.

Talks to the diffuser over plain BLE (bleak) so the protocol can be checked
against real hardware without touching Home Assistant. Only bleak is needed:

    python3 -m pip install bleak
    python3 tools/scent_master_probe.py                 # read-only
    python3 tools/scent_master_probe.py --write-test 2  # rewrite Setting 2 unchanged

Frames and decoding follow PROTOCOL.md; `--selftest` checks the encoder against
the captured frames without any radio.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import re
import sys

SERVICE_UUID = "0000ffe0-0000-1000-8000-00805f9b34fb"
NOTIFY_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"
WRITE_UUID = "0000ffe2-0000-1000-8000-00805f9b34fb"

HEADER = 0x55
DIR_WRITE = 0x10
DIR_NOTIFY = 0x11
FN_READ = 0x00
FN_DATA = 0x05

REG_CLOCK = 0x00
REG_SCHEDULE_1 = 0x01
REG_FAN = 0x06
REG_SESSION = 0x07
REG_ENABLE_1 = 0x11
REG_STATUS = 0xD1
REG_MODE = 0xD2
REG_NAME = 0xDA
REG_NOTE = 0xDB

SLOTS = 5
RECORD = 10
DAYS = (0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40)  # Mon..Sun
DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MAC_RE = re.compile(r"^(?:[0-9A-F]{2}[:-]){2,5}[0-9A-F]{2}$|^(?:[0-9A-F]{2}){3,6}$", re.I)


# -- framing -----------------------------------------------------------------

def frame(cmd: int, payload: bytes | None = None) -> bytes:
    """`55 10 <cmd> <fn> [<len> <payload>] <chk>`, chk = sum after the 0x55."""
    body = bytearray([DIR_WRITE, cmd & 0xFF])
    if payload is None:
        body.append(FN_READ)
    else:
        body.append(FN_DATA)
        body.append(len(payload) & 0xFF)
        body.extend(payload)
    body.append(sum(body) & 0xFF)
    return bytes([HEADER]) + bytes(body)


def read(cmd: int) -> bytes:
    return frame(cmd)


def write(cmd: int, payload: bytes) -> bytes:
    return frame(cmd, payload)


def clock_frame(now: datetime | None = None) -> bytes:
    now = now or datetime.now()
    return write(REG_CLOCK, bytes([DAYS[now.weekday()], now.hour, now.minute, now.second]))


def record_payload(weekdays: int, start: tuple[int, int], end: tuple[int, int],
                   work: int, pause: int) -> bytes:
    return bytes([
        weekdays & 0x7F,
        start[0] & 0xFF, start[1] & 0xFF, end[0] & 0xFF, end[1] & 0xFF,
        (work >> 8) & 0xFF, work & 0xFF,
        (pause >> 8) & 0xFF, pause & 0xFF,
    ])


def schedule_frame(slot: int, payload: bytes) -> bytes:
    return write(REG_SCHEDULE_1 + slot - 1, payload)


def enable_frame(slot: int, on: bool) -> bytes:
    return write(REG_ENABLE_1 + slot - 1, bytes([0x01 if on else 0x00]))


# -- decoding ----------------------------------------------------------------

def parse(argv: bytes) -> tuple[int, bytes] | None:
    if len(argv) < 5 or argv[0] != HEADER or argv[1] != DIR_NOTIFY:
        return None
    return argv[2], argv[5:5 + argv[4]]


def decode_schedules(payload: bytes) -> list[dict] | None:
    if len(payload) < SLOTS * RECORD:
        return None
    out = []
    for i in range(SLOTS):
        g = payload[i * RECORD:(i + 1) * RECORD]
        out.append({
            "slot": i + 1,
            "enabled": g[0] == 0x01,
            "weekdays": g[1] & 0x7F,
            "start": f"{g[2]:02d}:{g[3]:02d}",
            "end": f"{g[4]:02d}:{g[5]:02d}",
            "work": (g[6] << 8) | g[7],
            "pause": (g[8] << 8) | g[9],
        })
    return out


def describe(schedules: list[dict]) -> str:
    lines = []
    for s in schedules:
        days = [DAY_NAMES[i] for i in range(7) if s["weekdays"] & DAYS[i]]
        lines.append(
            f"  Setting {s['slot']}: {'ON ' if s['enabled'] else 'off'} "
            f"{s['start']}-{s['end']}  work {s['work']}s pause {s['pause']}s  "
            f"{','.join(days) or 'no days'}"
        )
    return "\n".join(lines)


# -- radio -------------------------------------------------------------------

class Link:
    def __init__(self, client) -> None:
        self.client = client
        self.frames: list[bytes] = []

    def _on_notify(self, _char, data: bytearray) -> None:
        self.frames.append(bytes(data))
        print(f"  <- {bytes(data).hex(' ').upper()}")

    async def start(self) -> None:
        await self.client.start_notify(NOTIFY_UUID, self._on_notify)

    async def send(self, data: bytes, settle: float = 0.35) -> None:
        print(f"  -> {data.hex(' ').upper()}")
        await self.client.write_gatt_char(WRITE_UUID, data, response=False)
        await asyncio.sleep(settle)

    async def wait_for(self, cmd: int, timeout: float = 3.0) -> bytes | None:
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            for f in list(self.frames):
                got = parse(f)
                if got and got[0] == cmd:
                    self.frames.remove(f)
                    return got[1]
            await asyncio.sleep(0.05)
        return None

    async def read_schedules(self, timeout: float = 4.0) -> list[dict] | None:
        """D1 is answered with D1 or F2 depending on firmware."""
        await self.send(read(REG_STATUS))
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            for f in list(self.frames):
                got = parse(f)
                if got and got[0] in (REG_STATUS, 0xF2):
                    self.frames.remove(f)
                    schedules = decode_schedules(got[1])
                    if schedules:
                        return schedules
            await asyncio.sleep(0.05)
        return None


def mfr_bytes(adv) -> bytes:
    data = getattr(adv, "manufacturer_data", None) or {}
    return b"".join(bytes(v) for v in data.values())


def mac_prefix(value: str) -> bytes:
    digits = re.sub(r"[^0-9A-Fa-f]", "", value)
    return bytes.fromhex(digits) if digits and len(digits) % 2 == 0 else b""


async def scan(timeout: float):
    from bleak import BleakScanner

    print(f"scanning {timeout:.0f}s…")
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    return list(found.values())


def report(found) -> None:
    print(f"{len(found)} device(s) advertising")
    for dev, adv in found:
        mfr = mfr_bytes(adv)
        services = [str(u)[4:8] for u in (getattr(adv, "service_uuids", None) or [])]
        name = dev.name or getattr(adv, "local_name", "") or "(no name)"
        print(
            f"  {name:34s} {dev.address}  rssi {getattr(adv, 'rssi', 0):4d}  "
            f"mfr {mfr[:10].hex(' ').upper() if mfr else '-'}  svc {services}"
        )


def pick(found, address: str | None, name_filter: str):
    """Name fragment, address/UUID fragment, or the MAC in the manufacturer data."""
    want = mac_prefix(address) if address and MAC_RE.match(address.strip()) else b""
    for dev, adv in found:
        label = f"{dev.name or ''} {getattr(adv, 'local_name', '') or ''}"
        if address and address.upper() in f"{label} {dev.address}".upper():
            return dev
        if want and mfr_bytes(adv).upper().startswith(want.upper()):
            return dev
        if not address and name_filter and name_filter.upper() in label.upper():
            return dev
    return None


async def run(args) -> int:
    from bleak import BleakClient

    found = await scan(args.timeout)
    report(found)
    if args.list:
        return 0
    target = pick(found, args.address, args.name)
    if target is None:
        print("no matching device. Close the phone app, keep the unit powered and in range,")
        print("and give Terminal Bluetooth permission (System Settings > Privacy & Security).")
        return 2
    print(f"connecting to {target.name or '(no name)'} [{target.address}]")
    async with BleakClient(target, timeout=30.0) as client:
        link = Link(client)
        await link.start()
        print("session start:")
        await link.send(write(REG_SESSION, bytes([0x01])))
        print("clock:")
        await link.send(clock_frame())
        print("read schedules:")
        schedules = await link.read_schedules()
        if not schedules:
            print("no schedule block — the unit did not answer 0xD1 (or answered a shape we don't know)")
            return 3
        print(describe(schedules))

        for reg, label in ((REG_FAN, "level/off"), (REG_MODE, "mode"),
                           (REG_NAME, "name"), (REG_NOTE, "note")):
            await link.send(read(reg))
            payload = await link.wait_for(reg, timeout=1.5)
            if payload is None:
                print(f"  {label}: no answer")
            elif reg in (REG_NAME, REG_NOTE):
                print(f"  {label}: {payload.strip(bytes(1)).decode('utf-8', 'replace')!r}")
            else:
                print(f"  {label}: {payload.hex(' ').upper()}")

        if args.write_test:
            slot = args.write_test
            current = schedules[slot - 1]
            sh, sm = (int(x) for x in current["start"].split(":"))
            eh, em = (int(x) for x in current["end"].split(":"))
            start, end = (sh, sm), (eh, em)
            payload = record_payload(
                current["weekdays"], start, end, current["work"], current["pause"]
            )
            print(f"writing Setting {slot} back unchanged ({payload.hex(' ').upper()}):")
            await link.send(schedule_frame(slot, payload))
            await link.send(enable_frame(slot, current["enabled"]))
            after = await link.read_schedules()
            if after is None:
                print("read-back failed")
                return 4
            print(describe(after))
            ok = after[slot - 1] == current
            print(f"write-back {'confirmed' if ok else 'DID NOT stick'}")
            if not ok:
                return 5
    return 0


def selftest() -> int:
    checks = {
        read(REG_STATUS): "55 10 D1 00 E1",
        read(REG_NOTE): "55 10 DB 00 EB",
        read(REG_MODE): "55 10 D2 00 E2",
        clock_frame(datetime(2026, 11, 22, 14, 31, 50)): "55 10 00 05 04 40 0E 1F 32 B8",
        schedule_frame(2, record_payload(0x52, (11, 0), (19, 0), 5, 900)):
            "55 10 02 05 09 52 0B 00 13 00 00 05 03 84 1C",
        schedule_frame(2, record_payload(0x52, (9, 0), (19, 0), 5, 900)):
            "55 10 02 05 09 52 09 00 13 00 00 05 03 84 1A",
        enable_frame(2, True): "55 10 12 05 01 01 29",
        write(REG_SESSION, b"\x01"): "55 10 07 05 01 01 1E",
    }
    bad = 0
    for got, expected in checks.items():
        want = bytes.fromhex(expected.replace(" ", ""))
        status = "ok" if got == want else f"MISMATCH want {expected}"
        bad += got != want
        print(f"{got.hex(' ').upper():60s} {status}")
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--address", help="BLE address or part of it (e.g. 9D:ED:F1)")
    ap.add_argument("--name", default="-TJ-", help="name fragment to match (default -TJ-)")
    ap.add_argument("--timeout", type=float, default=20.0, help="scan seconds (default 20)")
    ap.add_argument("--list", action="store_true", help="only list devices, do not connect")
    ap.add_argument("--write-test", type=int, metavar="SLOT",
                    help="rewrite that schedule unchanged and confirm the read-back")
    ap.add_argument("--selftest", action="store_true", help="check frames offline")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
