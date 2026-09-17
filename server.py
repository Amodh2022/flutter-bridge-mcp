#!/usr/bin/env python3
"""
flutter-bridge-mcp — joins Android's view of a running Flutter app to Dart's.

Android tooling sees a Flutter app as one opaque SurfaceView: it has logcat, tap
coordinates and a semantics tree, but no idea what a widget is. Dart tooling sees the
widget tree and source locations, but nothing of logcat, ANRs or native crashes. This
server holds both at once, so "this looks wrong on screen" can be answered with the
file and line that built it — and a fix verified by hot reloading and diffing pixels.

Second design goal: never hand the model raw logcat. Every tool filters, tails,
collapses duplicates and truncates server-side so responses stay small.

Requires: pip install .   plus adb on PATH (or ADB_PATH), and for the bridge,
          a debug/profile build and the flutter CLI (or FLUTTER_PATH).
Run:      python server.py
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from collections import deque
from typing import Optional
from xml.etree import ElementTree as ET

from mcp.server.mcpserver import Image, MCPServer
from PIL import Image as PILImage, ImageChops, ImageDraw

# ---------------------------------------------------------------- config

ADB = os.environ.get("ADB_PATH") or shutil.which("adb") or "adb"
DEFAULT_SERIAL = os.environ.get("ANDROID_SERIAL")
MAX_LINES = int(os.environ.get("LOGCAT_MCP_MAX_LINES", "200"))
MAX_MSG = int(os.environ.get("LOGCAT_MCP_MAX_MSG", "400"))
SCAN_LINES = int(os.environ.get("LOGCAT_MCP_SCAN_LINES", "8000"))
BUFFER_SIZE = int(os.environ.get("LOGCAT_MCP_BUFFER", "40000"))
R8_JAR = os.environ.get("R8_JAR")

mcp = MCPServer(
    "flutter-bridge",
    instructions=(
        "Drive an Android device and read its logs, and for Flutter debug builds "
        "map what is on screen to the Dart source that built it. Prefer ui_dump and "
        "tap(text=...) over raw coordinates; prefer flutter_locate when the question "
        "is which code is responsible for something visible."
    ),
)

LINE_RE = re.compile(
    r"^(?P<ts>\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\s+"
    r"(?P<pid>\d+)\s+(?P<tid>\d+)\s+"
    r"(?P<level>[VDIWEFS])\s+"
    r"(?P<tag>.*?)\s*:\s?(?P<msg>.*)$"
)
LEVELS = "VDIWEF"


# ---------------------------------------------------------------- adb glue


def _adb(args: list[str], serial: Optional[str] = None, timeout: int = 60) -> str:
    cmd = [ADB]
    s = serial or DEFAULT_SERIAL
    if s:
        cmd += ["-s", s]
    cmd += args
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except FileNotFoundError:
        raise RuntimeError(f"adb not found at {ADB!r}. Set ADB_PATH.")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"adb timed out after {timeout}s: {' '.join(args)}")
    if p.returncode != 0:
        err = p.stderr.decode(errors="replace").strip()
        raise RuntimeError(err or f"adb exited {p.returncode}")
    return p.stdout.decode("utf-8", errors="replace")


def _pids_for(package: str, serial: Optional[str]) -> list[str]:
    out = _adb(["shell", "pidof", package], serial=serial, timeout=15).strip()
    if out:
        return out.split()
    # Older devices lack pidof; fall back to ps.
    ps = _adb(["shell", "ps", "-A"], serial=serial, timeout=20)
    pids = []
    for line in ps.splitlines():
        parts = line.split()
        if parts and parts[-1] == package and len(parts) > 1 and parts[1].isdigit():
            pids.append(parts[1])
    return pids


# ---------------------------------------------------------------- filtering


def _parse(lines: list[str]) -> list[dict]:
    out, pending = [], None
    for raw in lines:
        m = LINE_RE.match(raw)
        if m:
            pending = m.groupdict()
            out.append(pending)
        elif pending is not None and raw.strip():
            # Continuation line (stack frames etc.) — attach to previous entry.
            pending["msg"] += "\n" + raw.rstrip()
    return out


def _filter(
    entries: list[dict],
    pids: Optional[set[str]],
    tag: Optional[str],
    min_level: str,
    contains: Optional[str],
) -> list[dict]:
    floor = LEVELS.index(min_level.upper()) if min_level.upper() in LEVELS else 0
    tag_re = re.compile(tag, re.I) if tag else None
    needle = contains.lower() if contains else None
    kept = []
    for e in entries:
        if e["level"] not in LEVELS or LEVELS.index(e["level"]) < floor:
            continue
        if pids and e["pid"] not in pids:
            continue
        if tag_re and not tag_re.search(e["tag"]):
            continue
        if needle and needle not in e["msg"].lower() and needle not in e["tag"].lower():
            continue
        kept.append(e)
    return kept


def _render(entries: list[dict], limit: int) -> str:
    """Tail to `limit`, collapse consecutive duplicates, truncate long messages."""
    entries = entries[-limit:]
    if not entries:
        return "(no matching log lines)"
    rows, last_key, count = [], None, 0

    def flush():
        if count > 1:
            rows[-1] += f"   [x{count} repeated]"

    for e in entries:
        key = (e["level"], e["tag"], e["msg"])
        if key == last_key:
            count += 1
            continue
        flush()
        last_key, count = key, 1
        msg = e["msg"]
        if len(msg) > MAX_MSG:
            msg = msg[:MAX_MSG] + f"… (+{len(msg) - MAX_MSG} chars)"
        rows.append(f"{e['ts']} {e['level']}/{e['tag']} ({e['pid']}): {msg}")
    flush()
    return "\n".join(rows)


# ---------------------------------------------------------------- tools


@mcp.tool()
def list_devices() -> str:
    """List connected Android devices and emulators with their state and model."""
    out = _adb(["devices", "-l"], timeout=15)
    lines = [l for l in out.splitlines()[1:] if l.strip()]
    return "\n".join(lines) or "No devices connected. Check USB debugging is enabled."


@mcp.tool()
def read_logs(
    package: Optional[str] = None,
    tag: Optional[str] = None,
    min_level: str = "I",
    contains: Optional[str] = None,
    limit: int = 100,
    buffer: str = "main",
    serial: Optional[str] = None,
) -> str:
    """Read recent logcat output, filtered.

    Args:
        package: only show logs from this app's process (e.g. com.example.app).
        tag: regex matched against the log tag.
        min_level: one of V, D, I, W, E, F. Defaults to I.
        contains: case-insensitive substring that must appear in tag or message.
        limit: max lines returned (hard-capped by LOGCAT_MCP_MAX_LINES).
        buffer: main, system, crash, events, radio, or all.
        serial: device serial when several are attached.
    """
    limit = max(1, min(limit, MAX_LINES))
    pids = set(_pids_for(package, serial)) if package else None
    if package and not pids:
        return f"{package} is not running. Launch the app, then read logs again."

    raw = _adb(
        ["logcat", "-d", "-v", "threadtime", "-b", buffer, "-t", str(SCAN_LINES)],
        serial=serial,
    )
    entries = _filter(_parse(raw.splitlines()), pids, tag, min_level, contains)
    header = f"[{len(entries)} matching lines, showing last {min(limit, len(entries))}]"
    return header + "\n" + _render(entries, limit)


@mcp.tool()
def find_crashes(
    package: Optional[str] = None,
    limit: int = 3,
    serial: Optional[str] = None,
) -> str:
    """Extract recent crashes and ANRs as whole stack traces from the crash buffer.

    Args:
        package: restrict to traces mentioning this package name.
        limit: how many of the most recent traces to return.
        serial: device serial when several are attached.
    """
    raw = _adb(
        ["logcat", "-d", "-v", "threadtime", "-b", "crash", "-t", str(SCAN_LINES)],
        serial=serial,
    )
    blocks, current = [], None
    for e in _parse(raw.splitlines()):
        text = f"{e['ts']} {e['level']}/{e['tag']}: {e['msg']}"
        starter = "FATAL EXCEPTION" in e["msg"] or e["tag"] in ("AndroidRuntime", "ANRManager")
        if starter and (current is None or "FATAL EXCEPTION" in e["msg"]):
            if current:
                blocks.append(current)
            current = text
        elif current is not None:
            current += "\n" + text
    if current:
        blocks.append(current)

    if package:
        blocks = [b for b in blocks if package in b]
    if not blocks:
        return "No crashes found in the crash buffer."

    picked = blocks[-limit:]
    out = [f"[{len(blocks)} crash blocks found, showing last {len(picked)}]"]
    for b in picked:
        out.append(b[:4000] + ("\n…(truncated)" if len(b) > 4000 else ""))
    return "\n\n---\n\n".join(out)


@mcp.tool()
def clear_logs(serial: Optional[str] = None) -> str:
    """Clear all logcat buffers. Do this before reproducing a bug so logs stay clean."""
    _adb(["logcat", "-c", "-b", "all"], serial=serial, timeout=20)
    return "Log buffers cleared. Reproduce the issue, then call read_logs or find_crashes."


# ------------------------------------------------- background capture session


class _Capture:
    def __init__(self):
        self.proc: Optional[subprocess.Popen] = None
        self.lines: deque[str] = deque(maxlen=BUFFER_SIZE)
        self.label = ""
        self.lock = threading.Lock()

    def start(self, label: str, serial: Optional[str]) -> None:
        self.stop()
        cmd = [ADB]
        s = serial or DEFAULT_SERIAL
        if s:
            cmd += ["-s", s]
        cmd += ["logcat", "-v", "threadtime"]
        self.lines.clear()
        self.label = label
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=1, text=True
        )
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()

    def _pump(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:  # type: ignore[union-attr]
            with self.lock:
                self.lines.append(line.rstrip("\n"))

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def snapshot(self) -> list[str]:
        with self.lock:
            return list(self.lines)


_capture = _Capture()


@mcp.tool()
def capture_start(label: str = "session", serial: Optional[str] = None) -> str:
    """Begin recording logcat into a background ring buffer.

    Use this when the user is about to reproduce a bug: start the capture, let them
    reproduce it, then query with capture_read. Unlike read_logs this survives log
    rotation and captures everything, including verbose output.
    """
    _capture.start(label, serial)
    return f"Capturing into ring buffer '{label}' (max {BUFFER_SIZE} lines). Reproduce the issue now, then call capture_read."


@mcp.tool()
def capture_read(
    package: Optional[str] = None,
    tag: Optional[str] = None,
    min_level: str = "V",
    contains: Optional[str] = None,
    limit: int = 100,
    stop: bool = False,
) -> str:
    """Query the background capture buffer with the same filters as read_logs.

    Args:
        stop: also end the capture after reading.
    """
    limit = max(1, min(limit, MAX_LINES))
    lines = _capture.snapshot()
    if stop:
        _capture.stop()
    if not lines:
        return "Capture buffer is empty. Did you call capture_start first?"
    pids = set(_pids_for(package, None)) if package else None
    entries = _filter(_parse(lines), pids, tag, min_level, contains)
    header = f"[buffer '{_capture.label}': {len(lines)} raw, {len(entries)} matching]"
    return header + "\n" + _render(entries, limit)


@mcp.tool()
def capture_stop() -> str:
    """Stop the background logcat capture and release the adb process."""
    _capture.stop()
    return "Capture stopped. The buffer is still readable with capture_read."


@mcp.tool()
def retrace(stacktrace: str, mapping_path: str) -> str:
    """De-obfuscate an R8/ProGuard stack trace using a mapping.txt file.

    Args:
        stacktrace: the obfuscated trace, copied from find_crashes output.
        mapping_path: path to mapping.txt, usually under
            app/build/outputs/mapping/<variant>/mapping.txt
    """
    if not os.path.isfile(mapping_path):
        return f"Mapping file not found: {mapping_path}"
    tool = shutil.which("retrace")
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write(stacktrace)
        trace_path = f.name
    try:
        if tool:
            cmd = [tool, mapping_path, trace_path]
        elif R8_JAR:
            cmd = ["java", "-cp", R8_JAR, "com.android.tools.r8.retrace.Retrace",
                   mapping_path, trace_path]
        else:
            return ("No retrace tool available. Install the R8 command line tools, "
                    "or set R8_JAR to the path of r8.jar.")
        p = subprocess.run(cmd, capture_output=True, timeout=120)
        out = p.stdout.decode(errors="replace")
        return out.strip() or p.stderr.decode(errors="replace").strip() or "(no output)"
    finally:
        os.unlink(trace_path)


# ---------------------------------------------------------------- device control


def _adb_bin(args: list[str], serial: Optional[str] = None, timeout: int = 60) -> bytes:
    """Like _adb but returns raw bytes — for screencap and other binary output."""
    cmd = [ADB]
    s = serial or DEFAULT_SERIAL
    if s:
        cmd += ["-s", s]
    cmd += args
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except FileNotFoundError:
        raise RuntimeError(f"adb not found at {ADB!r}. Set ADB_PATH.")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"adb timed out after {timeout}s: {' '.join(args)}")
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode(errors="replace").strip() or f"adb exited {p.returncode}")
    return p.stdout


BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")

KEYCODES = {
    "back": "KEYCODE_BACK",
    "home": "KEYCODE_HOME",
    "recents": "KEYCODE_APP_SWITCH",
    "enter": "KEYCODE_ENTER",
    "tab": "KEYCODE_TAB",
    "delete": "KEYCODE_DEL",
    "escape": "KEYCODE_ESCAPE",
    "search": "KEYCODE_SEARCH",
    "menu": "KEYCODE_MENU",
    "power": "KEYCODE_POWER",
    "wake": "KEYCODE_WAKEUP",
    "sleep": "KEYCODE_SLEEP",
    "volume_up": "KEYCODE_VOLUME_UP",
    "volume_down": "KEYCODE_VOLUME_DOWN",
    "camera": "KEYCODE_CAMERA",
    "dpad_up": "KEYCODE_DPAD_UP",
    "dpad_down": "KEYCODE_DPAD_DOWN",
    "dpad_left": "KEYCODE_DPAD_LEFT",
    "dpad_right": "KEYCODE_DPAD_RIGHT",
    "dpad_center": "KEYCODE_DPAD_CENTER",
}


def _screen_size(serial: Optional[str]) -> tuple[int, int]:
    out = _adb(["shell", "wm", "size"], serial=serial, timeout=15)
    # Prefer "Override size" when present — that is what is actually rendered.
    m = None
    for line in out.splitlines():
        got = re.search(r"(\d+)x(\d+)", line)
        if got and ("Override" in line or m is None):
            m = got
    if not m:
        raise RuntimeError(f"Could not parse screen size from: {out.strip()}")
    return int(m.group(1)), int(m.group(2))


def _nodes(serial: Optional[str]) -> list[dict]:
    """Dump the current view hierarchy and return the interesting nodes."""
    _adb(["shell", "uiautomator", "dump", "/sdcard/window_dump.xml"], serial=serial, timeout=60)
    xml = _adb_bin(["exec-out", "cat", "/sdcard/window_dump.xml"], serial=serial, timeout=30)
    try:
        root = ET.fromstring(xml.decode("utf-8", errors="replace"))
    except ET.ParseError as e:
        raise RuntimeError(f"Could not parse the UI dump: {e}")

    found = []
    for n in root.iter("node"):
        a = n.attrib
        text, desc = a.get("text", ""), a.get("content-desc", "")
        rid = a.get("resource-id", "").split("/")[-1]
        clickable = a.get("clickable") == "true"
        if not (text or desc or (rid and clickable)):
            continue
        m = BOUNDS_RE.match(a.get("bounds", ""))
        if not m:
            continue
        x1, y1, x2, y2 = (int(v) for v in m.groups())
        if x2 <= x1 or y2 <= y1:
            continue
        found.append({
            "text": text,
            "desc": desc,
            "id": rid,
            "cls": a.get("class", "").split(".")[-1],
            "clickable": clickable,
            "checked": a.get("checked") == "true",
            "enabled": a.get("enabled") != "false",
            "x": (x1 + x2) // 2,
            "y": (y1 + y2) // 2,
            "bounds": (x1, y1, x2, y2),
        })
    return found


def _describe(n: dict) -> str:
    label = n["text"] or n["desc"] or n["id"] or n["cls"]
    bits = [f'"{label}"' if (n["text"] or n["desc"]) else label]
    if n["id"] and label != n["id"]:
        bits.append(f"#{n['id']}")
    bits.append(f"({n['x']},{n['y']})")
    flags = []
    if n["clickable"]:
        flags.append("clickable")
    if n["checked"]:
        flags.append("checked")
    if not n["enabled"]:
        flags.append("disabled")
    if flags:
        bits.append("[" + ",".join(flags) + "]")
    return " ".join(bits)


@mcp.tool()
def screenshot(max_width: int = 900, serial: Optional[str] = None) -> Image:
    """Capture what is currently on screen. Use this to see the app before acting on it.

    The image is downscaled before sending, so coordinates read off it are NOT the
    device's own. Use ui_dump, or tap(text=...), to get real tap targets.

    Args:
        max_width: longest edge of the returned image in pixels.
        serial: device serial when several are attached.
    """
    png = _adb_bin(["exec-out", "screencap", "-p"], serial=serial, timeout=60)
    if not png:
        raise RuntimeError("screencap returned no data.")
    img = PILImage.open(io.BytesIO(png))
    if max(img.size) > max_width:
        ratio = max_width / max(img.size)
        img = img.resize((int(img.width * ratio), int(img.height * ratio)), PILImage.LANCZOS)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    return Image(data=buf.getvalue(), format="png")


@mcp.tool()
def ui_dump(contains: Optional[str] = None, clickable_only: bool = False,
            limit: int = 60, serial: Optional[str] = None) -> str:
    """List on-screen UI elements with their tap coordinates.

    Cheaper and more precise than a screenshot when you only need to find a control.

    Args:
        contains: case-insensitive filter on text, content description or id.
        clickable_only: only return elements that accept taps.
        limit: max elements returned.
        serial: device serial when several are attached.
    """
    nodes = _nodes(serial)
    if clickable_only:
        nodes = [n for n in nodes if n["clickable"]]
    if contains:
        needle = contains.lower()
        nodes = [n for n in nodes
                 if needle in n["text"].lower() or needle in n["desc"].lower()
                 or needle in n["id"].lower()]
    if not nodes:
        return "(no matching elements on screen)"
    shown = nodes[:limit]
    header = f"[{len(nodes)} elements, showing {len(shown)}]"
    return header + "\n" + "\n".join(_describe(n) for n in shown)


@mcp.tool()
def tap(x: Optional[int] = None, y: Optional[int] = None, text: Optional[str] = None,
        serial: Optional[str] = None) -> str:
    """Tap the screen, either at a coordinate or on the element matching `text`.

    Prefer `text`: it survives layout and resolution differences. Exact matches win
    over partial ones; if several elements match, none is tapped and they are listed
    so you can pick a coordinate instead.

    Args:
        x, y: device coordinates. Ignored when `text` is given.
        text: text, content description or resource id of the element to tap.
        serial: device serial when several are attached.
    """
    if text:
        needle = text.lower()
        nodes = _nodes(serial)
        exact = [n for n in nodes
                 if needle in (n["text"].lower(), n["desc"].lower(), n["id"].lower())]
        partial = [n for n in nodes
                   if needle in n["text"].lower() or needle in n["desc"].lower()
                   or needle in n["id"].lower()]
        hits = exact or partial
        if not hits:
            return f"Nothing on screen matches {text!r}. Call ui_dump to see what is there."
        # A clickable element is what the user means, even when the label sits on a child.
        clickable = [n for n in hits if n["clickable"]]
        if len(hits) > 1 and len(clickable) != 1 and len(exact) != 1:
            listing = "\n".join(_describe(n) for n in hits[:10])
            return f"{len(hits)} elements match {text!r} — tap by coordinate instead:\n{listing}"
        target = clickable[0] if len(clickable) == 1 else hits[0]
        x, y = target["x"], target["y"]
        label = _describe(target)
    elif x is None or y is None:
        return "Give either text, or both x and y."
    else:
        label = f"({x},{y})"
    _adb(["shell", "input", "tap", str(x), str(y)], serial=serial, timeout=20)
    return f"Tapped {label}."


@mcp.tool()
def swipe(direction: Optional[str] = None, x1: Optional[int] = None, y1: Optional[int] = None,
          x2: Optional[int] = None, y2: Optional[int] = None, duration_ms: int = 300,
          serial: Optional[str] = None) -> str:
    """Swipe or scroll. Give a direction for a centred swipe, or explicit coordinates.

    Args:
        direction: up, down, left or right. "up" scrolls the content up (reveals what
            is below), matching how a finger moves.
        x1, y1, x2, y2: start and end points, used when direction is omitted.
        duration_ms: swipe duration; raise it for a slow drag, lower it to fling.
        serial: device serial when several are attached.
    """
    if direction:
        d = direction.lower()
        w, h = _screen_size(serial)
        cx, cy = w // 2, h // 2
        span_y, span_x = int(h * 0.3), int(w * 0.3)
        moves = {
            "up": (cx, cy + span_y, cx, cy - span_y),
            "down": (cx, cy - span_y, cx, cy + span_y),
            "left": (cx + span_x, cy, cx - span_x, cy),
            "right": (cx - span_x, cy, cx + span_x, cy),
        }
        if d not in moves:
            return f"Unknown direction {direction!r}. Use up, down, left or right."
        x1, y1, x2, y2 = moves[d]
        what = f"Swiped {d}"
    elif None in (x1, y1, x2, y2):
        return "Give either a direction, or all of x1, y1, x2, y2."
    else:
        what = f"Swiped ({x1},{y1}) to ({x2},{y2})"
    _adb(["shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration_ms)],
         serial=serial, timeout=30)
    return f"{what}."


@mcp.tool()
def input_text(text: str, submit: bool = False, clear: bool = False,
               serial: Optional[str] = None) -> str:
    """Type into the focused field. Tap the field first so it has focus.

    Args:
        text: the text to type. ASCII only — adb cannot type emoji or most non-Latin text.
        submit: press Enter afterwards.
        clear: delete the field's existing contents first.
        serial: device serial when several are attached.
    """
    if clear:
        _adb(["shell", "input", "keyevent", "KEYCODE_MOVE_END"], serial=serial, timeout=15)
        _adb(["shell", "input", "keyevent"] + ["KEYCODE_DEL"] * 60, serial=serial, timeout=40)
    # `input text` throws on non-ASCII rather than skipping it, so drop it here.
    dropped = "".join(c for c in text if ord(c) > 127)
    typed = "".join(c for c in text if ord(c) < 128)
    if typed:
        # input text reads spaces as %s and chokes on shell metacharacters.
        escaped = typed.replace("%", "%%").replace(" ", "%s")
        escaped = re.sub(r"([\"'$`\\&|;<>()*?~#!])", r"\\\1", escaped)
        _adb(["shell", "input", "text", escaped], serial=serial, timeout=30)
    if submit:
        _adb(["shell", "input", "keyevent", "KEYCODE_ENTER"], serial=serial, timeout=15)
    note = f" Dropped {dropped!r} — adb cannot type non-ASCII." if dropped else ""
    if not typed and not submit:
        return f"Nothing typed.{note}"
    return f"Typed {typed!r}{' and pressed Enter' if submit else ''}.{note}"


@mcp.tool()
def press_key(key: str, serial: Optional[str] = None) -> str:
    """Press a hardware or navigation key.

    Args:
        key: back, home, recents, enter, tab, delete, escape, search, menu, power,
            wake, sleep, volume_up, volume_down, camera, or dpad_up/down/left/right/center.
            A raw KEYCODE_* name or a numeric keycode also works.
        serial: device serial when several are attached.
    """
    k = KEYCODES.get(key.lower().strip())
    if not k:
        raw = key.strip().upper()
        if raw.startswith("KEYCODE_") or raw.isdigit():
            k = raw
        else:
            return f"Unknown key {key!r}. Known keys: {', '.join(sorted(KEYCODES))}."
    _adb(["shell", "input", "keyevent", k], serial=serial, timeout=20)
    return f"Pressed {key}."


@mcp.tool()
def launch_app(package: str, activity: Optional[str] = None, clear_data: bool = False,
               serial: Optional[str] = None) -> str:
    """Launch an app, optionally from a clean state.

    Args:
        package: application id, e.g. com.example.app.
        activity: fully qualified activity to start instead of the launcher entry point.
        clear_data: wipe the app's data first, for a true first-run test.
        serial: device serial when several are attached.
    """
    if clear_data:
        _adb(["shell", "pm", "clear", package], serial=serial, timeout=60)
    if activity:
        comp = activity if "/" in activity else f"{package}/{activity}"
        _adb(["shell", "am", "start", "-n", comp], serial=serial, timeout=60)
    else:
        _adb(["shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"],
             serial=serial, timeout=60)
    state = " from cleared data" if clear_data else ""
    return f"Launched {package}{state}. Take a screenshot or call ui_dump to see where it landed."


@mcp.tool()
def stop_app(package: str, serial: Optional[str] = None) -> str:
    """Force-stop an app. Pair with launch_app to test a cold start.

    Args:
        package: application id, e.g. com.example.app.
        serial: device serial when several are attached.
    """
    _adb(["shell", "am", "force-stop", package], serial=serial, timeout=30)
    return f"Force-stopped {package}."


@mcp.tool()
def device_info(serial: Optional[str] = None) -> str:
    """Report screen size, density, Android version and the foreground activity."""
    w, h = _screen_size(serial)
    def prop(name: str) -> str:
        try:
            return _adb(["shell", "getprop", name], serial=serial, timeout=15).strip() or "?"
        except RuntimeError:
            return "?"
    density = "?"
    try:
        m = re.search(r"(\d+)", _adb(["shell", "wm", "density"], serial=serial, timeout=15))
        density = m.group(1) if m else "?"
    except RuntimeError:
        pass
    focus = "?"
    try:
        dump = _adb(["shell", "dumpsys", "activity", "activities"], serial=serial, timeout=30)
        m = re.search(r"ResumedActivity[=:]\s*ActivityRecord\{\S+\s+\S+\s+(\S+/\S+)", dump)
        focus = m.group(1) if m else "?"
    except RuntimeError:
        pass
    return (f"screen: {w}x{h} @ {density}dpi\n"
            f"android: {prop('ro.build.version.release')} (API {prop('ro.build.version.sdk')})\n"
            f"model: {prop('ro.product.model')}\n"
            f"foreground: {focus}")


# ---------------------------------------------------- Flutter <-> Android bridge
#
# Android tooling sees a Flutter app as one opaque SurfaceView; the Dart tooling
# sees widgets but knows nothing about logcat, ANRs or native crashes. These tools
# join the two: screen coordinates come from the Android semantics tree, identity
# and source locations come from the Dart VM Service.

VM_URI_RE = re.compile(r"Dart VM service is listening on (http://127\.0\.0\.1:(\d+)/(\S*))")
FLUTTER_TIMEOUT = int(os.environ.get("FLUTTER_MCP_TIMEOUT", "30"))


class _VMService:
    """Discovers, forwards and talks to a debug-build Flutter app's VM Service."""

    def __init__(self):
        self.ws_uri: Optional[str] = None
        self.package: Optional[str] = None

    def discover(self, package: Optional[str], serial: Optional[str], relaunch: bool) -> str:
        if relaunch and package:
            _adb(["shell", "am", "force-stop", package], serial=serial, timeout=30)
            _adb(["logcat", "-c", "-b", "all"], serial=serial, timeout=20)
            _adb(["shell", "monkey", "-p", package, "-c",
                  "android.intent.category.LAUNCHER", "1"], serial=serial, timeout=60)

        deadline = time.time() + (25 if relaunch else 3)
        match = None
        while time.time() < deadline:
            # Scan far wider than SCAN_LINES: the URI is printed once, at launch.
            raw = _adb(["logcat", "-d", "-t", "200000"], serial=serial, timeout=60)
            found = VM_URI_RE.findall(raw)
            if found:
                # Old launches leave stale lines behind, so try newest first and keep
                # the first port that actually answers.
                for cand in reversed(found):
                    local = self._forward(cand[1], serial)
                    self.ws_uri = f"ws://127.0.0.1:{local}/{cand[2]}ws"
                    try:
                        asyncio.run(self._call(self.ws_uri, "getVM", {}))
                        return self.ws_uri
                    except Exception:
                        self.ws_uri = None
                match = None
            time.sleep(1.5)
        if not match:
            raise RuntimeError(
                "No Dart VM Service found in logcat. It is only printed at launch, and "
                "only by debug/profile builds. Call flutter_connect(package=..., "
                "relaunch=True) to restart the app and capture it, and check this is not "
                "a release build."
            )
        raise RuntimeError("Unreachable")

    def _forward(self, device_port: str, serial: Optional[str]) -> int:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            local = s.getsockname()[1]
        _adb(["forward", f"tcp:{local}", f"tcp:{device_port}"], serial=serial, timeout=20)
        return local

    def uri(self, package: Optional[str], serial: Optional[str]) -> str:
        if not self.ws_uri:
            self.discover(package, serial, relaunch=False)
        return self.ws_uri  # type: ignore[return-value]

    def call(self, method: str, params: Optional[dict] = None,
             package: Optional[str] = None, serial: Optional[str] = None,
             retry: bool = True) -> dict:
        """One JSON-RPC round trip. Reconnects once if the app was restarted."""
        try:
            return asyncio.run(self._call(self.uri(package, serial), method, params or {}))
        except RuntimeError:
            raise
        except Exception:
            if not retry:
                raise RuntimeError(
                    "Lost the VM Service connection. The app may have been restarted — "
                    "call flutter_connect again."
                )
            self.ws_uri = None
            return self.call(method, params, package, serial, retry=False)

    async def _call(self, uri: str, method: str, params: dict) -> dict:
        import websockets
        async with websockets.connect(uri, max_size=None,
                                      open_timeout=FLUTTER_TIMEOUT) as ws:
            async def rpc(m: str, p: dict) -> dict:
                await ws.send(json.dumps({"jsonrpc": "2.0", "id": "1", "method": m, "params": p}))
                raw = await asyncio.wait_for(ws.recv(), timeout=FLUTTER_TIMEOUT)
                msg = json.loads(raw)
                if "error" in msg:
                    raise RuntimeError(f"{m} failed: {msg['error'].get('message', msg['error'])}")
                return msg.get("result", {})

            if method == "getVM":
                return await rpc("getVM", params)
            vm = await rpc("getVM", {})
            isolates = vm.get("isolates") or []
            if not isolates:
                raise RuntimeError("The app has no running Dart isolate.")
            params = dict(params, isolateId=isolates[0]["id"])
            return await rpc(method, params)


_vm = _VMService()


def _widget_nodes(package: Optional[str], serial: Optional[str],
                  local_only: bool = True) -> list[dict]:
    """Flatten the summary widget tree, keeping depth and source location."""
    res = _vm.call(
        "ext.flutter.inspector.getRootWidgetTree",
        {"groupName": "mcp", "isSummaryTree": "true", "withPreviews": "true",
         "fullDetails": "true"},
        package, serial,
    )
    root = res.get("result") or {}

    out: list[dict] = []

    def walk(n: dict, depth: int) -> None:
        loc = n.get("creationLocation") or {}
        preview = n.get("textPreview") or ""
        out.append({
            "id": n.get("valueId"),
            "type": n.get("widgetRuntimeType") or "",
            "text": preview,
            "desc": ((n.get("description") or "") +
                     (f' "{preview}"' if preview else "")).strip(),
            "local": bool(n.get("createdByLocalProject")),
            "file": (loc.get("file") or "").replace("file://", ""),
            "line": loc.get("line"),
            "depth": depth,
        })
        for c in n.get("children") or []:
            walk(c, depth + 1)

    walk(root, 0)
    return [n for n in out if n["local"]] if local_only else out


def _short_path(path: str) -> str:
    """Trim an absolute Dart path down to the part inside the project."""
    if not path:
        return ""
    for marker in ("/lib/", "/test/"):
        if marker in path:
            return marker.strip("/") + "/" + path.split(marker, 1)[1]
    return os.path.basename(path)


def _fmt_widget(n: dict, with_src: bool = True) -> str:
    label = n["desc"] or n["type"]
    if len(label) > 90:
        label = label[:90] + "…"
    if with_src and n["file"]:
        return f"{label}  →  {_short_path(n['file'])}:{n['line']}"
    return label


@mcp.tool()
def flutter_connect(package: Optional[str] = None, relaunch: bool = False,
                    serial: Optional[str] = None) -> str:
    """Connect to a running Flutter app's Dart VM Service. Call this first.

    The VM Service URI is printed to logcat only at launch, and only by debug and
    profile builds. If it has already scrolled out of the buffer, pass relaunch=True
    to restart the app and capture it.

    Args:
        package: application id, needed for relaunch.
        relaunch: force-stop and restart the app to capture a fresh URI.
        serial: device serial when several are attached.
    """
    _vm.ws_uri = None
    _vm.discover(package, serial, relaunch)
    vm = _vm.call("getVM", {}, package, serial)
    isolates = ", ".join(i.get("name", "?") for i in vm.get("isolates", []))
    nodes = _widget_nodes(package, serial)
    files = {n["file"] for n in nodes if n["file"]}
    return (f"Connected to the Dart VM Service ({vm.get('version', '?')}).\n"
            f"isolates: {isolates}\n"
            f"{len(nodes)} widgets from your project, across {len(files)} source files.")


@mcp.tool()
def flutter_widget_tree(contains: Optional[str] = None, max_depth: int = 0,
                        limit: int = 60, include_framework: bool = False,
                        package: Optional[str] = None, serial: Optional[str] = None) -> str:
    """Show the live widget tree with the source location of each widget.

    Only widgets from your own project are shown by default — the framework's own
    wrappers are noise. Each line ends with the file and line that built it.

    Args:
        contains: case-insensitive filter on widget type or description.
        max_depth: 0 for no limit, otherwise prune deeper than this.
        limit: max widgets returned.
        include_framework: also show widgets from Flutter and third-party packages.
        package, serial: as elsewhere.
    """
    nodes = _widget_nodes(package, serial, local_only=not include_framework)
    if max_depth:
        nodes = [n for n in nodes if n["depth"] <= max_depth]
    if contains:
        needle = contains.lower()
        nodes = [n for n in nodes if needle in n["type"].lower() or needle in n["desc"].lower()]
    if not nodes:
        return "(no matching widgets — is the app on the screen you expect?)"
    base = min(n["depth"] for n in nodes)
    shown = nodes[:limit]
    body = "\n".join("  " * min(n["depth"] - base, 12) + _fmt_widget(n) for n in shown)
    return f"[{len(nodes)} widgets, showing {len(shown)}]\n{body}"


@mcp.tool()
def flutter_locate(text: str, package: Optional[str] = None,
                   serial: Optional[str] = None) -> str:
    """Find something on screen and report both how to tap it and where it lives in code.

    This is the bridge: tap coordinates come from Android's semantics tree, while the
    widget type and source location come from the Dart VM Service. Use it to go from
    "this looks wrong on screen" to the exact line that built it.

    Args:
        text: visible text, semantics label or widget type to look for.
        package, serial: as elsewhere.
    """
    needle = text.lower()
    native = [n for n in _nodes(serial)
              if needle in n["text"].lower() or needle in n["desc"].lower()
              or needle in n["id"].lower()]
    widgets = [n for n in _widget_nodes(package, serial)
               if needle in n["desc"].lower() or needle in n["type"].lower()]

    if not native and not widgets:
        return (f"Nothing matches {text!r} on screen or in the widget tree. "
                f"Flutter only exposes a widget to Android when it has semantics — "
                f"try flutter_widget_tree to see what is actually built.")

    out = []
    if native:
        out.append("On screen (tap these):")
        out += [f"  {_describe(n)}" for n in native[:6]]
    else:
        out.append("On screen: nothing — this widget has no semantics, so Android "
                   "cannot see it and tap(text=...) will not find it.")
    if widgets:
        out.append("\nIn your code:")
        out += [f"  {_fmt_widget(w)}" for w in widgets[:6]]
    else:
        out.append("\nIn your code: no matching widget — the text may come from a "
                   "plugin, a platform view, or be drawn on a canvas.")
    return "\n".join(out)


@mcp.tool()
def flutter_diagnose(package: Optional[str] = None, limit: int = 5,
                     serial: Optional[str] = None) -> str:
    """Correlate Dart-side errors with the native Android log around them.

    A Flutter failure usually leaves two unrelated-looking traces: a Dart exception
    and, for anything crossing a platform channel, a Java/Kotlin one in logcat. This
    pulls both and puts them next to each other in time order.

    Args:
        package: app to restrict native logs to, e.g. com.example.app.
        limit: how many Dart errors to report.
        serial: device serial when several are attached.
    """
    raw = _adb(["logcat", "-d", "-v", "threadtime", "-t", str(SCAN_LINES)],
               serial=serial, timeout=60)
    entries = _parse(raw.splitlines())
    app_pids: set[str] = set()
    if package:
        try:
            app_pids = set(_pids_for(package, serial))
        except RuntimeError:
            pass

    # A Flutter error is many consecutive `flutter` lines fenced by box drawing,
    # so collect whole blocks rather than the individual lines.
    dart, native = [], []
    block: Optional[dict] = None
    for e in entries:
        blob = e["msg"]
        if e["tag"] == "flutter":
            if "╔" in blob or "EXCEPTION CAUGHT" in blob:
                if block:
                    dart.append(block)
                block = {"ts": e["ts"], "msg": blob}
                continue
            if block is not None:
                block["msg"] += "\n" + blob
                if "╚" in blob:
                    dart.append(block)
                    block = None
                continue
            if "Unhandled" in blob or "Error:" in blob or "Exception" in blob:
                dart.append({"ts": e["ts"], "msg": blob})
        elif e["level"] in ("E", "F") and e["tag"] != "flutter":
            # Match on pid: the app's own name rarely appears in a native trace.
            if not app_pids or e["pid"] in app_pids or package and package in blob:
                native.append(e)

    if block:
        dart.append(block)
    if not dart and not native:
        return ("No Dart or native errors in the log buffer. Call clear_logs, "
                "reproduce the problem, then run this again.")

    out = []
    if dart:
        out.append(f"=== Dart errors ({len(dart)}, showing {min(limit, len(dart))}) ===")
        for e in dart[-limit:]:
            msg = e["msg"][:1500]
            out.append(f"{e['ts']}  {msg}")
    else:
        out.append("=== Dart errors: none ===")

    if native:
        out.append(f"\n=== Native errors ({len(native)}, showing last {min(limit * 2, len(native))}) ===")
        for e in native[-(limit * 2):]:
            out.append(f"{e['ts']} {e['level']}/{e['tag']}: {e['msg'][:400]}")
        out.append("\nIf a Dart error and a native error share a timestamp, the failure "
                   "most likely crossed a platform channel — check the plugin involved.")
    else:
        out.append("\n=== Native errors: none — this failure stayed inside Dart ===")
    return "\n".join(out)


# ---------------------------------------------------------------- visual verify
#
# The point of a checkpoint is to answer "did my edit change what I intended, and
# nothing else?" — a question a screenshot alone cannot answer.

_checkpoints: dict[str, bytes] = {}


def _raw_screen(serial: Optional[str]) -> "PILImage.Image":
    png = _adb_bin(["exec-out", "screencap", "-p"], serial=serial, timeout=60)
    if not png:
        raise RuntimeError("screencap returned no data.")
    return PILImage.open(io.BytesIO(png)).convert("RGB")


@mcp.tool()
def ui_checkpoint(label: str = "before", serial: Optional[str] = None) -> str:
    """Remember the current screen so a later ui_diff can show what changed.

    Take one before editing code, then call ui_diff after hot reloading.

    Args:
        label: name for this checkpoint.
        serial: device serial when several are attached.
    """
    img = _raw_screen(serial)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    _checkpoints[label] = buf.getvalue()
    return f"Checkpoint {label!r} saved ({img.width}x{img.height}). Make your change, then call ui_diff."


@mcp.tool()
def ui_diff(label: str = "before", threshold: int = 12, max_width: int = 900,
            serial: Optional[str] = None) -> list:
    """Compare the screen now against a checkpoint and highlight what moved.

    Returns a summary plus the current screen with changed regions outlined, so you
    can confirm an edit did what you meant and did not disturb anything else.

    Args:
        label: which checkpoint to compare against.
        threshold: per-channel difference (0-255) that counts as a real change.
            Raise it to ignore animations and anti-aliasing.
        max_width: longest edge of the returned image.
        serial: device serial when several are attached.
    """
    if label not in _checkpoints:
        have = ", ".join(sorted(_checkpoints)) or "none"
        return [f"No checkpoint named {label!r}. Saved checkpoints: {have}."]

    before = PILImage.open(io.BytesIO(_checkpoints[label])).convert("RGB")
    after = _raw_screen(serial)
    if before.size != after.size:
        return [f"Screen size changed ({before.size} → {after.size}); cannot compare. "
                f"Take a fresh checkpoint."]

    diff = ImageChops.difference(before, after).convert("L")
    mask = diff.point(lambda v: 255 if v > threshold else 0)
    bbox = mask.getbbox()
    changed = sum(mask.point(lambda v: 1 if v else 0).getdata())
    total = before.width * before.height
    pct = 100.0 * changed / total

    if not bbox or changed == 0:
        return [f"No visible change since checkpoint {label!r}. "
                f"If you expected one, the hot reload may not have applied — "
                f"check flutter_hot_reload output."]

    # Outline each changed region on a copy of the current screen.
    shot = after.copy()
    draw = ImageDraw.Draw(shot)
    regions = _diff_regions(mask)
    for (x1, y1, x2, y2) in regions[:12]:
        draw.rectangle([x1 - 2, y1 - 2, x2 + 2, y2 + 2], outline=(255, 0, 0), width=4)

    if max(shot.size) > max_width:
        r = max_width / max(shot.size)
        shot = shot.resize((int(shot.width * r), int(shot.height * r)), PILImage.LANCZOS)
    buf = io.BytesIO()
    shot.save(buf, format="PNG", optimize=True)

    lines = [f"{pct:.2f}% of pixels changed since {label!r}, in {len(regions)} region(s).",
             f"Overall bounds: {bbox}."]
    for i, (x1, y1, x2, y2) in enumerate(regions[:8], 1):
        lines.append(f"  {i}. ({x1},{y1})-({x2},{y2})  {x2 - x1}x{y2 - y1}px")
    if pct > 60:
        lines.append("Most of the screen changed — this looks like a navigation or "
                     "rebuild, not a local edit.")
    return ["\n".join(lines), Image(data=buf.getvalue(), format="png")]


def _diff_regions(mask: "PILImage.Image", grid: int = 40) -> list[tuple[int, int, int, int]]:
    """Cluster changed pixels into coarse boxes, so one box per changed element."""
    w, h = mask.size
    cols, rows = max(1, w // grid), max(1, h // grid)
    small = mask.resize((cols, rows), PILImage.BOX)
    hot = {(x, y) for y in range(rows) for x in range(cols) if small.getpixel((x, y)) > 8}
    boxes, seen = [], set()
    for cell in sorted(hot):
        if cell in seen:
            continue
        stack, group = [cell], []
        seen.add(cell)
        while stack:  # flood fill over adjacent hot cells
            cx, cy = stack.pop()
            group.append((cx, cy))
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                n = (cx + dx, cy + dy)
                if n in hot and n not in seen:
                    seen.add(n)
                    stack.append(n)
        xs = [c[0] for c in group]
        ys = [c[1] for c in group]
        boxes.append((min(xs) * grid, min(ys) * grid,
                      min(w, (max(xs) + 1) * grid), min(h, (max(ys) + 1) * grid)))
    boxes.sort(key=lambda b: (b[2] - b[0]) * (b[3] - b[1]), reverse=True)
    return boxes


# ------------------------------------------------- managed flutter attach session
#
# Hot reload needs a Dart compiler, which the VM Service alone does not provide.
# The Flutter tool has one, so the server owns an attach process and drives it the
# way a developer would: by typing `r`.

FLUTTER_BIN = os.environ.get("FLUTTER_PATH") or shutil.which("flutter") or "flutter"
READY_MARK = "Flutter run key commands"


class _FlutterSession:
    def __init__(self):
        self.proc: Optional[subprocess.Popen] = None
        self.lines: deque[str] = deque(maxlen=2000)
        self.project: Optional[str] = None
        self.lock = threading.Lock()

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _pump(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:  # type: ignore[union-attr]
            with self.lock:
                self.lines.append(line.rstrip("\n"))

    def tail(self, since: int = 0) -> list[str]:
        with self.lock:
            return list(self.lines)[since:]

    def count(self) -> int:
        with self.lock:
            return len(self.lines)

    def start(self, project: str, serial: Optional[str], timeout: int = 180) -> str:
        self.stop()
        if not os.path.isdir(os.path.join(project, "lib")):
            raise RuntimeError(f"{project} does not look like a Flutter project (no lib/).")
        cmd = [FLUTTER_BIN, "attach"]
        s = serial or DEFAULT_SERIAL
        if s:
            cmd += ["-d", s]
        self.lines.clear()
        self.project = project
        self.proc = subprocess.Popen(
            cmd, cwd=project, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, bufsize=1,
        )
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()

        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.alive():
                raise RuntimeError("flutter attach exited:\n" + "\n".join(self.tail()[-15:]))
            if any(READY_MARK in l for l in self.tail()):
                return "\n".join(l for l in self.tail() if l.strip())[-800:]
            time.sleep(1)
        out = "\n".join(self.tail()[-15:])
        self.stop()
        raise RuntimeError(
            "flutter attach did not become ready in time. Is the app running, and is it "
            f"a debug build?\n{out}"
        )

    def send(self, key: str, done: tuple[str, ...], timeout: int = 120) -> str:
        if not self.alive():
            raise RuntimeError("No attach session. Call flutter_attach first.")
        start = self.count()
        assert self.proc and self.proc.stdin
        self.proc.stdin.write(key + "\n")
        self.proc.stdin.flush()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if any(any(d in l for d in done) for l in self.tail(start)):
                time.sleep(0.4)  # let the last lines land
                return "\n".join(l for l in self.tail(start) if l.strip())
            if not self.alive():
                return "\n".join(self.tail(start)) + "\n(the attach session exited)"
            time.sleep(0.5)
        return "\n".join(self.tail(start)) or "(no output before timeout)"

    def stop(self) -> None:
        if self.alive():
            assert self.proc
            try:
                if self.proc.stdin:
                    self.proc.stdin.write("d\n")  # detach, leaving the app running
                    self.proc.stdin.flush()
                self.proc.wait(timeout=8)
            except Exception:
                self.proc.kill()
        self.proc = None


_session = _FlutterSession()

RELOAD_DONE = ("Reloaded ", "Restarted ", "Error", "error:", "Try again", "Unable to")


@mcp.tool()
def flutter_attach(project_dir: str, serial: Optional[str] = None) -> str:
    """Attach the Flutter tool to the running app so hot reload becomes possible.

    Needed once per session before flutter_hot_reload: the VM Service on its own has
    no Dart compiler, so source edits cannot be applied without this.

    Args:
        project_dir: the Flutter project root, the directory holding pubspec.yaml.
        serial: device serial when several are attached.
    """
    out = _session.start(os.path.expanduser(project_dir), serial)
    # The attach session publishes its own VM Service URI; adopt it. Read it from the
    # untruncated session log — `out` is clipped and would yield a half-written port.
    # flutter attach sets up its own forward, so this port is already host-side —
    # forwarding it again would point at an unrelated port on the device.
    m = re.search(r"http://127\.0\.0\.1:(\d+)/(\S*/)", "\n".join(_session.tail()))
    if m:
        _vm.ws_uri = f"ws://127.0.0.1:{m.group(1)}/{m.group(2)}ws"
    return "Attached. Hot reload is now available.\n" + out


@mcp.tool()
def flutter_hot_reload(full_restart: bool = False, package: Optional[str] = None,
                       serial: Optional[str] = None) -> str:
    """Apply Dart source edits to the running app.

    Requires flutter_attach first. Use full_restart after changing initState, global
    state or main(), which a plain reload cannot pick up.

    Args:
        full_restart: hot restart instead of hot reload.
        package, serial: as elsewhere.
    """
    if not _session.alive():
        return ("No attach session — hot reload needs the Flutter tool's compiler. "
                "Call flutter_attach(project_dir=...) first.")
    out = _session.send("R" if full_restart else "r", RELOAD_DONE)
    low = out.lower()
    if "error" in low or "unable to" in low:
        return f"Hot {'restart' if full_restart else 'reload'} FAILED:\n{out}"
    return (f"Hot {'restart' if full_restart else 'reload'} applied.\n{out}\n"
            f"Call ui_diff to confirm the screen changed the way you intended.")


@mcp.tool()
def flutter_detach() -> str:
    """End the attach session, leaving the app running on the device."""
    if not _session.alive():
        return "No attach session was running."
    _session.stop()
    return "Detached. The app is still running."


def main() -> None:
    """Console-script entry point (stdio transport)."""
    mcp.run()


if __name__ == "__main__":
    main()
