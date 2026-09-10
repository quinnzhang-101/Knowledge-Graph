#!/usr/bin/env python3
"""
epub_normalizer.py — Flatten EPUB to canonical text with chapter index.

Part of Book-Scribe Historical Knowledge Package v1 (Phase 1: source layer).

Produces two outputs in <output-dir>/<book_slug>/:
    source_normalized.txt   UTF-8 flat text; all Evidence char offsets anchor here.
    source_index.json       Chapter index with char_start/char_end per chapter.

Locked design rules (2026-09-10):
    - offset unit: CHAR position (not byte) — critical for CJK text
    - chapter_id: pure sequence number (ch-001, ch-002, ...; ch-notes-NNN for notes)
    - body text only (no injected title prefix); chapter title lives in index
    - paragraphs joined by \\n\\n; chapters separated by \\n\\n
    - skipped items (cover/nav/image-only/<50 chars) recorded in skipped_items
    - original EPUB path recorded in normalized_from (we do NOT copy the EPUB)
    - book slug: author-assigned (passed via --book-slug); person slugs use pypinyin elsewhere

Usage:
    python epub_normalizer.py <epub_path> --book-slug <slug> [--output-dir <dir>] [--verbose]
"""

import argparse
import json
import re
import sys
import warnings
from datetime import date
from pathlib import Path
from typing import Tuple

# ebooklib emits deprecation noise on import; silence it
warnings.filterwarnings("ignore")

import ebooklib
from ebooklib import epub
from bs4 import BeautifulSoup


# --- constants ------------------------------------------------------------

SKIP_HREF_PATTERNS = re.compile(
    r"(cover|nav|toc|titlepage|copyright|dedication|contents|index|logo)",
    re.IGNORECASE,
)
NOTES_HREF_PATTERNS = re.compile(r"(note|footnote|endnote|annotation)", re.IGNORECASE)
# Below this many chars, treat as junk (covers, nav, stray title pages).
# 30 chars filters "Cover"/"Title"/"Contents" (≤10 chars) while sparing real
# short chapters (poems, interludes, one-line reference entries).
MIN_CHAPTER_CHARS = 30

# Front-matter detection by RESOLVED TITLE. Needed because front-matter pages
# are often named generically ('text/part0003.html') — only the TOC title
# ('目录', '版权页') reveals they are not content.
#   - Chinese: substring match (handles '目录页', '版权页' variants)
#   - English: exact match on trimmed lowercase (avoids 'topic' matching 'toc')
FRONT_MATTER_CN = ("书签页", "书名页", "版权页", "目录", "封面", "扉页")
FRONT_MATTER_EN = {
    "title page", "titlepage", "copyright", "contents",
    "table of contents", "toc", "cover", "half title",
}


def is_front_matter(title: str) -> bool:
    """True if the resolved title marks a front-matter page (not content)."""
    if not title:
        return False
    t = title.strip()
    if any(cn in t for cn in FRONT_MATTER_CN):
        return True
    return t.lower() in FRONT_MATTER_EN


# --- helpers --------------------------------------------------------------

def is_skippable(href: str, content: bytes) -> Tuple[bool, str]:
    """Decide whether a spine item should be skipped. Returns (skip, reason)."""
    if href and SKIP_HREF_PATTERNS.search(href):
        return True, "skippable_pattern"
    if b"<img" in content:
        try:
            soup = BeautifulSoup(content, "lxml")
            text = soup.get_text(strip=True)
            if len(text) < MIN_CHAPTER_CHARS:
                return True, "image_only_or_too_short"
        except Exception:
            pass
    return False, ""


def extract_text(content: bytes) -> str:
    """Extract clean body text from XHTML. Paragraphs joined by \\n\\n."""
    soup = BeautifulSoup(content, "lxml")
    # Strip non-content tags
    for tag in soup.find_all(["script", "style", "head"]):
        tag.decompose()
    # Collect block-level content
    paragraphs = []
    for p in soup.find_all(["p", "h1", "h2", "h3", "h4", "h5", "h6"]):
        text = p.get_text(strip=True)
        if text:
            paragraphs.append(text)
    if not paragraphs:
        # Fallback: grab whatever text exists
        return soup.get_text(separator="\n", strip=True)
    return "\n\n".join(paragraphs)


def build_toc_map(book) -> dict:
    """Build a flat href → title map from TOC (recursive).

    Real EPUBs (e.g. Z-Library conversions) carry URL fragments in TOC hrefs:
        'text/part0004.html#3Q280-6942233d4dee4c22a497d156f0755292'
    while spine item names never do ('text/part0004.html'). Strip fragments
    on the TOC side so lookups against bare item names actually match.
    Without this, every title silently falls back to 'Section N'.
    """
    toc_map = {}

    def put(href: str, title: str) -> None:
        if not href or not title:
            return
        toc_map[href.split("#")[0]] = title

    def walk(entries):
        for entry in entries:
            # ebooklib nests as (Section, [children]) tuples
            if isinstance(entry, tuple) and len(entry) == 2:
                section, children = entry
                if hasattr(section, "href") and hasattr(section, "title"):
                    put(section.href, section.title)
                if children:
                    walk(children)
            elif hasattr(entry, "href") and hasattr(entry, "title"):
                put(entry.href, entry.title)

    try:
        walk(book.toc)
    except Exception:
        pass
    return toc_map


def resolve_title(href: str, toc_map: dict, fallback_idx: int) -> str:
    """Title resolution order: exact href → href without fragment → 'Section N'."""
    if href in toc_map:
        return toc_map[href]
    href_base = href.split("#")[0] if href else ""
    if href_base and href_base in toc_map:
        return toc_map[href_base]
    return f"Section {fallback_idx}"


# --- core -----------------------------------------------------------------

def normalize_epub(epub_path: Path, book_slug: str, output_dir: Path,
                   skip_front_matter: bool = True) -> dict:
    """Read EPUB, produce source_normalized.txt + source_index.json. Returns index dict.

    Args:
        skip_front_matter: drop title/copyright/TOC pages identified by resolved
            TOC title. Only takes effect when TOC title resolution succeeds.
    """
    if not epub_path.exists():
        raise FileNotFoundError(f"EPUB not found: {epub_path}")

    book = epub.read_epub(str(epub_path), options={"ignore_ncx": False})
    toc_map = build_toc_map(book)

    # Spine is a list of (idref, linear) tuples; some malformed EPUBs use plain strings
    spine = book.spine
    items_by_idref = {
        item.get_id(): item
        for item in book.get_items_of_type(ebooklib.ITEM_DOCUMENT)
    }

    chapters = []
    skipped_items = []
    parts = []
    char_pos = 0
    chapter_idx = 0
    notes_idx = 0
    section_idx = 1

    for spine_entry in spine:
        if isinstance(spine_entry, tuple):
            idref, linear = spine_entry[0], spine_entry[1]
        else:
            idref, linear = spine_entry, True
        if not linear:
            continue

        item = items_by_idref.get(idref)
        if item is None:
            skipped_items.append({
                "idref": idref, "href": None, "reason": "idref_not_in_document_items",
            })
            continue

        href = item.get_name() or ""
        try:
            content = item.get_content()
        except Exception as e:
            skipped_items.append({
                "idref": idref, "href": href, "reason": f"read_error: {e}",
            })
            continue

        # Resolve title BEFORE skip checks: front matter is identified by
        # title, not filename (e.g. 'text/part0003.html' → '目录').
        # section_idx only advances for KEPT chapters, so 'Section N'
        # fallbacks stay sequential across skipped items.
        raw_title = resolve_title(href, toc_map, section_idx)

        skip, reason = is_skippable(href, content)
        if skip:
            skipped_items.append({"idref": idref, "href": href,
                                  "title": raw_title, "reason": reason})
            continue

        if skip_front_matter and is_front_matter(raw_title):
            skipped_items.append({"idref": idref, "href": href,
                                  "title": raw_title, "reason": "front_matter"})
            continue

        text = extract_text(content)
        if len(text.strip()) < MIN_CHAPTER_CHARS:
            skipped_items.append({"idref": idref, "href": href,
                                  "title": raw_title, "reason": "below_min_chars"})
            continue

        is_notes = bool(NOTES_HREF_PATTERNS.search(href))
        if is_notes:
            notes_idx += 1
            chapter_id = f"ch-notes-{notes_idx:03d}"
            title = f"Notes {notes_idx}"
        else:
            chapter_idx += 1
            chapter_id = f"ch-{chapter_idx:03d}"
            title = raw_title
            section_idx += 1

        char_start = char_pos
        char_end = char_start + len(text)
        chapters.append({
            "chapter_id": chapter_id,
            "title": title,
            "char_start": char_start,
            "char_end": char_end,
            "is_notes": is_notes,
            "source_href": href,
            "idref": idref,
        })
        parts.append(text)
        # +2 for the \n\n separator we will join with
        char_pos = char_end + 2

    if not chapters:
        raise RuntimeError(
            "No chapters extracted — EPUB may be malformed or all items were skipped."
        )

    normalized_text = "\n\n".join(parts)

    out_dir = output_dir / book_slug
    out_dir.mkdir(parents=True, exist_ok=True)

    text_path = out_dir / "source_normalized.txt"
    text_path.write_text(normalized_text, encoding="utf-8")

    index = {
        "schema_version": "1.0",
        "book_id": f"book:{book_slug}",
        "book_slug": book_slug,
        "normalized_from": str(epub_path),
        "normalized_from_name": epub_path.name,
        "normalized_at": date.today().isoformat(),
        "total_chars": len(normalized_text),
        "chapter_count": len(chapters),
        "chapters": chapters,
        "skipped_items": skipped_items,
    }
    index_path = out_dir / "source_index.json"
    index_path.write_text(
        json.dumps(index, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return index


# --- CLI ------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Flatten EPUB to canonical text with chapter index "
            "(Book-Scribe Package v1, Phase 1)."
        )
    )
    parser.add_argument("epub_path", help="Path to the input EPUB file.")
    parser.add_argument(
        "--book-slug", required=True,
        help="Author-assigned book slug, e.g. wanli-fifteen-years.",
    )
    parser.add_argument(
        "--output-dir", default="./books",
        help="Output directory (default: ./books).",
    )
    parser.add_argument(
        "--no-skip-front-matter", action="store_true",
        help="Keep title/copyright/TOC pages (default: dropped when identified by TOC title).",
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Print skipped items in detail.",
    )
    args = parser.parse_args()

    epub_path = Path(args.epub_path).resolve()
    output_dir = Path(args.output_dir).resolve()

    print(f"Input:    {epub_path}")
    print(f"Slug:     {args.book_slug}")
    print(f"Output:   {output_dir / args.book_slug}")
    print()

    try:
        index = normalize_epub(
            epub_path, args.book_slug, output_dir,
            skip_front_matter=not args.no_skip_front_matter,
        )
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    print("Done.")
    print(f"  Total chars:  {index['total_chars']}")
    print(f"  Chapters:     {index['chapter_count']}")
    print(f"  Skipped:      {len(index['skipped_items'])}")
    if args.verbose and index["skipped_items"]:
        print("\n  Skipped items:")
        for s in index["skipped_items"]:
            title = f" [{s['title']}]" if s.get("title") else ""
            print(f"    {s.get('href') or s.get('idref')}{title} — {s.get('reason')}")
    print("\nFiles:")
    print(f"  {output_dir / args.book_slug / 'source_normalized.txt'}")
    print(f"  {output_dir / args.book_slug / 'source_index.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
