"""Sync the real frontend into the GitHub Pages static preview.

The Pages preview intentionally reuses static/index.html and static/*
so the public demo stays visually aligned with the app. The only extra
piece is docs/static/js/mock-api.js, which replaces backend API calls.
"""

from __future__ import annotations

import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = ROOT / "static"
DOCS_DIR = ROOT / "docs"
DOCS_STATIC_DIR = DOCS_DIR / "static"
INDEX_SRC = STATIC_DIR / "index.html"
INDEX_DEST = DOCS_DIR / "index.html"
MOCK_SCRIPT = '<script src="./static/js/mock-api.js?v=github-pages-preview"></script>'
APP_SCRIPT = '<script src="./static/js/app.js?v=20260630-bugfix9"></script>'
MOCK_API_PATH = DOCS_STATIC_DIR / "js" / "mock-api.js"


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def write_text(path: Path, value: str) -> None:
    path.write_text(value.rstrip() + "\n", encoding="utf-8")


def copy_tree(src: Path, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)


def rewrite_static_paths(value: str) -> str:
    return (
        value.replace('href="/static/', 'href="./static/')
        .replace('src="/static/', 'src="./static/')
        .replace("'/static/", "'./static/")
        .replace('"/static/', '"./static/')
    )


def sync_index() -> None:
    html = rewrite_static_paths(read_text(INDEX_SRC))
    if MOCK_SCRIPT not in html:
        html = html.replace(APP_SCRIPT, MOCK_SCRIPT + "\n" + APP_SCRIPT)
    write_text(INDEX_DEST, html)


def sync_static_assets() -> None:
    mock_api = MOCK_API_PATH.read_text(encoding="utf-8") if MOCK_API_PATH.exists() else ""

    copy_tree(STATIC_DIR / "css", DOCS_STATIC_DIR / "css")
    copy_tree(STATIC_DIR / "js", DOCS_STATIC_DIR / "js")
    copy_tree(STATIC_DIR / "assets", DOCS_STATIC_DIR / "assets")

    if mock_api:
        MOCK_API_PATH.write_text(mock_api.rstrip() + "\n", encoding="utf-8")

    for path in (DOCS_STATIC_DIR / "js").glob("*.js"):
        js = rewrite_static_paths(read_text(path))
        write_text(path, js)


def main() -> None:
    DOCS_STATIC_DIR.mkdir(parents=True, exist_ok=True)
    sync_static_assets()
    sync_index()
    print("Synced GitHub Pages static preview from static/.")


if __name__ == "__main__":
    main()
