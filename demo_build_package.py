#!/usr/bin/env python3
"""
demo_build_package.py — Build a minimal Package from a REAL normalized book
and run validate_package on it.

This is a FIXTURE / end-to-end reference, NOT the real generator. The real
generator (Phase 3) will have an LLM produce these objects. This script
hardcodes a small set of objects so Phase 2 (the validator) can be exercised
against real char offsets and real original_text.

Usage:
    python demo_build_package.py <book_dir> [--chapter-id ch-002] [--validate]

Example:
    python demo_build_package.py books/sunzi-bingfa --chapter-id ch-002 --validate
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from validate_package import validate_package  # noqa: E402


def write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def find_slice(text: str, keyword: str, before: int = 0, after: int = 90):
    """Locate keyword in text; return (start, end) covering a sentence-ish window."""
    i = text.find(keyword)
    if i < 0:
        return None
    start = max(0, i - before)
    end = min(len(text), i + after)
    # Extend forward to the next 。 if one appears soon after
    for j in range(min(len(text) - 1, i + after), min(len(text), i + after + 60)):
        if text[j] == "。":
            end = j + 1
            break
    return start, end


def build_demo_package(book_dir: Path, chapter_id: str) -> dict:
    """Build a minimal Package from one real chapter. Returns a summary dict."""
    book_slug = book_dir.name
    norm_path = book_dir / "source_normalized.txt"
    index_path = book_dir / "source_index.json"
    if not norm_path.exists() or not index_path.exists():
        raise SystemExit(
            f"ERROR: {book_dir} not normalized yet. Run epub_normalizer.py first."
        )

    source_text = norm_path.read_text(encoding="utf-8")
    index = json.loads(index_path.read_text(encoding="utf-8"))
    chapter = next((c for c in index["chapters"]
                    if c["chapter_id"] == chapter_id), None)
    if chapter is None:
        available = ", ".join(c["chapter_id"] for c in index["chapters"])
        raise SystemExit(
            f"ERROR: chapter {chapter_id} not found. Available: {available}"
        )

    ch_start, ch_end = chapter["char_start"], chapter["char_end"]
    ch_body = source_text[ch_start:ch_end]

    # --- Evidence: two real excerpts from this chapter -------------------
    evidence_specs = [
        ("0001", "兵者，国之大事", "孙子开篇即断言战争是国家生死存亡的大事。"),
        ("0002", "道者，令民与上同意", "孙子将「道」界定为君民意志一致。"),
    ]
    evidence = []
    for eid, keyword, claim in evidence_specs:
        sl = find_slice(ch_body, keyword)
        if sl is None:
            raise SystemExit(
                f"ERROR: keyword {keyword!r} not found in chapter {chapter_id}. "
                f"Pick a different chapter or keyword."
            )
        rel_start, rel_end = sl
        abs_start, abs_end = ch_start + rel_start, ch_start + rel_end
        evidence.append({
            "evidence_id": f"evidence:{book_slug}:{eid}",
            "source_book_id": f"book:{book_slug}",
            "source_location": {
                "chapter_id": chapter_id,
                "char_start": abs_start,
                "char_end": abs_end,
            },
            "evidence_type": "textual",
            "claim": claim,
            "original_text": source_text[abs_start:abs_end],
            "epistemic_status": "source_text",
            "_file": f"ev-{eid}.json",
        })

    ev1, ev2 = evidence[0]["evidence_id"], evidence[1]["evidence_id"]

    # --- manifest.json ---------------------------------------------------
    write_json(book_dir / "manifest.json", {
        "package_id": f"package:{book_slug}",
        "package_type": "historical_knowledge_package",
        "schema_version": "1.0",
        "source_book_id": f"book:{book_slug}",
        "builder": {"name": "book-scribe", "version": "0.1.0"},
        "created_at": "2026-09-10",
        "contents": {
            "characters": 1, "events": 1, "causal_chains": 0,
            "contexts": 1, "mirrors": 1, "topics": 1,
            "relationships": 1, "evidence": 2,
        },
    })

    # --- book.json -------------------------------------------------------
    write_json(book_dir / "book.json", {
        "book_id": f"book:{book_slug}",
        "title": chapter["title"] + "（示例 Package）",
        "authors": [{"name": "孙武"}, {"name": "陈曦"}],
        "language": "zh-CN",
        "book_type": "classical_military_treatise",
        "publication": {"year": 1997},
    })

    # --- evidence/ -------------------------------------------------------
    for ev in evidence:
        obj = {k: v for k, v in ev.items() if k != "_file"}
        write_json(book_dir / "evidence" / ev["_file"], obj)

    # --- characters/ -----------------------------------------------------
    write_json(book_dir / "characters" / "char-sun-wu.json", {
        "entity_id": f"entity:book:{book_slug}:person:sun-wu",
        "entity_type": "person",
        "canonical_name": "孙武",
        "aliases": ["孙子", "孙武子", "Sun Tzu"],
        "source_book_id": f"book:{book_slug}",
        "evidence_refs": [{"id": ev1}],
    })

    # --- events/ ---------------------------------------------------------
    event_id = f"event:book:{book_slug}:sunzi-on-war-as-state-affair"
    write_json(book_dir / "events" / "event-war-as-state-affair.json", {
        "event_id": event_id,
        "event_type": "other",
        "title": "孙子论「兵者，国之大事」",
        "time": {"value": "春秋末期", "precision": "period"},
        "location": None,
        "participants": [f"entity:book:{book_slug}:person:sun-wu"],
        "description": {
            "claim": "孙子在《计篇》开篇即断言战争关系国家生死存亡，必须审慎考察。",
            "epistemic_status": "source_fact",
        },
        "source_book_id": f"book:{book_slug}",
        "evidence_refs": [{"id": ev1}, {"id": ev2}],
    })

    # --- topics/ ---------------------------------------------------------
    write_json(book_dir / "topics" / "topic-miaosuan.json", {
        "topic_id": "topic:strategic-calculation",
        "name": "庙算",
        "aliases": ["战略筹划", "战前庙算"],
        "source_book_id": f"book:{book_slug}",
    })

    # --- relationships/ --------------------------------------------------
    write_json(book_dir / "relationships" / "rel-sun-wu-dictum.json", {
        "relationship_id": f"relationship:book:{book_slug}:sun-wu-dictum",
        "source": f"entity:book:{book_slug}:person:sun-wu",
        "relation": "authored",
        "target": event_id,
        "source_book_id": f"book:{book_slug}",
        "evidence_refs": [{"id": ev1}],
    })

    # --- context/ --------------------------------------------------------
    context_id = f"context:book:{book_slug}:spring-autumn-military-thought"
    write_json(book_dir / "context" / "ctx-spring-autumn.json", {
        "context_id": context_id,
        "title": "春秋末期军事思想背景",
        "content": "《孙子兵法》成书于春秋末期，其时诸侯争霸、战争频繁，兵学思想由占卜转向理性筹划。",
        "source_book_id": f"book:{book_slug}",
        "evidence_refs": [{"id": ev1}],
    })

    # --- mirror/ ---------------------------------------------------------
    write_json(book_dir / "mirror" / "mirror-rationalism.json", {
        "mirror_id": f"mirror:book:{book_slug}:rationalism",
        "title": "由神秘主义向理性筹划的转向",
        "content": "将「五事」「七计」视为可计算变量，显示孙子把战争从占卜祭祷中剥离出来的理性取向。",
        "epistemic_status": "model_interpretation",
        "basis_refs": [event_id, context_id, "topic:strategic-calculation"],
        "source_book_id": f"book:{book_slug}",
    })

    return {
        "book_slug": book_slug,
        "chapter_id": chapter_id,
        "chapter_title": chapter["title"],
        "evidence": [
            {"id": e["evidence_id"],
             "offsets": (e["source_location"]["char_start"],
                         e["source_location"]["char_end"]),
             "text": e["original_text"][:60] + ("..." if len(e["original_text"]) > 60 else "")}
            for e in evidence
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a demo Package from a real normalized book (fixture, not the real generator)."
    )
    parser.add_argument("book_dir", help="Normalized book directory, e.g. books/sunzi-bingfa")
    parser.add_argument("--chapter-id", default=None,
                        help="Chapter to draw evidence from (default: first non-notes chapter).")
    parser.add_argument("--validate", action="store_true",
                        help="Run validate_package after building.")
    args = parser.parse_args()

    book_dir = Path(args.book_dir).resolve()
    if not book_dir.exists():
        print(f"ERROR: book_dir not found: {book_dir}", file=sys.stderr)
        return 2

    # Default: first non-notes chapter
    chapter_id = args.chapter_id
    if chapter_id is None:
        index = json.loads((book_dir / "source_index.json").read_text(encoding="utf-8"))
        non_notes = [c for c in index["chapters"] if not c["is_notes"]]
        if not non_notes:
            print("ERROR: no non-notes chapters found", file=sys.stderr)
            return 2
        chapter_id = non_notes[0]["chapter_id"]

    summary = build_demo_package(book_dir, chapter_id)

    print(f"Built demo Package: {summary['book_slug']}")
    print(f"  Chapter: {summary['chapter_id']} — {summary['chapter_title']}")
    print("  Evidence:")
    for e in summary["evidence"]:
        print(f"    {e['id']} @ [{e['offsets'][0]}:{e['offsets'][1]}]  {e['text']}")

    if not args.validate:
        return 0

    print()
    report = validate_package(book_dir)
    if report.ok():
        print("\nVALIDATION PASSED — demo Package is valid.")
        if report.warnings:
            print(f"({len(report.warnings)} warning(s))")
        return 0
    print(f"\nVALIDATION FAILED — {len(report.errors)} error(s):")
    for i, issue in enumerate(report.errors, 1):
        loc = f"[{issue.file}] " if issue.file else ""
        print(f"{i:3}. {loc}{issue.code}: {issue.message}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
