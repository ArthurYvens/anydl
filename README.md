# YTConverter

Download YouTube videos as MP4, or extract just the audio (MP3, M4A, WAV, OPUS,
FLAC). Small desktop app with a graphical interface and a command line mode,
built on [yt-dlp](https://github.com/yt-dlp/yt-dlp).

- Video up to 4K, or capped at a resolution you pick
- Audio extraction with a bitrate of your choice, cover art and tags embedded
- Multiple links at once, or a whole playlist
- Progress, speed and ETA, with a working cancel button
- No Python knowledge required: the setup script installs everything

## Install

### Windows

Double-click **`install.bat`**. It handles everything, in order:

1. **Python** — looks for an existing install; otherwise tries winget, and if
   that is unavailable it downloads the official installer from python.org and
   runs it silently.
2. **yt-dlp** — installs or updates it with pip.
3. **ffmpeg** — tries winget, and falls back to downloading a build into `bin/`.

Administrator rights are not needed; Python is installed for the current user
only. Then start the app with **`run.bat`**.

### Linux / macOS

```bash
chmod +x install.sh run.sh
./install.sh
./run.sh
```

`install.sh` creates a local `.venv` and installs yt-dlp there, leaving the
system Python untouched. It does not install ffmpeg for you — it prints the one
command for your platform, since package managers need your password.

The graphical interface needs tkinter, which some distributions ship
separately (`python3-tk`, `python3-tkinter` or `tk`). Without it the command
line mode still works.

### Manual

```bash
pip install -r requirements.txt
python ytconverter.py
```

## Usage

The app window takes one or more links, a mode (video or audio only), and a
destination folder. Files go to `~/Downloads/YTConverter` by default.

Command line:

```bash
python ytconverter.py "https://youtu.be/VIDEO_ID"                  # best quality video
python ytconverter.py "https://youtu.be/VIDEO_ID" -q 1080p         # cap at 1080p
python ytconverter.py "https://youtu.be/VIDEO_ID" -a               # MP3 at 192 kbps
python ytconverter.py "https://youtu.be/VIDEO_ID" -a -f wav        # WAV
python ytconverter.py "https://youtu.be/VIDEO_ID" -a -b 320        # MP3 at 320 kbps
python ytconverter.py "PLAYLIST_URL" --playlist -a                 # whole playlist as MP3
python ytconverter.py "https://youtu.be/VIDEO_ID" -o /path/to/dir  # custom folder
```

Run it with no arguments to open the interface.

| Flag | Meaning | Default |
|---|---|---|
| `-a`, `--audio` | extract audio only | off |
| `-f`, `--format` | `mp3`, `m4a`, `wav`, `opus`, `flac` | `mp3` |
| `-b`, `--bitrate` | audio bitrate in kbps | `192` |
| `-q`, `--quality` | `Best`, `2160p`, `1440p`, `1080p`, `720p`, `480p`, `360p` | `Best` |
| `-o`, `--output` | destination folder | `~/Downloads/YTConverter` |
| `--playlist` | download the entire playlist | off |

## About ffmpeg

ffmpeg is optional but changes what the app can do:

| | with ffmpeg | without ffmpeg |
|---|---|---|
| Video | up to 4K (merges the video and audio tracks) | single pre-muxed stream only, ~720p |
| Audio | MP3/WAV/FLAC with cover art and tags | saves the original M4A, no transcoding |

The app looks for ffmpeg on `PATH` first, then in a `bin/` folder next to the
script — so you can drop `ffmpeg` and `ffprobe` there instead of installing
system-wide.

## Video codec

When you pick a specific resolution, the app prefers **H.264**, which plays in
any player or editor. On `Best` it does not filter by codec, because YouTube
only serves H.264 up to 1080p — filtering there would silently throw away 4K
and leave you with a 1080p file.

## Troubleshooting

**"ffmpeg not found" right after installing it.** `PATH` only updates for
processes started afterwards. Close the app and the terminal, then reopen.

**Extraction errors after YouTube changes something.** Update yt-dlp:

```bash
python -m pip install -U yt-dlp
```

**The window does not open on Linux.** tkinter is missing — see the install
section above.

## Requirements

- Python 3.9+
- yt-dlp
- ffmpeg (optional, recommended)

## License

MIT — see [LICENSE](LICENSE).

Downloading content you do not own may violate YouTube's Terms of Service and
local copyright law. Use this on material you have the rights to.
