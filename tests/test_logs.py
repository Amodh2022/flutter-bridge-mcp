"""Logcat parsing, filtering and rendering. No device required."""
from flutter_bridge_mcp import server as s


def parse(text: str) -> list[dict]:
    return s._parse(text.strip().splitlines())


LINES = """
09-17 12:00:00.100  1234 1234 I MyTag   : first message
09-17 12:00:00.200  1234 1234 W Other   : a warning
09-17 12:00:00.300  9999 9999 E Third   : from another process
""".strip()


def test_parses_threadtime_fields():
    e = parse(LINES)[0]
    assert e["ts"] == "09-17 12:00:00.100"
    assert (e["pid"], e["tid"]) == ("1234", "1234")
    assert e["level"] == "I"
    assert e["tag"] == "MyTag"
    assert e["msg"] == "first message"


def test_continuation_lines_attach_to_previous_entry():
    """Stack frames arrive as bare lines with no header of their own."""
    entries = parse(
        "09-17 12:00:00.100  1 1 E T: java.lang.RuntimeException: boom\n"
        "\tat com.example.Foo.bar(Foo.java:42)\n"
        "\tat com.example.Baz.qux(Baz.java:7)"
    )
    assert len(entries) == 1
    assert entries[0]["msg"].count("\n") == 2
    assert "Foo.java:42" in entries[0]["msg"]


def test_leading_junk_before_any_header_is_dropped():
    assert parse("not a log line at all") == []


def test_filter_level_floor_is_inclusive():
    entries = parse(LINES)
    assert len(s._filter(entries, None, None, "I", None)) == 3
    assert len(s._filter(entries, None, None, "W", None)) == 2
    assert len(s._filter(entries, None, None, "E", None)) == 1


def test_filter_by_pid_and_tag_and_substring():
    entries = parse(LINES)
    assert len(s._filter(entries, {"1234"}, None, "V", None)) == 2
    assert len(s._filter(entries, None, "^My", "V", None)) == 1
    # `contains` matches the tag as well as the message
    assert len(s._filter(entries, None, None, "V", "WARNING")) == 1
    assert len(s._filter(entries, None, None, "V", "Third")) == 1


def test_render_collapses_consecutive_duplicates():
    dup = "\n".join(
        f"09-17 12:00:0{i}.000  1 1 I T: same message" for i in range(4)
    )
    out = s._render(parse(dup), 10)
    assert out.count("same message") == 1
    assert "[x4 repeated]" in out


def test_render_does_not_collapse_across_a_different_line():
    text = (
        "09-17 12:00:00.000  1 1 I T: a\n"
        "09-17 12:00:01.000  1 1 I T: b\n"
        "09-17 12:00:02.000  1 1 I T: a"
    )
    assert s._render(parse(text), 10).count("I/T (1): a") == 2


def test_render_truncates_long_messages_and_says_how_much():
    long = "x" * (s.MAX_MSG + 50)
    out = s._render(parse(f"09-17 12:00:00.000  1 1 I T: {long}"), 10)
    assert "(+50 chars)" in out
    assert len(out) < len(long)


def test_render_tails_to_the_limit():
    text = "\n".join(f"09-17 12:00:00.00{i}  1 1 I T: line{i}" for i in range(5))
    out = s._render(parse(text), 2)
    assert "line4" in out and "line0" not in out


def test_render_handles_no_matches():
    assert s._render([], 10) == "(no matching log lines)"
