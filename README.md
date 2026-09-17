# flutter-bridge-mcp

**Your Android tools see a Flutter app as one opaque rectangle. This makes that
rectangle readable.** Point at wrong-looking text on screen and get back the file and
line that built it, edit it, hot reload, then diff the pixels to prove only that
changed.

It also does the Android basics — logcat, crashes, taps, screenshots — filtered and
truncated server-side so a response can never blow up the model's context.

## Tools

### Reading logs

| Tool | Purpose |
| --- | --- |
| `list_devices` | Connected devices/emulators |
| `read_logs` | Recent logcat, filtered by package / tag / level / substring |
| `find_crashes` | Whole stack traces from the crash buffer |
| `clear_logs` | Wipe buffers before reproducing a bug |
| `capture_start` / `capture_read` / `capture_stop` | Background ring-buffer capture |
| `retrace` | De-obfuscate an R8/ProGuard trace with `mapping.txt` |

### Driving the device

| Tool | Purpose |
| --- | --- |
| `screenshot` | See the screen (downscaled before sending) |
| `ui_dump` | List on-screen elements with tap coordinates |
| `tap` | Tap by element text, or by coordinate |
| `swipe` | Scroll or drag, by direction or coordinates |
| `input_text` | Type into the focused field, optionally clearing or submitting |
| `press_key` | back, home, recents, enter, dpad, volume, power… |
| `launch_app` / `stop_app` | Cold starts, and `clear_data` for first-run tests |
| `device_info` | Screen size, density, Android version, foreground activity |

### Flutter ↔ Android bridge

Android tooling sees a Flutter app as one opaque `SurfaceView`; Dart tooling sees
widgets but knows nothing about logcat, ANRs or native crashes. These join the two.

| Tool | Purpose |
| --- | --- |
| `flutter_connect` | Attach to the running app's Dart VM Service |
| `flutter_widget_tree` | Live widget tree, each widget tagged with its source file and line |
| `flutter_locate` | **The bridge** — tap coordinates *and* the code that built the widget |
| `flutter_diagnose` | Dart exceptions correlated with the native errors around them |
| `flutter_attach` | Attach the Flutter tool so hot reload works |
| `flutter_hot_reload` | Apply Dart edits to the running app |
| `flutter_detach` | End the attach session, leaving the app running |

### The loop

The point of the bridge is this cycle, which no other Android or Flutter MCP closes:

```
screenshot          see the bug
flutter_locate      -> "Total Patients" at (410,269), built by
                       lib/presentation/dashboard/widgets/dashboard_grid.dart:76
ui_checkpoint       remember the screen
<edit that line>
flutter_hot_reload  apply it
ui_diff             confirm exactly what changed, and nothing else
```

`ui_diff` reports the percentage of pixels that changed, clusters them into regions,
and returns the screen with those regions outlined — so "did my fix land, and did it
disturb anything else?" is answered rather than eyeballed.

| Tool | Purpose |
| --- | --- |
| `ui_checkpoint` | Remember the current screen |
| `ui_diff` | Compare now against a checkpoint, outlining what moved |

Requires a **debug or profile build**. The VM Service URI is printed to logcat only at
launch, so if it has scrolled away use `flutter_connect(package=..., relaunch=True)`.

Prefer `ui_dump` and `tap(text=...)` over screenshots and raw coordinates: it is far
cheaper, and it does not break when the layout or resolution changes. Screenshots are
downscaled, so coordinates read off one are *not* device coordinates.

There is deliberately no arbitrary `adb shell` tool.

## Install

It needs Python 3.10+ and `adb` — from Android Studio's SDK, or `platform-tools`.

```bash
pipx install flutter-bridge-mcp
```

[pipx](https://pipx.pypa.io) puts the server in its own virtualenv and the
`flutter-bridge-mcp` executable on your PATH, which is what the MCP configs below
expect. On Debian/Ubuntu, `sudo apt install pipx && pipx ensurepath` first.

A plain `pip install` works too, but on Debian, Ubuntu and recent Fedora it fails
with `error: externally-managed-environment` — those distros forbid pip from writing
into the system Python ([PEP 668](https://peps.python.org/pep-0668/)). Use pipx, or a
virtualenv of your own:

```bash
python3 -m venv ~/.venvs/flutter-bridge
~/.venvs/flutter-bridge/bin/pip install flutter-bridge-mcp
```

Then point your MCP config at `~/.venvs/flutter-bridge/bin/flutter-bridge-mcp`
rather than the bare command. Do not reach for `--break-system-packages`; it writes
into the Python your package manager owns.

From a clone, to hack on it:

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

The executable is then `.venv/bin/flutter-bridge-mcp`.

## Tests

The unit suite covers log parsing, error grouping and widget-tree flattening, and
needs no device:

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[test]"
.venv/bin/python -m pytest
```

`selftest.py` is the other half — it exercises the real thing against an attached
device:

```bash
.venv/bin/python selftest.py --package com.example.app --project ~/StudioProjects/app
```

Without `--package` it checks the Android side only; `--project` adds hot reload.

## Use in Claude Code (including the Claude plugin in Android Studio)

The Studio plugin reads the Claude Code CLI config, so registering it once covers both:

```bash
claude mcp add flutter-bridge flutter-bridge-mcp \
  -e ADB_PATH=/abs/path/to/Android/Sdk/platform-tools/adb
```

If you installed into a virtualenv rather than with pipx, use that venv's absolute
path in place of the bare `flutter-bridge-mcp`.

## Use with Gemini in Android Studio

Create `mcp.json` in the Studio config directory
(`~/.config/Google/AndroidStudio<version>/mcp.json` on Linux,
`~/Library/Application Support/Google/AndroidStudio<version>/` on macOS):

```json
{
  "mcpServers": {
    "flutter-bridge": {
      "command": "/abs/path/to/flutter-bridge-mcp",
      "args": [],
      "env": { "ADB_PATH": "/abs/path/to/Android/Sdk/platform-tools/adb" }
    }
  }
}
```

Restart Studio, then in Gemini → **Agent** mode check the tools menu for `flutter-bridge`.

## Configuration

| Env var | Default | Meaning |
| --- | --- | --- |
| `ADB_PATH` | `adb` on PATH | adb binary |
| `ANDROID_SERIAL` | — | Default device when several are attached |
| `FLUTTER_BRIDGE_MAX_LINES` | 200 | Hard cap on returned lines |
| `FLUTTER_BRIDGE_MAX_MSG` | 400 | Per-message truncation |
| `FLUTTER_BRIDGE_SCAN_LINES` | 8000 | Lines pulled from logcat before filtering |
| `FLUTTER_BRIDGE_BUFFER` | 40000 | Ring-buffer size for `capture_start` |
| `FLUTTER_BRIDGE_TIMEOUT` | 30 | Seconds to wait on a Dart VM Service call |
| `FLUTTER_PATH` | `flutter` on PATH | flutter binary, for `flutter_attach` |
| `R8_JAR` | — | Path to `r8.jar` if `retrace` is not on PATH |

## License

MIT — see [LICENSE](LICENSE).
