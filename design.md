# Design notes

Why this server exists, how it works, and where it sits relative to the other
MCP servers in this space. Written for anyone deciding whether to use it,
extend it, or rebuild the idea somewhere else.

## The problem

Flutter paints its entire UI into a single Android `SurfaceView`. Everything
Android's tooling knows how to do — `uiautomator dump`, accessibility
inspection, coordinate-based tapping — sees one opaque rectangle. Meanwhile the
Dart side knows every widget, its type and the source line that built it, but
has never heard of logcat, ANRs or native crashes.

So a wrong-looking pixel on screen and the Dart line responsible for it sit on
opposite sides of a boundary with no shared identifier. Answering "what built
this, and did my fix work?" means reading two trees in two coordinate systems
and joining them by hand.

This server does that join.

## Prior art, and what is actually different here

There are good tools on either side of the boundary. None of them cross it.

| Project | Provides | Does not provide |
| --- | --- | --- |
| [Official Dart/Flutter MCP](https://github.com/dart-lang/ai/tree/main/pkgs/dart_mcp_server) — `widget_inspector`, `hot_reload`, `hot_restart`, `get_runtime_errors`, `get_app_logs` | Dart-side inspection and reload | Screenshots, device interaction, logcat, native crash/ANR traces, pixel diffing |
| [mcp_flutter](https://github.com/Arenukvern/mcp_flutter) — ~30 tools: semantic snapshot, tap, type, screenshots, hot reload | A closed visual/semantic loop inside Dart | Requires adding a package to your app; no widget→source mapping; no logcat or native errors; no pixel diffing |
| Android MCPs ([mobile-mcp](https://github.com/mobile-next/mobile-mcp), android-mcp, adb-mcp) — screenshots, uiautomator trees, tap/swipe/type, logcat | Full device control | No Flutter awareness at all; a Flutter app is one `SurfaceView` |

What is specific to this server:

1. **The join.** One call returns tap coordinates *and* the Dart `file:line`
   that built the thing you are pointing at.
2. **Dart errors correlated with native ones.** A Dart exception and the
   `AndroidRuntime` trace that followed it, side by side, matched on pid.
3. **Pixel-level verification** of whether a hot reload changed what you
   intended, and nothing else.
4. **Nothing added to your app.** No package, no codegen, no import. It reads
   the Dart VM Service that every debug build already exposes, and drives adb.

That last point is the one worth keeping if anyone reimplements this.

## Architecture

A single-file `stdio` MCP server (`flutter_bridge_mcp/server.py`) talking to two
sources:

- **adb** — `uiautomator dump`, `screencap`, `input`, `logcat`, `pm`, `monkey`.
  Always invoked as an argv list; `shell=True` appears nowhere in the file.
- **The Dart VM Service** — JSON-RPC over a websocket, reached through an
  `adb forward` to loopback.

### Finding the VM Service without touching the app

The VM Service URI is printed to logcat exactly once, at launch:

```
VM_URI_RE = re.compile(r"Dart VM service is listening on (http://127\.0\.0\.1:(\d+)/(\S*))")
```

Three details make this reliable in practice:

- **Newest first.** Old launches leave stale URIs in the buffer, so candidates
  are tried in reverse order.
- **Probe before accepting.** A candidate is only used if a `getVM` call
  actually answers on it. A dead port from a previous run fails fast and the
  next candidate is tried.
- **The auth token is a path segment**, captured including its trailing slash,
  so `ws_uri` is `ws://127.0.0.1:<local>/<token>/ws`. There is no header or
  query parameter.

`flutter_connect(relaunch=True)` force-stops the app, clears the log buffer and
relaunches via `monkey`, which guarantees the next URI found is the fresh one.
Local ports are chosen by binding port 0 and letting the OS pick.

Each RPC opens its own short-lived websocket with `max_size=None` — widget trees
exceed the default 1 MiB frame cap. Most VM Service extensions need an
`isolateId`, which is injected automatically after a `getVM` handshake.

### The two trees

**Android side** (`_nodes`): `uiautomator dump` to a file, read back with
`exec-out` so adb does not mangle line endings, parsed as XML. A node is kept
only if it has text, a content description, or a resource id *and* is clickable
— anonymous layout containers are dropped. Tap points are the integer centroid
of the parsed `bounds`.

**Dart side** (`_widget_nodes`): one RPC,
`ext.flutter.inspector.getRootWidgetTree`, with `isSummaryTree` on (the full
render tree would be thousands of nodes). Two inspector fields carry the weight:

- `createdByLocalProject` — filters the tree down to widgets from your package,
  discarding the framework. This is the main context-control move on the Dart
  side.
- `creationLocation` — the `file` and `line`, available because debug builds
  compile with `--track-widget-creation`. Absolute paths are trimmed at `/lib/`
  to produce `lib/home.dart:12`.

**The join** (`flutter_locate`) queries both trees for the same string and
reports each side separately. It is deliberately honest about the asymmetry: a
widget with no semantics is invisible to Android, and the tool says so rather
than returning nothing, because "not found" and "found but untappable" need
different fixes.

### Hot reload

Hot reload needs a Dart compiler, which the VM Service alone does not provide.
The Flutter tool has one, so the server owns a `flutter attach` process and
drives it the way a developer would — by writing `r` to its stdin and watching
stdout for a completion marker. Both success and failure markers are watched, so
a broken reload returns immediately instead of waiting out the timeout.
Shutdown writes `d` (detach), leaving the app running on the device.

After attaching, the server adopts the URI that `flutter attach` printed rather
than forwarding its own — the Flutter tool has already set up a host-side
forward, and re-forwarding that port number would point at something unrelated.

### Visual verification

`ui_checkpoint` stores a full-resolution PNG in memory. `ui_diff` compares it to
the current screen:

```python
diff    = ImageChops.difference(before, after).convert("L")
mask    = diff.point(lambda v: 255 if v > threshold else 0)
changed = sum(mask.point(lambda v: 1 if v else 0).getdata())
```

Note that `.convert("L")` applies ITU-R 601 luma weighting
(`0.299R + 0.587G + 0.114B`), so `threshold` gates a weighted blend of the
channel deltas, not any single channel — a blue-only change is attenuated
roughly 9× relative to green.

Regions are found with a deliberately coarse two-stage gate, which is what keeps
anti-aliasing and subpixel text shifts from producing noise:

1. Per-pixel threshold (default 12).
2. The mask is box-downsampled to a 40px grid; a cell counts as changed only if
   roughly 3% of its pixels did.
3. 4-connected flood fill over hot cells, each group becoming a grid-aligned
   box, sorted largest first.

Boxes are drawn on the *after* image, inflated slightly so the outline does not
cover the change. Above 60% changed, the response says outright that this looks
like a navigation or full rebuild rather than a local edit.

## Designing for a model rather than a human

The second design goal, stated at the top of the server: never hand the model
raw logcat. A human skims 8000 lines; a model pays for every one of them and
loses the thread.

- **Two separate budgets.** `SCAN_LINES` (8000) is how much logcat is *read*
  before filtering; `MAX_LINES` (200) is how much is *returned*. Filtering
  happens over a wide window, emission over a narrow one.
- **Consecutive duplicates collapse** into `[xN repeated]`, which flattens
  chatty repeat-spam to one line.
- **Truncation is always signposted** — `(+N chars)` on a clipped message,
  `[N total, showing M]` on every list. The model is never silently handed a
  partial view it believes is complete.
- **Limits are clamped, not trusted**: `limit = max(1, min(limit, MAX_LINES))`.
- **Failure paths say what to do next** — "call `flutter_connect` again", "did
  you call `capture_start` first?" — which prevents blind retry loops.

### Why there is no `adb shell` tool

An arbitrary shell tool would be the easiest thing to add and the worst thing to
ship. It would hand the model unbounded code execution on a real device, return
unbounded unfiltered output straight into context — defeating every measure
above — and let it bypass the curated tools entirely.

Instead each capability is narrow and validated: `press_key` maps friendly names
through a fixed keycode table and otherwise accepts only `KEYCODE_*` or a
number; `swipe` validates direction against a fixed set; `input_text` drops
non-ASCII (and *reports* the drop) because `input text` throws on it, then
escapes shell metacharacters because the on-device `input` runs through a shell
on the other side.

## Known limitations

- **Debug or profile builds only.** Release builds expose no VM Service and no
  widget creation locations.
- **Widgets without semantics are unreachable from the Android side.** Flutter
  only publishes what it is told to; `tap(text=...)` cannot find the rest.
- **Checkpoints are in-memory**, unbounded and not persisted. They die with the
  server.
- **The first isolate wins.** There is no main-isolate selection logic.
- **Inspector object groups are never disposed**, so repeated widget-tree calls
  accumulate references in the running app.
- **Screenshots are downscaled** before being returned, so coordinates read off
  one are not device coordinates. This is why `tap(text=...)` exists.

## Status

v0.1.1 — on [PyPI](https://pypi.org/project/flutter-bridge-mcp/). 26 tools,
27 unit tests covering log parsing, error grouping and widget-tree flattening
with no device required, plus `selftest.py` as a device-backed smoke test. CI
runs the suite on Python 3.10 through 3.13.
