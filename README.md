# anydl

A desktop front end for [yt-dlp](https://github.com/yt-dlp/yt-dlp).

**yt-dlp does the real work.** The ~1700 site extractors, the download engine,
the format selection language, the ffmpeg orchestration, the cookie handling —
all of it is theirs. anydl is a window over it, an installer, and a set of
defaults you would otherwise have to type by hand. It is about 800 lines
against yt-dlp's 240,000.

So: if you live in a terminal and already have Python and ffmpeg, use yt-dlp
directly. It is the better tool for you. anydl is for the case where you do
not, or where you would rather click than remember the incantations below.

Download video as MP4, or extract just the audio (MP3, M4A, WAV, OPUS, FLAC),
from YouTube, Instagram, X/Twitter, TikTok, Facebook, Reddit, Twitch, Vimeo,
SoundCloud, Dailymotion and the rest of the yt-dlp list.

- Video up to 4K, or capped at a resolution you pick
- Audio extraction with a bitrate of your choice, cover art and tags embedded
- Multiple links at once, or a whole playlist
- Progress, speed and ETA, with a working cancel button
- No Python knowledge required: the setup script installs everything

## What you get over plain yt-dlp

**Getting it running at all.** With yt-dlp you first install Python, then pip
install yt-dlp, then install ffmpeg and get it onto `PATH` yourself. Here you
double-click `install.bat` and it does those three things, unattended, without
administrator rights.

**A 1080p MP4 that plays anywhere.** yt-dlp defaults to the best stream it can
find, which above 720p is usually AV1 or VP9 — fine in a browser, a stutter or
a black frame in an older player or editor. Asking for H.264 with a working
fallback chain means typing this:

```bash
yt-dlp -f "bestvideo[height<=1080][vcodec^=avc1]+bestaudio[ext=m4a]/bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best[height<=1080]/best" --merge-output-format mp4 --embed-metadata "URL"
```

In anydl you pick `1080p` from a dropdown. That exact chain is what it runs.

**An MP3 with the cover art and tags on it.** By hand:

```bash
yt-dlp -x --audio-format mp3 --audio-quality 192 --embed-metadata --embed-thumbnail "URL"
```

In anydl: choose *Audio only*, `mp3`, `192`.

**Failures that tell you what to do.** Ask yt-dlp for browser cookies on
Windows with the browser still open and you get `failed to load cookies`. anydl
says to close the browser including the tray icon, and mentions that Firefox
does not have the problem and that a `cookies.txt` always works.

**Working without ffmpeg instead of half-failing.** No ffmpeg means video and
audio tracks cannot be merged. anydl falls back to a pre-muxed stream and says
in the log that this caps you near 720p, rather than emitting a warning about
DASH containers and leaving you to work out why the file looks wrong.

None of this is a capability yt-dlp lacks. It is the same engine with the
tedious parts pre-answered.

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
python anydl.py
```

## Usage

The app window takes one or more links, a mode (video or audio only), and a
destination folder. Files go to `~/Downloads/anydl` by default.

Command line:

```bash
python anydl.py "URL"                       # best quality video
python anydl.py "URL" -q 1080p              # cap at 1080p
python anydl.py "URL" -a                    # MP3 at 192 kbps
python anydl.py "URL" -a -f wav             # WAV
python anydl.py "URL" -a -b 320             # MP3 at 320 kbps
python anydl.py "PLAYLIST_URL" --playlist -a   # whole playlist as MP3
python anydl.py "URL" -o /path/to/dir       # custom folder

# a site that needs an account
python anydl.py "https://www.instagram.com/p/..." --cookies-from-browser firefox
```

Run it with no arguments to open the interface.

| Flag | Meaning | Default |
|---|---|---|
| `-a`, `--audio` | extract audio only | off |
| `-f`, `--format` | `mp3`, `m4a`, `wav`, `opus`, `flac` | `mp3` |
| `-b`, `--bitrate` | audio bitrate in kbps | `192` |
| `-q`, `--quality` | `Best`, `2160p`, `1440p`, `1080p`, `720p`, `480p`, `360p` | `Best` |
| `-o`, `--output` | destination folder | `~/Downloads/anydl` |
| `--playlist` | download the entire playlist | off |
| `--cookies-from-browser` | `firefox`, `chrome`, `edge`, `brave`, ... | `none` |
| `--cookies` | path to a `cookies.txt` file | none |

## Sites that need an account

Nothing in the app is tied to one site: it hands the URL straight to yt-dlp.

Public pages work immediately. Sites that hide media behind an account
(Instagram is the usual one, and Vimeo now too) answer only to a signed-in
session, so you have to lend the app one:

- **From your browser** — pick it under *Sign-in cookies* in the interface, or
  pass `--cookies-from-browser firefox` on the command line.
- **From a file** — export a `cookies.txt` with a browser extension and select
  it with the *cookies.txt...* button, or pass `--cookies path/to/cookies.txt`.

> **Windows caveat:** Chromium-based browsers (Chrome, Edge, Brave, Opera) keep
> their cookie database locked while running, and reading it fails with a
> permission error. **Close the browser completely**, including any tray icon,
> then retry. Firefox does not lock its cookies, and a `cookies.txt` file works
> no matter what is open.

Your cookies are read locally and passed straight to yt-dlp. They are never
written anywhere by this app, and never leave your machine except as the normal
authentication headers the target site already expects.

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
any player or editor. On `Best` it does not filter by codec: YouTube, for one,
only serves H.264 up to 1080p, so filtering there would silently throw away 4K
and leave you with a 1080p file.

## Troubleshooting

**"ffmpeg not found" right after installing it.** `PATH` only updates for
processes started afterwards. Close the app and the terminal, then reopen.

**Extraction errors after a site changes something.** This is the common one,
and it is almost always fixed upstream within days. Update yt-dlp:

```bash
python -m pip install -U yt-dlp
```

**The window does not open on Linux.** tkinter is missing — see the install
section above.

## Requirements

- Python 3.9+
- yt-dlp
- ffmpeg (optional, recommended)
- Pillow — only to regenerate the icon, never to run the app

## License

MIT — see [LICENSE](LICENSE).

Downloading content you do not own may breach a site's Terms of Service and
local copyright law. Use this on material you have the rights to.
