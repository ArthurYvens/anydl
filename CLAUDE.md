# AnyDL — Claude Code instructions

## Project

AnyDL is a Python application/CLI around yt-dlp with optional ffmpeg.
The main application is currently concentrated in `anydl.py`.

## Development rules

- Preserve Python 3.9+ compatibility unless explicitly changing support.
- Do not perform real downloads in unit tests.
- Mock network, yt-dlp, ffmpeg, subprocess, and pip behavior.
- Never use real cookies or credentials in tests.
- Prefer `pytest`.
- Every bug fix should have a regression test when practical.
- Avoid unrelated refactors during bug fixes.
- Keep CLI and GUI behavior aligned where they share business logic.
- Treat cross-platform behavior as important: Windows, macOS, Linux.
- Do not expose cookie contents or authentication data in logs.

## Required validation

After meaningful Python changes:

    python -m compileall .
    python -m pytest -q

If available:

    python -m ruff check .

For CLI changes:

    python anydl.py --help
    python anydl.py --version

## QA agent

The repository includes `.claude/agents/qa-engineer.md`.

Use it for:

- bug investigation
- regression testing
- code changes that affect downloads
- yt-dlp/ffmpeg integration
- CLI behavior
- filesystem/cross-platform changes
- pre-release QA

The QA agent must reproduce before fixing when possible and must report
exact commands/results.
