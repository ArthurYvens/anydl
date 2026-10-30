import pytest


def test_parse_timestamp(anydl_module):
    assert anydl_module.parse_timestamp("90") == 90
    assert anydl_module.parse_timestamp("1:30") == 90
    assert anydl_module.parse_timestamp("1:02:03") == 3723
    assert anydl_module.parse_timestamp("") is None


@pytest.mark.parametrize("value", ["1:2:3:4", "abc", "1:x"])
def test_parse_timestamp_rejects_invalid(value, anydl_module):
    with pytest.raises(ValueError):
        anydl_module.parse_timestamp(value)


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0s"),
        (1, "1s"),
        (90, "1m30s"),
        (3661, "1h01m01s"),
    ],
)
def test_stamp_label(seconds, expected, anydl_module):
    assert anydl_module.stamp_label(seconds) == expected


@pytest.mark.parametrize(
    ("seconds", "decimals", "expected"),
    [
        (0, 0, "0:00"),
        (90, 0, "1:30"),
        (3723, 0, "1:02:03"),
        # A fractional mark off a timeline. The first version padded to the
        # wrong width and produced "0:003.8".
        (3.79, 1, "0:03.8"),
        (90.25, 1, "1:30.2"),
        (3723.04, 1, "1:02:03.0"),
        # Rounding after the split carried into the seconds field and printed
        # "0:60" instead of rolling the minute over.
        (59.7, 0, "1:00"),
        (3599.96, 1, "1:00:00.0"),
    ],
)
def test_clock_label(seconds, decimals, expected, anydl_module):
    assert anydl_module.clock_label(seconds, decimals) == expected


def test_clock_label_survives_a_round_trip(anydl_module):
    """What the timeline writes has to read back as what it meant.

    At whole seconds two marks inside the same second collapse onto one
    string, which clip_range() then rejects as an end before its start.
    """
    start, end = 3.79, 4.29
    text_start = anydl_module.clock_label(start, 1)
    text_end = anydl_module.clock_label(end, 1)

    assert text_start != text_end
    assert anydl_module.parse_timestamp(text_start) < anydl_module.parse_timestamp(text_end)
    assert abs(anydl_module.parse_timestamp(text_start) - start) <= 0.05
    assert abs(anydl_module.parse_timestamp(text_end) - end) <= 0.05


def test_section_suffix(anydl_module):
    assert anydl_module.section_suffix(None, None) == ""
    assert anydl_module.section_suffix(90, None) == " [from 1m30s]"
    assert anydl_module.section_suffix(None, 90) == " [to 1m30s]"
    assert anydl_module.section_suffix(90, 120) == " [1m30s-2m00s]"


def test_parse_version_does_not_compare_strings(anydl_module):
    assert anydl_module.parse_version("2026.08.19") == (2026, 8, 19)
    assert anydl_module.parse_version("2026.8.19") == (2026, 8, 19)
    assert anydl_module.parse_version("garbage") == ()


def test_format_size(anydl_module):
    assert anydl_module.format_size(None) == "?"
    assert anydl_module.format_size(1024 * 1024) == "1.0 MB"
    assert anydl_module.format_size(1024**3) == "1.00 GB"


def test_audio_size_estimate(anydl_module):
    settings = {"audio_format": "mp3", "bitrate": "192"}
    assert anydl_module.audio_size_estimate(settings, 10) == 240000
    assert anydl_module.audio_size_estimate(settings, None) is None
