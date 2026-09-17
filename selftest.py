#!/usr/bin/env python3
"""Check that everything this server needs is actually working.

    .venv/bin/python selftest.py
    .venv/bin/python selftest.py --package com.example.app --project ~/StudioProjects/app

Without --package it checks the Android side only. With one, it also checks the
Flutter bridge; add --project to check hot reload too.
"""
from __future__ import annotations

import argparse
import sys
import time

from flutter_bridge_mcp import server as s

PASS, FAIL, SKIP = "  PASS", "  FAIL", "  SKIP"
results: list[tuple[str, str, str]] = []


def check(name: str, fn, skip_if: str = "") -> bool:
    if skip_if:
        results.append((SKIP, name, skip_if))
        print(f"{SKIP}  {name} — {skip_if}")
        return False
    try:
        detail = fn() or ""
        results.append((PASS, name, str(detail)))
        print(f"{PASS}  {name}" + (f" — {detail}" if detail else ""))
        return True
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        results.append((FAIL, name, msg))
        print(f"{FAIL}  {name} — {msg[:200]}")
        return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--package", help="app id, e.g. com.example.app")
    ap.add_argument("--project", help="Flutter project root, for the hot reload check")
    ap.add_argument("--serial", help="device serial when several are attached")
    args = ap.parse_args()
    ser = args.serial

    print(f"\nadb: {s.ADB}\n")

    print("Android layer")
    ok_device = check("device connected", lambda: s.list_devices().splitlines()[0][:60])
    if not ok_device:
        print("\nNo device — start an emulator and run again.")
        return 1
    check("device_info", lambda: s.device_info(ser).splitlines()[0])
    check("screenshot", lambda: f"{len(s.screenshot(serial=ser).data) // 1024} KB png")
    check("ui_dump", lambda: s.ui_dump(limit=1, serial=ser).splitlines()[0])
    check("read_logs", lambda: s.read_logs(limit=1, serial=ser).splitlines()[0])

    print("\nVisual verification")
    check("ui_checkpoint", lambda: s.ui_checkpoint("selftest", ser))
    check("ui_diff (expects no change)", lambda: s.ui_diff("selftest", serial=ser)[0].splitlines()[0])

    print("\nFlutter bridge")
    reason = "" if args.package else "no --package given"
    connected = check("flutter_connect",
                      lambda: s.flutter_connect(args.package, relaunch=True, serial=ser).splitlines()[-1],
                      skip_if=reason)
    check("flutter_widget_tree",
          lambda: s.flutter_widget_tree(limit=1, package=args.package, serial=ser).splitlines()[0],
          skip_if="" if connected else "not connected")
    check("flutter_locate",
          lambda: s.flutter_locate("a", package=args.package, serial=ser).splitlines()[0],
          skip_if="" if connected else "not connected")
    check("flutter_diagnose",
          lambda: s.flutter_diagnose(args.package, serial=ser).splitlines()[0],
          skip_if="" if connected else "not connected")

    print("\nHot reload")
    why = "" if (connected and args.project) else (
        "no --project given" if connected else "not connected")
    attached = check("flutter_attach",
                     lambda: s.flutter_attach(args.project, ser).splitlines()[0],
                     skip_if=why)
    if attached:
        check("flutter_hot_reload", lambda: s.flutter_hot_reload().splitlines()[0])
        check("flutter_detach", lambda: s.flutter_detach())

    n_pass = sum(1 for r in results if r[0] == PASS)
    n_fail = sum(1 for r in results if r[0] == FAIL)
    n_skip = sum(1 for r in results if r[0] == SKIP)
    print(f"\n{n_pass} passed, {n_fail} failed, {n_skip} skipped")
    if n_fail:
        print("\nFailures:")
        for status, name, detail in results:
            if status == FAIL:
                print(f"  {name}: {detail[:300]}")
    return 1 if n_fail else 0


if __name__ == "__main__":
    sys.exit(main())
