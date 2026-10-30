def test_explain_strips_error_prefix(converter):
    assert converter._explain("ERROR: something failed") == "something failed"


def test_explain_login_error(converter):
    result = converter._explain("login required")

    assert "account" in result.lower()
    assert "cookies" in result.lower()


def test_explain_browser_cookie_error(anydl_module, monkeypatch):
    monkeypatch.setattr(anydl_module, "find_ffmpeg", lambda: None)
    converter = anydl_module.Converter(
        on_log=lambda *_: None,
        browser="chrome",
    )

    result = converter._explain("Permission denied reading cookie database")

    assert "chrome" in result
    assert "close" in result.lower()


def test_explain_stale_ytdlp_error(converter):
    result = converter._explain("Unable to extract something")

    assert converter.update_hint in result
