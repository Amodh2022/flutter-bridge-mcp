"""Flutter bridge logic: error grouping, widget tree, VM URI discovery."""
from flutter_bridge_mcp import server as s


def parse(text: str) -> list[dict]:
    return s._parse(text.strip().splitlines())


FENCED = """
09-17 12:00:00.100  1 1 E flutter : ╔═══ EXCEPTION CAUGHT BY WIDGETS LIBRARY ═══
09-17 12:00:00.101  1 1 E flutter : type 'Null' is not a subtype of type 'String'
09-17 12:00:00.102  1 1 E flutter : #0  PatientCard.build (package:app/x.dart:42:9)
09-17 12:00:00.103  1 1 E flutter : ╚═══════════════════════════════════════════
""".strip()


def test_fenced_block_becomes_one_error():
    blocks = s._dart_error_blocks(parse(FENCED))
    assert len(blocks) == 1
    assert blocks[0]["ts"] == "09-17 12:00:00.100"
    assert "x.dart:42:9" in blocks[0]["msg"]


def test_two_fenced_blocks_stay_separate():
    assert len(s._dart_error_blocks(parse(FENCED + "\n" + FENCED))) == 2


def test_unterminated_block_is_still_returned():
    """A buffer can be cut mid-error; a truncated error beats a dropped one."""
    truncated = "\n".join(FENCED.splitlines()[:3])
    blocks = s._dart_error_blocks(parse(truncated))
    assert len(blocks) == 1
    assert "╚" not in blocks[0]["msg"]


def test_new_fence_flushes_the_previous_unterminated_block():
    text = "\n".join(FENCED.splitlines()[:2] + FENCED.splitlines())
    assert len(s._dart_error_blocks(text and parse(text))) == 2


def test_unfenced_exception_line_counts_on_its_own():
    text = "09-17 12:00:00.100  1 1 E flutter : Unhandled Exception: bad state"
    assert len(s._dart_error_blocks(parse(text))) == 1


def test_non_flutter_lines_are_ignored_entirely():
    text = "09-17 12:00:00.100  1 1 E AndroidRuntime: Exception: not dart"
    assert s._dart_error_blocks(parse(text)) == []


NATIVE = """
09-17 12:00:00.200  1234 1 E AndroidRuntime: java.lang.IllegalStateException
09-17 12:00:00.300  5555 1 E SomethingElse: unrelated process
09-17 12:00:00.400  1234 1 I Chatty: not an error
""".strip()


def test_native_errors_are_matched_by_pid():
    got = s._native_errors(parse(NATIVE), {"1234"}, None)
    assert [e["tag"] for e in got] == ["AndroidRuntime"]


def test_native_errors_unfiltered_when_pid_unknown():
    assert len(s._native_errors(parse(NATIVE), set(), None)) == 2


def test_native_errors_fall_back_to_package_in_message():
    text = "09-17 12:00:00.200  777 1 E T: failure in com.example.app"
    assert len(s._native_errors(parse(text), {"1234"}, "com.example.app")) == 1


# --------------------------------------------------------------- widget tree

TREE = {
    "widgetRuntimeType": "RootWidget",
    "valueId": "inspector-0",
    "children": [
        {
            "widgetRuntimeType": "Scaffold",
            "valueId": "inspector-1",
            "createdByLocalProject": True,
            "creationLocation": {"file": "file:///proj/lib/home.dart", "line": 12},
            "children": [
                {
                    "widgetRuntimeType": "Text",
                    "description": "Text",
                    "textPreview": "Total Patients",
                    "valueId": "inspector-2",
                    "createdByLocalProject": True,
                    "creationLocation": {"file": "file:///proj/lib/card.dart", "line": 80},
                }
            ],
        }
    ],
}


def test_flatten_keeps_depth_and_strips_file_scheme():
    nodes = s._flatten_widget_tree(TREE, local_only=False)
    assert [n["type"] for n in nodes] == ["RootWidget", "Scaffold", "Text"]
    assert [n["depth"] for n in nodes] == [0, 1, 2]
    assert nodes[1]["file"] == "/proj/lib/home.dart"


def test_flatten_local_only_drops_framework_widgets():
    types = [n["type"] for n in s._flatten_widget_tree(TREE, local_only=True)]
    assert types == ["Scaffold", "Text"]


def test_text_preview_is_searchable_in_desc():
    """flutter_locate matches on desc, so the rendered string must reach it."""
    text_node = s._flatten_widget_tree(TREE)[-1]
    assert "Total Patients" in text_node["desc"]
    assert text_node["text"] == "Total Patients"


def test_missing_creation_location_is_not_fatal():
    nodes = s._flatten_widget_tree({"widgetRuntimeType": "X"}, local_only=False)
    assert nodes[0]["file"] == "" and nodes[0]["line"] is None


def test_short_path_trims_to_the_project_relative_part():
    assert s._short_path("/home/me/proj/lib/a/b.dart") == "lib/a/b.dart"
    assert s._short_path("/elsewhere/b.dart") == "b.dart"
    assert s._short_path("") == ""


# ------------------------------------------------------------ URI discovery

LOG_WITH_TWO = """
09-17 11:00:00.000  1 1 I flutter : The Dart VM service is listening on http://127.0.0.1:1111/aaa=/
09-17 12:00:00.000  1 1 I flutter : The Dart VM service is listening on http://127.0.0.1:2222/bbb=/
""".strip()


def test_finds_every_vm_service_uri_in_order():
    found = s.VM_URI_RE.findall(LOG_WITH_TWO)
    assert [f[1] for f in found] == ["1111", "2222"]


def test_newest_uri_is_tried_first():
    """Old launches leave stale lines behind, so discovery must prefer the newest."""
    found = s.VM_URI_RE.findall(LOG_WITH_TWO)
    assert list(reversed(found))[0][1] == "2222"


def test_auth_token_is_captured_with_its_trailing_slash():
    token = s.VM_URI_RE.findall(LOG_WITH_TWO)[0][2]
    assert token == "aaa=/"  # so ws_uri becomes .../aaa=/ws
