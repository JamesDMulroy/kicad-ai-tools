from pathlib import Path

from scripts.public_wording import rewrite


def test_rewrite_python_markdown_and_idempotence(tmp_path: Path):
    py = tmp_path / "sample.py"
    py.write_text(
        '# owner\'s desktop\n'
        '# covers: ADE-3\n'
        '"""owner\'s desktop"""\n'
        'value = 1  # covers: XYZ-9\n'
        'normal = "owner\'s desktop"\n'
        'owner_desktop_scan = "owner\'s desktop"\n'
        'make = lambda value: value\n',
        encoding="utf-8",
    )
    md = tmp_path / "README.md"
    md.write_text("owner's desktop\nowner desktop owner-desktop\n", encoding="utf-8")

    rewritten = rewrite(py.read_text(encoding="utf-8"), ".py")
    assert rewritten == (
        "# user's desktop\n"
        '"""user\'s desktop"""\n'
        "value = 1\n"
        'normal = "owner\'s desktop"\n'
        'owner_desktop_scan = "owner\'s desktop"\n'
        'make = lambda value: value\n'
    )
    assert rewrite(md.read_text(encoding="utf-8"), ".md") == (
        "user's desktop\nuser desktop user-desktop\n"
    )

    py.write_text(rewritten, encoding="utf-8")
    assert rewrite(py.read_text(encoding="utf-8"), ".py") == rewritten


def test_rewrite_python_eof_tokens(tmp_path: Path):
    no_newline = tmp_path / "no_newline.py"
    no_newline.write_text("# owner's desktop", encoding="utf-8")
    assert rewrite(no_newline.read_text(encoding="utf-8"), ".py") == "# user's desktop"

    terminal_comment = tmp_path / "terminal_comment.py"
    terminal_comment.write_text("value = 1  # covers: EOF-1", encoding="utf-8")
    assert rewrite(terminal_comment.read_text(encoding="utf-8"), ".py") == "value = 1"
