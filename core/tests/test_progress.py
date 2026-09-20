import logging

import bgmeter
from bgmeter import ProgressEvent, ProgressLevel, ReadOptions, emit_progress


def test_progress_levels_have_stable_values():
    assert [level.value for level in ProgressLevel] == ["info", "detail", "warning"]


def test_progress_event_defaults_and_is_frozen():
    event = ProgressEvent(ProgressLevel.INFO, "Hello.")

    assert event.current is None and event.total is None
    try:
        event.message = "changed"
    except AttributeError:
        pass
    else:
        raise AssertionError("ProgressEvent must be frozen")


def test_emit_progress_ignores_a_missing_callback():
    emit_progress(None, ProgressEvent(ProgressLevel.INFO, "Hello."))


def test_emit_progress_delivers_the_event():
    seen = []
    event = ProgressEvent(ProgressLevel.DETAIL, "Reading.", current=1, total=2)

    emit_progress(seen.append, event)

    assert seen == [event]


def test_emit_progress_swallows_callback_errors_and_logs_them(caplog):
    def broken(event):
        raise RuntimeError("reporter exploded")

    with caplog.at_level(logging.WARNING, logger="bgmeter.progress"):
        emit_progress(broken, ProgressEvent(ProgressLevel.INFO, "Hello."))

    [record] = caplog.records
    assert record.name == "bgmeter.progress"
    assert record.levelno == logging.WARNING
    assert "RuntimeError" in record.getMessage()


def test_read_options_progress_is_optional_and_not_part_of_identity():
    assert ReadOptions().progress is None
    assert ReadOptions(progress=print) == ReadOptions()
    assert "progress" not in repr(ReadOptions(progress=print))


def test_progress_api_is_exported():
    for name in ("ProgressCallback", "ProgressEvent", "ProgressLevel", "emit_progress"):
        assert name in bgmeter.__all__
        assert hasattr(bgmeter, name)
