"""Command line entry points."""

import argparse
import sys

from .config import APP_NAME, AUDIO_FORMATS, BROWSERS, PRESETS
from .converter import Converter
from .environment import (
    default_output_dir,
    find_ffmpeg,
    installed_ytdlp_version,
    latest_ytdlp_version,
    update_ytdlp,
)
from .text import describe_settings, find_preset, format_progress, parse_timestamp, parse_version


# ====================================================================== CLI
def report_versions():
    """What the app is actually running with, for a bug report or a check."""
    installed = installed_ytdlp_version()
    print("%s using yt-dlp %s" % (APP_NAME, installed or "(not installed)"))
    ffmpeg_dir = find_ffmpeg()
    print("ffmpeg: " + (ffmpeg_dir if ffmpeg_dir else "not found"))
    if not installed:
        return
    try:
        latest = latest_ytdlp_version()
    except Exception as exc:  # noqa: BLE001
        print("could not check for a newer yt-dlp: %s" % exc)
        return
    if parse_version(latest) > parse_version(installed):
        print("yt-dlp %s is available. Update with: %s --update" % (latest, APP_NAME))
    else:
        print("yt-dlp is up to date.")


def main():
    parser = argparse.ArgumentParser(
        prog="anydl",
        description="Download video as MP4 from any site yt-dlp supports, "
                    "or extract just the audio track.",
    )
    parser.add_argument("url", nargs="*", help="link(s) to download")
    # These default to None so a preset can fill them in and an explicit flag
    # can still win over the preset.
    parser.add_argument("-p", "--preset", choices=[p["cli"] for p in PRESETS],
                        help="a set of ready-made choices: "
                             + ", ".join("%s (%s)" % (p["cli"], p["name"]) for p in PRESETS))
    parser.add_argument("-a", "--audio", action="store_true", default=None,
                        help="extract audio only")
    parser.add_argument("-f", "--format", default=None, choices=AUDIO_FORMATS,
                        dest="audio_format", help="audio format (default: mp3)")
    parser.add_argument("-b", "--bitrate", default=None, help="audio bitrate in kbps")
    parser.add_argument("-q", "--quality", default=None,
                        help="video quality: Best, 1080p, 720p, ...")
    parser.add_argument("-o", "--output", default=default_output_dir(),
                        help="destination folder")
    parser.add_argument("--playlist", action="store_true", help="download the whole playlist")
    parser.add_argument("--subs", metavar="LANG", default=None,
                        help="also fetch subtitles: a language code (en, pt, ...), "
                             "a comma separated list, or all")
    parser.add_argument("--embed-subs", dest="embed_subs", action="store_true",
                        help="put the subtitles inside the video instead of a .srt beside it")
    parser.add_argument("--from", dest="section_start", metavar="TIME", default=None,
                        help="start the file at this point: 1:30, 90 or 1:02:03")
    parser.add_argument("--to", dest="section_end", metavar="TIME", default=None,
                        help="and stop it here (needs ffmpeg)")
    parser.add_argument("--limit-rate", dest="limit_rate", metavar="MB", type=float,
                        default=None, help="cap the download at this many MB per second")
    parser.add_argument("--sponsorblock", action="store_true",
                        help="cut sponsor, self-promotion and reminder segments out "
                             "of the file (YouTube, via the SponsorBlock database)")
    parser.add_argument("--cookies-from-browser", dest="browser", choices=BROWSERS,
                        default="none",
                        help="borrow the signed-in session from a local browser "
                             "(needed for Instagram, Vimeo and other gated sites)")
    parser.add_argument("--cookies", dest="cookie_file", default=None,
                        help="path to a cookies.txt file, as an alternative to "
                             "--cookies-from-browser")
    parser.add_argument("--update", action="store_true",
                        help="update yt-dlp to the current release and exit")
    parser.add_argument("--version", action="store_true",
                        help="show which yt-dlp and ffmpeg are in use, and exit")
    args = parser.parse_args()

    if args.version:
        report_versions()
        return

    if args.update:
        ok, message = update_ytdlp()
        print(message)
        sys.exit(0 if ok else 1)

    if not args.url:
        # Imported here, not at the top: the command line has to keep working
        # on a machine that has no tkinter installed at all.
        from .main_window import launch_gui

        launch_gui()
        return

    settings = {"mode": "video", "quality": "Best", "audio_format": "mp3", "bitrate": "192"}
    if args.preset:
        settings.update(find_preset(args.preset)["settings"])
    if args.audio:
        settings["mode"] = "audio"
    if args.audio_format:
        settings["audio_format"] = args.audio_format
    if args.bitrate:
        settings["bitrate"] = args.bitrate
    if args.quality:
        settings["quality"] = args.quality
    if args.preset:
        print(describe_settings(settings, bool(find_ffmpeg())))

    try:
        section_start = parse_timestamp(args.section_start)
        section_end = parse_timestamp(args.section_end)
    except ValueError as exc:
        parser.error("'%s' is not a time. Write 1:30, or 90, or 1:02:03." % exc)

    if (section_start is not None and section_end is not None
            and section_end <= section_start):
        parser.error("the end of the clip has to come after its start.")

    last = [-1]

    def on_progress(pct, stats):
        if int(pct) != last[0]:
            last[0] = int(pct)
            line = "%5.1f%%  %s" % (pct, format_progress(stats))
            print("\r" + line.ljust(70), end="", flush=True)

    converter = Converter(
        on_log=lambda m: print("\n" + str(m)),
        on_progress=on_progress,
        on_done=lambda ok, m: print("\n" + m),
        browser=args.browser,
        cookie_file=args.cookie_file,
    )
    if args.limit_rate:
        converter.set_rate_limit(int(args.limit_rate * 1048576))
    ok = converter.download(args.url, args.output, settings["mode"], settings["quality"],
                            settings["audio_format"], settings["bitrate"], args.playlist,
                            extras={"subtitles": args.subs,
                                    "embed_subs": args.embed_subs,
                                    "sponsorblock": args.sponsorblock,
                                    "section_start": section_start,
                                    "section_end": section_end})
    sys.exit(0 if ok else 1)
