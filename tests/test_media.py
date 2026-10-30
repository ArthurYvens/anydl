"""The pieces playback is built from, without ffmpeg or a sound card."""

import io
import queue
import struct
import wave


def ppm(width, height, shade):
    return b"P6\n%d %d\n255\n" % (width, height) + bytes([shade]) * (width * height * 3)


def test_read_ppm_splits_a_pipe_into_frames(anydl_module):
    from anydl_core.media import read_ppm

    pipe = io.BytesIO(ppm(4, 2, 10) + ppm(4, 2, 20))

    first, second = read_ppm(pipe), read_ppm(pipe)

    assert first == ppm(4, 2, 10)
    assert second == ppm(4, 2, 20)
    assert read_ppm(pipe) is None


def test_read_ppm_drops_a_frame_cut_short(anydl_module):
    """ffmpeg killed mid-frame leaves half a body on the pipe; showing it
    would hand Tk a truncated image."""
    from anydl_core.media import read_ppm

    assert read_ppm(io.BytesIO(ppm(4, 2, 10)[:-5])) is None


def test_take_skips_frames_that_are_already_late(anydl_module):
    """The clock decides which frame is due. Frames behind it are dropped, so
    a slow tick costs a skipped frame instead of a picture that lags the
    sound for the rest of the clip."""
    from anydl_core.media import FrameStream

    stream = FrameStream.__new__(FrameStream)
    stream.frames = queue.Queue()
    stream.taken = 0
    for index in range(5):
        stream.frames.put(index)

    assert stream.take(2) == 2
    assert stream.taken == 3
    assert stream.take(2) is None  # nothing new is due yet
    assert stream.take(10) == 4    # all that has arrived


def test_audio_clip_starts_on_the_exact_sample(anydl_module, tmp_path):
    """ffplay with -ss started at the keyframe before the point -- 5.28s for
    a request of 10s on the test video. The clip is cut from the samples, so
    its first sample is the one at the requested second."""
    from anydl_core.media import ClipSource

    rate = 1000
    track = tmp_path / "sound.wav"
    with wave.open(str(track), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(struct.pack("<%dh" % (5 * rate), *range(5 * rate)))

    source = ClipSource.__new__(ClipSource)
    source._dir = str(tmp_path)
    source.clips = 0
    source.last_clip = None
    source.sound = str(track)

    with wave.open(source.audio_clip(1.5, 2.5), "rb") as clip:
        samples = struct.unpack("<%dh" % clip.getnframes(), clip.readframes(clip.getnframes()))

    assert len(samples) == rate
    assert samples[0] == 1500
    assert samples[-1] == 2499
