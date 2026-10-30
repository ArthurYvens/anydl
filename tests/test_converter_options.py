def base_settings(tmp_path, **overrides):
    settings = {
        "out_dir": str(tmp_path),
        "mode": "video",
        "quality": "Best",
        "playlist": False,
        "section_start": None,
        "section_end": None,
        "subtitles": "none",
        "embed_subs": False,
        "sponsorblock": False,
    }
    settings.update(overrides)
    return settings


def test_video_best_without_ffmpeg(converter, tmp_path):
    opts = converter._build_options(base_settings(tmp_path))

    assert opts["noplaylist"] is True
    assert opts["format"] == "best[ext=mp4]/best"
    assert "merge_output_format" not in opts


def test_video_specific_quality_without_ffmpeg(converter, tmp_path):
    opts = converter._build_options(
        base_settings(tmp_path, quality="1080p")
    )

    assert opts["format"] == "best[height<=1080][ext=mp4]/best[height<=1080]/best"


def test_playlist_output_and_behavior(converter, tmp_path):
    opts = converter._build_options(
        base_settings(tmp_path, playlist=True)
    )

    assert opts["noplaylist"] is False
    assert opts["ignoreerrors"] is True
    assert "%(playlist_title|)s" in opts["outtmpl"]


def test_audio_without_ffmpeg(converter, tmp_path):
    logs = []
    converter.on_log = logs.append

    opts = converter._build_options(
        base_settings(
            tmp_path,
            mode="audio",
            audio_format="mp3",
            bitrate="320",
        )
    )

    assert opts["format"] == "bestaudio[ext=m4a]/bestaudio"
    assert "postprocessors" not in opts
    assert any("ffmpeg missing" in line.lower() for line in logs)


def test_section_suffix_is_in_output_template(converter, tmp_path):
    opts = converter._build_options(
        base_settings(tmp_path, section_start=90, section_end=120)
    )

    assert "[1m30s-2m00s]" in opts["outtmpl"]


def test_an_ordinary_download_does_not_overwrite(converter, tmp_path):
    opts = converter._build_options(base_settings(tmp_path))

    # None is yt-dlp's own default, which leaves an existing file alone.
    assert opts["overwrites"] is None


def test_an_accepted_replacement_does_overwrite(converter, tmp_path):
    """Agreeing to fetch a better copy has to actually produce one.

    yt-dlp skips any download whose target name is already taken. Without
    this flag the 1080p upgrade reported success, left the 720p file exactly
    where it was, and recorded 1080p in the library index -- so the upgrade
    was then never offered again.
    """
    opts = converter._build_options(base_settings(tmp_path, replace=True))

    assert opts["overwrites"] is True


def test_cookie_file_is_applied(converter, tmp_path):
    # Cookies reach the converter through its constructor, the way a queue row
    # hands them over, not through the per-download settings dict. This test
    # used to pass one in the settings and assert on an option nothing had
    # written, so it failed while the feature itself worked.
    converter.cookie_file = "cookies.txt"

    opts = converter._build_options(base_settings(tmp_path))

    assert opts["cookiefile"] == "cookies.txt"


def test_browser_cookies_are_applied(converter, tmp_path):
    converter.browser = "firefox"

    opts = converter._build_options(base_settings(tmp_path))

    assert opts["cookiesfrombrowser"] == ("firefox", None, None, None)


def test_subtitles_are_disabled_by_default(converter, tmp_path):
    opts = converter._build_options(base_settings(tmp_path))

    assert "writesubtitles" not in opts
    assert "subtitleslangs" not in opts


def test_download_reports_failure(converter, tmp_path, monkeypatch):
    # The CLI exits on this value: a failed link must not read as success.
    done = []
    converter.on_done = lambda ok, msg: done.append((ok, msg))
    monkeypatch.setattr(converter, "run", lambda url, settings: ("fail", "boom"))

    assert converter.download(["https://example.invalid/x"], str(tmp_path)) is False
    assert done[0][0] is False
    assert "Saved to" not in done[0][1]


def test_download_reports_success(converter, tmp_path, monkeypatch):
    monkeypatch.setattr(converter, "run", lambda url, settings: ("ok", "Saved x"))

    assert converter.download(["https://example.invalid/x"], str(tmp_path)) is True
