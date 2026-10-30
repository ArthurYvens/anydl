import json


def test_key_prefers_the_site_id_over_the_url(anydl_module):
    index = anydl_module.LibraryIndex

    # The same video has a dozen link shapes; the id is the one stable thing.
    assert index.key_for("https://youtu.be/abc123", "abc123") == "id:abc123"
    assert index.key_for("https://www.youtube.com/watch?v=abc123", "abc123") == "id:abc123"


def test_url_key_ignores_scheme_host_prefix_and_fragment(anydl_module):
    index = anydl_module.LibraryIndex

    same = {
        index.key_for("https://example.com/Watch/1"),
        index.key_for("http://example.com/Watch/1"),
        index.key_for("www.example.com/watch/1/"),
        index.key_for("https://example.com/watch/1#t=30"),
    }

    assert len(same) == 1


def test_recording_then_finding(anydl_module, tmp_path):
    video = tmp_path / "Some video.mp4"
    video.write_bytes(b"x")
    index = anydl_module.LibraryIndex(str(tmp_path))

    index.record("https://youtu.be/abc123",
                 {"id": "abc123", "title": "Some video", "height": 720, "ext": "mp4"},
                 str(video), {"mode": "video", "quality": "720p"})

    # A fresh index has to see it, or nothing survives closing the app.
    again = anydl_module.LibraryIndex(str(tmp_path))
    found = again.find("https://youtu.be/abc123")
    assert found["height"] == 720
    assert found["title"] == "Some video"


def test_a_deleted_file_stops_counting_as_owned(anydl_module, tmp_path):
    video = tmp_path / "gone.mp4"
    video.write_bytes(b"x")
    index = anydl_module.LibraryIndex(str(tmp_path))
    index.record("https://youtu.be/gone", {"id": "gone", "height": 720},
                 str(video), {"mode": "video"})

    video.unlink()

    # Warning about a file the person already deleted would be worse than
    # saying nothing at all.
    assert index.find("https://youtu.be/gone") is None


def test_a_corrupt_index_is_ignored_not_fatal(anydl_module, tmp_path):
    (tmp_path / anydl_module.LibraryIndex.FILENAME).write_text("{not json", encoding="utf8")

    index = anydl_module.LibraryIndex(str(tmp_path))

    assert index.entries == {}
    assert index.find("https://youtu.be/anything") is None


def test_index_is_valid_json_on_disk(anydl_module, tmp_path):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    index = anydl_module.LibraryIndex(str(tmp_path))
    index.record("https://youtu.be/v", {"id": "v"}, str(video), {"mode": "video"})

    data = json.loads((tmp_path / anydl_module.LibraryIndex.FILENAME).read_text("utf8"))

    assert data["version"] == 1
    assert data["entries"]


def test_wanted_height(anydl_module):
    assert anydl_module.wanted_height("Best") is None
    assert anydl_module.wanted_height("") is None
    assert anydl_module.wanted_height("1080p") == 1080
    assert anydl_module.wanted_height("2160p") == 2160


def test_a_better_copy_is_offered_as_an_upgrade(anydl_module):
    entry = {"title": "Talk", "height": 720, "mode": "video", "path": "/x/Talk.mp4"}

    headline, question = anydl_module.describe_existing(
        entry, {"mode": "video", "quality": "1080p"})

    assert "720p" in headline
    assert "1080p" in question


def test_the_same_quality_is_offered_as_a_repeat(anydl_module):
    entry = {"title": "Talk", "height": 1080, "mode": "video", "path": "/x/Talk.mp4"}

    _headline, question = anydl_module.describe_existing(
        entry, {"mode": "video", "quality": "1080p"})

    assert "again" in question.lower()


def test_asking_for_audio_when_the_video_is_owned_is_not_a_duplicate(anydl_module):
    entry = {"title": "Talk", "height": 1080, "mode": "video", "path": "/x/Talk.mp4"}

    _headline, question = anydl_module.describe_existing(
        entry, {"mode": "audio", "quality": "Best"})

    assert "audio" in question.lower()
