def test_progress_hook_uses_estimated_total(converter):
    events = []
    converter.on_progress = lambda pct, stats: events.append((pct, stats))

    converter._last_emit = -1000
    converter._progress_hook(
        {
            "status": "downloading",
            "downloaded_bytes": 50,
            "total_bytes_estimate": 100,
            "speed": 10,
            "eta": 5,
            "info_dict": {"title": "Example"},
        }
    )

    assert events
    pct, stats = events[-1]
    assert pct == 50
    assert stats["done"] == 50
    assert stats["total"] == 100


def test_progress_hook_without_total_does_not_divide_by_zero(converter):
    events = []
    converter.on_progress = lambda pct, stats: events.append((pct, stats))

    converter._last_emit = -1000
    converter._progress_hook(
        {
            "status": "downloading",
            "downloaded_bytes": 50,
            "speed": 0,
            "eta": 0,
            "info_dict": {"title": "Example"},
        }
    )

    assert events[-1][0] == 0


def test_finished_hook_sets_final_file(converter):
    events = []
    converter.on_progress = lambda pct, stats: events.append((pct, stats))

    converter._progress_hook(
        {"status": "finished", "filename": "/tmp/example.mp4"}
    )

    assert converter.final_file == "/tmp/example.mp4"
    assert events[-1] == (100, None)


def test_describe_file_includes_playlist_position(converter):
    result = converter._describe_file(
        {
            "filename": "/tmp/file.mp4",
            "info_dict": {
                "title": "Video",
                "playlist_index": 2,
                "n_entries": 10,
            },
        }
    )

    assert result == "Video  (2 of 10)"


def test_logger_adapter_deduplicates_errors(anydl_module):
    messages = []
    logger = anydl_module._LoggerAdapter(messages.append)

    logger.error("ERROR: boom")
    logger.error("ERROR: ERROR: boom")

    assert messages == ["[error] boom"]
