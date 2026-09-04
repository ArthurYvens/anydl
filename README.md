# anydl

A desktop front end for [yt-dlp](https://github.com/yt-dlp/yt-dlp).

**yt-dlp does the real work.** The ~1700 site extractors, the download engine,
the format selection language, the ffmpeg orchestration, the cookie handling —
all of it is theirs. anydl is a window over it, an installer, and a set of
defaults you would otherwise have to type by hand. It is about 1500 lines
against yt-dlp's 240,000.

So: if you live in a terminal and already have Python and ffmpeg, use yt-dlp
directly. It is the better tool for you. anydl is for the case where you do
not, or where you would rather click than remember the incantations below.

Download video as MP4, or extract just the audio (MP3, M4A, WAV, OPUS, FLAC),
from YouTube, Instagram, X/Twitter, TikTok, Facebook, Reddit, Twitch, Vimeo,
SoundCloud, Dailymotion and the rest of the yt-dlp list.

- Video up to 4K, or capped at a resolution you pick
- Audio extraction with a bitrate of your choice, cover art and tags embedded
- Presets: one click for an MP3 at 320, a 1080p MP4, something phone sized
- A queue: one line per link, each with its own progress and its own cancel
- Preview: the thumbnail, what the link holds, and how big it will be
- Watches the clipboard, if you let it: copy a link and it is already there
- Take only a part of a video, by typing where it starts and where it ends
- Pause a download and pick it up later, from the bytes already on disk
- A speed limit you can move while a download is running
- Subtitles, as a `.srt` file or inside the video
- SponsorBlock: cut the sponsor segments out
- It keeps yt-dlp up to date for you, which is what usually breaks
- No Python knowledge required: the setup script installs everything

## What you get over plain yt-dlp

**Getting it running at all.** With yt-dlp you first install Python, then pip
install yt-dlp, then install ffmpeg and get it onto `PATH` yourself. Here you
double-click `install.bat` and it does those three things, unattended, without
administrator rights.

**Staying working.** Sites change their pages; yt-dlp follows, usually within
days. A copy that is a few weeks old is the single most common reason a
download starts failing, and the message you get — `unable to extract player
response` — says nothing about that. anydl checks the current release in the
background when it opens, and if yours is behind it says so and offers a button
that runs the upgrade and shows you the output. Errors that look like a stale
extractor point at the same button.

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

In anydl: choose *Audio only*, `mp3`, `192`. Or click **Music 320** and skip
the three dropdowns; the line underneath says what the preset will produce, and
the dropdowns still show — and still accept — what it picked.

**A queue rather than a batch.** Paste twenty links and each one gets a row
with its own status, percentage, speed and ETA. Each row also keeps the
settings that were on screen when you added it, so an MP3 and a 1080p video can
sit in the same queue. One failure marks that row and the queue carries on, and
cancelling a row leaves the others alone.

**Knowing what you are about to download.** yt-dlp will list the formats with
`-F`, in a terminal, after you have found the flag. *Preview* asks the site the
same question and answers a more useful one: with the settings you have set
right now, this is the stream anydl would take and this is what it weighs. In
audio mode it also says what the file becomes — a 10 MB Opus stream is a 27 MB
MP3 at 320, and nothing else tells you that before the fact.

**Catching the link you just copied.** A command line tool cannot watch your
clipboard; there is no command line tool running. Tick *Watch the clipboard*
and every link you copy lands in the box, ready for one click. Nothing is
stored and nothing is sent: it is compared against the last thing seen, and
anything that is not a single http link is ignored.

**Changing your mind halfway.** yt-dlp takes `-r 2M` when it starts and that is
that; there is no pausing it either — Ctrl+C stops the run. Here the speed
limit is a slider that moves while the download is running, and *Pause* stops
one without losing the bytes already fetched. Resuming carries on from the
part file rather than starting over.

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

The window takes one or more links, a preset or your own choices, and a
destination folder. Files go to `~/Downloads/anydl` by default.

**Download** puts every link in the box on the queue and starts it. **Preview**
takes the first link and reports what it holds — title, length, the stream your
settings would fetch and its size — with an *Add to queue* button if you like
the look of it. **Watch the clipboard** drops each link you copy into the box.

The queue runs one at a time, in order, and you can keep adding while it works.
Double-click a row to open the folder it is going to; right-click one for
pause, resume, cancel and remove; `Delete` drops the rows that are not running.
**Pause** stops what is downloading and turns into **Resume**, which picks it
up from the bytes already on disk. The **speed limit** slider applies to the
download in flight, not just the next one.

**Only from / to** takes a slice instead of the whole video — `1:30` to `4:00`,
or `90`, or `1:02:03`. The file is named after the slice, so it never lands on
top of the full version, and the cut needs ffmpeg. Leave both empty for
everything.

### Presets

| Preset | What it does |
|---|---|
| Music 320 | audio only, MP3 at 320 kbps, cover art and tags embedded |
| Archive 1080p | H.264 MP4 capped at 1080p |
| Phone 720p | H.264 MP4 capped at 720p |

A preset only moves the same controls you would have set yourself; change one
afterwards and the selection becomes *Custom*.

### Command line

```bash
python anydl.py "URL"                       # best quality video
python anydl.py "URL" -q 1080p              # cap at 1080p
python anydl.py "URL" -a                    # MP3 at 192 kbps
python anydl.py "URL" -a -f wav             # WAV
python anydl.py "URL" -p music              # the Music 320 preset
python anydl.py "URL" -p music -b 128       # ...with the bitrate overridden
python anydl.py "URL" --subs en             # plus an English .srt
python anydl.py "URL" --subs en --embed-subs   # ...inside the MP4 instead
python anydl.py "URL" --sponsorblock        # cut the sponsor segments out
python anydl.py "URL" --from 1:30 --to 4:00 # only that stretch of it
python anydl.py "URL" --limit-rate 2        # no faster than 2 MB/s
python anydl.py "PLAYLIST_URL" --playlist -a   # whole playlist as MP3
python anydl.py "URL" -o /path/to/dir       # custom folder

python anydl.py --version                   # what yt-dlp and ffmpeg it found
python anydl.py --update                    # update yt-dlp

# a site that needs an account
python anydl.py "https://www.instagram.com/p/..." --cookies-from-browser firefox
```

Run it with no arguments to open the interface.

| Flag | Meaning | Default |
|---|---|---|
| `-p`, `--preset` | `music`, `archive`, `phone` (explicit flags still win) | none |
| `-a`, `--audio` | extract audio only | off |
| `-f`, `--format` | `mp3`, `m4a`, `wav`, `opus`, `flac` | `mp3` |
| `-b`, `--bitrate` | audio bitrate in kbps | `192` |
| `-q`, `--quality` | `Best`, `2160p`, `1440p`, `1080p`, `720p`, `480p`, `360p` | `Best` |
| `-o`, `--output` | destination folder | `~/Downloads/anydl` |
| `--playlist` | download the entire playlist | off |
| `--subs` | subtitle language: `en`, `pt`, a comma separated list, or `all` | none |
| `--embed-subs` | put them inside the video instead of a `.srt` beside it | off |
| `--sponsorblock` | cut sponsor, self-promo and reminder segments | off |
| `--from` | start the file here: `1:30`, `90` or `1:02:03` | start |
| `--to` | and stop it here (needs ffmpeg) | end |
| `--limit-rate` | cap the download at this many MB per second | none |
| `--cookies-from-browser` | `firefox`, `chrome`, `edge`, `brave`, ... | `none` |
| `--cookies` | path to a `cookies.txt` file | none |
| `--update` | update yt-dlp and exit | |
| `--version` | print the yt-dlp and ffmpeg in use, and exit | |

### Subtitles

Pick a language and you get a `.srt` next to the video, converted from whatever
the site serves — YouTube's `.vtt` is not something every player opens. Tick
*inside the video* and the track goes into the MP4 instead, with no loose file.
Machine-made captions are requested alongside real ones, since most of YouTube
has nothing else; where a human subtitle exists it is preferred.

### SponsorBlock

Removes the sponsor, self-promotion and reminder segments using the
[SponsorBlock](https://sponsor.ajay.app/) database, so the file you keep is
shorter than the one that was published. It only applies to YouTube, and only
to videos somebody has already submitted segments for. Needs ffmpeg.

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
| Subtitles, SponsorBlock | yes | not offered |

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
and it is almost always fixed upstream within days. The app checks for it on
startup and offers the button; otherwise:

```bash
python anydl.py --update
```

which is the same thing as `python -m pip install -U yt-dlp`. If a download had
already run in that window, restart it afterwards — the old yt-dlp is loaded
into the running process and stays there.

**The window does not open on Linux.** tkinter is missing — see the install
section above.

## Requirements

- Python 3.9+
- yt-dlp
- ffmpeg (optional, recommended)

## License

MIT — see [LICENSE](LICENSE).

Downloading content you do not own may breach a site's Terms of Service and
local copyright law. Use this on material you have the rights to.
