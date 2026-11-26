"""Rewrite a small set of public-facing phrases without touching code strings."""

import argparse
import ast
import io
import re
import tokenize
from pathlib import Path


_REPLACEMENTS = (
    ("owner's input desktop", "user's input desktop"),
    ("owner's desktop", "user's desktop"),
    ("owner desktop", "user desktop"),
    ("owner-desktop", "user-desktop"),
)
_COVERS = re.compile(r"# covers:\s*[^\r\n]+\s*$")


def _wording(text: str) -> str:
    for old, new in _REPLACEMENTS:
        text = text.replace(old, new)
    return text


def _offsets(text: str):
    starts = [0]
    for match in re.finditer(r"\n", text):
        starts.append(match.end())
    return starts


def _span(starts, position):
    line, column = position
    return starts[line - 1] + column


def _python_rewrite(text: str) -> str:
    starts = _offsets(text)
    spans = []
    tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    tree = ast.parse(text)
    doc_positions = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", ())
        if not isinstance(body, list):
            continue
        if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) and isinstance(body[0].value.value, str):
            value = body[0].value
            doc_positions.add((value.lineno, value.col_offset, value.end_lineno, value.end_col_offset))
    for token in tokens:
        if token.type not in (tokenize.COMMENT, tokenize.STRING):
            continue
        start, end = _span(starts, token.start), _span(starts, token.end)
        if token.type == tokenize.COMMENT:
            line_start = starts[token.start[0] - 1]
            line_end = starts[token.end[0]] if token.end[0] < len(starts) else len(text)
            if _COVERS.fullmatch(token.string):
                before = text[line_start:start]
                if not before.strip():
                    spans.append((line_start, line_end, ""))
                else:
                    spans.append((line_start + len(before.rstrip()), end, ""))
            else:
                spans.append((start, end, _wording(token.string)))
        elif token.type == tokenize.STRING and (token.start[0], token.start[1], token.end[0], token.end[1]) in doc_positions:
            spans.append((start, end, _wording(token.string)))
    for start, end, replacement in sorted(spans, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text


def rewrite(path_text: str, suffix: str) -> str:
    """Return rewritten content for a file body and its suffix."""
    if suffix.lower() == ".md":
        return _wording(path_text)
    if suffix.lower() in (".py", ".pyw"):
        return _python_rewrite(path_text)
    return path_text


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    changed = 0
    for path in args.root.rglob("*"):
        if any(part in {".git", "venv", "node_modules"} for part in path.parts) or not path.is_file():
            continue
        if path.suffix.lower() not in {".md", ".py", ".pyw"}:
            continue
        old = path.read_text(encoding="utf-8")
        new = rewrite(old, path.suffix)
        if new != old:
            print(path)
            if not args.check:
                path.write_text(new, encoding="utf-8", newline="")
            changed += 1
    print(f"{changed} changed")


if __name__ == "__main__":
    main()
