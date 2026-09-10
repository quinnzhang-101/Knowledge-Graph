#!/usr/bin/env python3
"""
validate_package.py — Package v1 validator.

Part of Book-Scribe Historical Knowledge Package v1 (Phase 2: validator).

Checks (locked 2026-09-10):
    1.  Required files present: manifest.json, book.json,
        source_normalized.txt, source_index.json
    2.  Schema compliance: each JSON file validates against its schema
    3.  No duplicate IDs within each object type
    4.  manifest.contents.<type> counts match actual file counts
    5.  Every evidence_ref resolves to an existing evidence object
    6.  Every entity_id ref (events.participants, relationships.source) resolves
    7.  Every event_id ref (relationships.target, causal.cause/effect) resolves
    8.  Every mirror.basis_refs resolves to any Package object
    9.  Every knowledge object (char/event/rel/causal/context) has ≥1 evidence_ref
    10. Every evidence.source_location.chapter_id exists in source_index.json
    11. Every evidence char_start/char_end within source_normalized.txt bounds
    12. Every evidence.original_text matches source_normalized.txt[char_start:char_end]

Exit codes: 0 = valid, 1 = errors found, 2 = book_dir not found.

Usage:
    python validate_package.py <book_dir> [--verbose]
"""

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import jsonschema
from referencing import Registry, Resource

SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"


# Object type → (subdirectory, schema file, ID field)
OBJECT_TYPES = {
    "characters":    ("characters",    "character.schema.json",    "entity_id"),
    "events":        ("events",        "event.schema.json",        "event_id"),
    "evidence":      ("evidence",      "evidence.schema.json",     "evidence_id"),
    "topics":        ("topics",        "topic.schema.json",        "topic_id"),
    "relationships": ("relationships", "relationship.schema.json", "relationship_id"),
    "causal_chains": ("causal",        "causal.schema.json",       "causal_id"),
    "contexts":      ("context",       "context.schema.json",     "context_id"),
    "mirrors":       ("mirror",        "mirror.schema.json",      "mirror_id"),
}


# --- report ---------------------------------------------------------------

@dataclass
class ValidationIssue:
    severity: str  # "error" | "warning"
    code: str
    message: str
    file: Optional[str] = None


@dataclass
class ValidationReport:
    issues: list = field(default_factory=list)

    def error(self, code: str, message: str, file: Optional[str] = None) -> None:
        self.issues.append(ValidationIssue("error", code, message, file))

    def warning(self, code: str, message: str, file: Optional[str] = None) -> None:
        self.issues.append(ValidationIssue("warning", code, message, file))

    @property
    def errors(self) -> list:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list:
        return [i for i in self.issues if i.severity == "warning"]

    def ok(self) -> bool:
        return not self.errors


# --- schema loading -------------------------------------------------------

def _build_registry(schema_dir: Path) -> Registry:
    """Load all schemas in schema_dir into a referencing.Registry so
    cross-schema $refs (e.g. evidence_ref.schema.json) can resolve."""
    resources = {}
    for path in schema_dir.glob("*.schema.json"):
        with open(path, encoding="utf-8") as f:
            contents = json.load(f)
        resource = Resource.from_contents(contents)
        uri = contents.get("$id", path.name)
        resources[uri] = resource
    return Registry().with_resources(resources.items())


def _load_schema(name: str) -> dict:
    with open(SCHEMA_DIR / name, encoding="utf-8") as f:
        return json.load(f)


def _validate_schema(obj: dict, schema: dict, registry: Registry,
                     filename: str, report: ValidationReport) -> None:
    try:
        jsonschema.validate(obj, schema, registry=registry)
    except jsonschema.ValidationError as e:
        # Trim the long schema path; keep just the message + path
        path = ".".join(str(p) for p in e.absolute_path) if e.absolute_path else "<root>"
        report.error(
            "schema_violation",
            f"{filename}: {e.message} (at {path})",
            filename,
        )


# --- core validation ------------------------------------------------------

def validate_package(book_dir: Path) -> ValidationReport:
    report = ValidationReport()
    registry = _build_registry(SCHEMA_DIR)

    # 1. Required files
    required = ["manifest.json", "book.json",
                "source_normalized.txt", "source_index.json"]
    for fname in required:
        if not (book_dir / fname).exists():
            report.error("missing_file", f"Required file missing: {fname}")
    if not report.ok():
        return report  # Cannot continue without base files

    # Load source layer
    source_text = (book_dir / "source_normalized.txt").read_text(encoding="utf-8")
    source_index = load_json(book_dir / "source_index.json")
    total_chars = source_index.get("total_chars", len(source_text))
    valid_chapter_ids = {c["chapter_id"] for c in source_index.get("chapters", [])}

    # Validate source_index basic shape
    if "chapters" not in source_index:
        report.error("source_index_malformed",
                     "source_index.json missing 'chapters' field",
                     "source_index.json")
    if "total_chars" not in source_index:
        report.warning("source_index_missing_total_chars",
                       "source_index.json missing 'total_chars'; using len(source_text)",
                       "source_index.json")

    # 2a. Validate manifest
    manifest = load_json(book_dir / "manifest.json")
    _validate_schema(manifest, _load_schema("manifest.schema.json"),
                    registry, "manifest.json", report)

    # 2b. Validate book
    book = load_json(book_dir / "book.json")
    _validate_schema(book, _load_schema("book.schema.json"),
                    registry, "book.json", report)

    # Cross-file: manifest.source_book_id == book.book_id
    if manifest.get("source_book_id") and book.get("book_id") and \
       manifest["source_book_id"] != book["book_id"]:
        report.error(
            "cross_file_inconsistency",
            f"manifest.source_book_id ({manifest['source_book_id']}) "
            f"!= book.book_id ({book['book_id']})",
        )

    # Load all object types
    all_objects: dict = {}  # type_name → {id: (filepath_str, obj)}
    for type_name, (subdir, schema_file, id_field) in OBJECT_TYPES.items():
        type_dir = book_dir / subdir
        objs = {}
        if not type_dir.exists():
            all_objects[type_name] = objs
            continue
        schema = _load_schema(schema_file)
        for fpath in sorted(type_dir.glob("*.json")):
            try:
                obj = load_json(fpath)
            except json.JSONDecodeError as e:
                report.error("json_parse_error",
                             f"{fpath.name}: {e}", str(fpath))
                continue
            _validate_schema(obj, schema, registry, fpath.name, report)
            obj_id = obj.get(id_field)
            if not obj_id:
                report.error("missing_id",
                             f"{fpath.name}: missing {id_field}", str(fpath))
                continue
            if obj_id in objs:
                report.error(
                    "duplicate_id",
                    f"{obj_id} appears in both {objs[obj_id][0]} and {fpath.name}",
                    str(fpath),
                )
                continue
            objs[obj_id] = (fpath.name, obj)
        all_objects[type_name] = objs

    # 4. manifest counts vs actual
    manifest_contents = manifest.get("contents", {})
    for type_name in OBJECT_TYPES:
        expected = manifest_contents.get(type_name, 0)
        actual = len(all_objects[type_name])
        if expected != actual:
            report.error(
                "manifest_count_mismatch",
                f"contents.{type_name}: manifest says {expected}, actual {actual}",
            )

    # Build ID sets for ref resolution
    evidence_ids = set(all_objects["evidence"].keys())
    entity_ids = set(all_objects["characters"].keys())
    event_ids = set(all_objects["events"].keys())
    causal_ids = set(all_objects["causal_chains"].keys())
    context_ids = set(all_objects["contexts"].keys())
    mirror_ids = set(all_objects["mirrors"].keys())
    relationship_ids = set(all_objects["relationships"].keys())

    # 5 + 9. evidence_ref resolution + min-1-evidence_ref check
    # Types that carry evidence_refs (per schemas): character, event,
    # relationship, causal, context. Topic and Mirror do not.
    EVIDENCE_REF_TYPES = ["characters", "events", "relationships",
                          "causal_chains", "contexts"]
    for type_name in EVIDENCE_REF_TYPES:
        for obj_id, (fname, obj) in all_objects[type_name].items():
            refs = obj.get("evidence_refs", [])
            if len(refs) < 1:
                report.error(
                    "missing_evidence_ref",
                    f"{fname}: knowledge object has 0 evidence_refs (≥1 required)",
                    fname,
                )
            for ref in refs:
                ref_id = ref.get("id") if isinstance(ref, dict) else ref
                if ref_id not in evidence_ids:
                    report.error(
                        "dangling_evidence_ref",
                        f"{fname}: evidence_ref {ref_id} does not resolve to evidence/",
                        fname,
                    )

    # 6. entity_id resolution
    for event_id, (fname, obj) in all_objects["events"].items():
        for part_id in obj.get("participants", []):
            if part_id not in entity_ids:
                report.error(
                    "dangling_entity_ref",
                    f"{fname}: participant {part_id} not in characters/",
                    fname,
                )
    for rel_id, (fname, obj) in all_objects["relationships"].items():
        if obj.get("source") and obj["source"] not in entity_ids:
            report.error(
                "dangling_entity_ref",
                f"{fname}: source {obj['source']} not in characters/",
                fname,
            )

    # 7. event_id resolution
    for rel_id, (fname, obj) in all_objects["relationships"].items():
        if obj.get("target") and obj["target"] not in event_ids:
            report.error(
                "dangling_event_ref",
                f"{fname}: target {obj['target']} not in events/",
                fname,
            )
    for cid, (fname, obj) in all_objects["causal_chains"].items():
        for side in ("cause", "effect"):
            ref = obj.get(side, {}).get("ref")
            if ref and ref not in event_ids:
                report.error(
                    "dangling_event_ref",
                    f"{fname}: {side}.ref {ref} not in events/",
                    fname,
                )

    # 8. mirror.basis_refs resolution (any Package object EXCEPT evidence —
    # evidence is one layer below; mirrors sit above it per the v1 spec).
    topic_ids = set(all_objects["topics"].keys())
    all_known_ids = (entity_ids | event_ids | causal_ids | context_ids |
                     mirror_ids | relationship_ids | topic_ids)
    for mid, (fname, obj) in all_objects["mirrors"].items():
        for basis_ref in obj.get("basis_refs", []):
            if basis_ref not in all_known_ids:
                report.error(
                    "dangling_basis_ref",
                    f"{fname}: basis_ref {basis_ref} not found in any object type",
                    fname,
                )

    # 10-12. Evidence source_location checks
    for eid, (fname, obj) in all_objects["evidence"].items():
        loc = obj.get("source_location", {})
        ch_id = loc.get("chapter_id")
        cs = loc.get("char_start")
        ce = loc.get("char_end")

        # 10. chapter_id exists
        if ch_id and valid_chapter_ids and ch_id not in valid_chapter_ids:
            report.error(
                "invalid_chapter_id",
                f"{fname}: source_location.chapter_id {ch_id} not in source_index.json",
                fname,
            )
            continue

        # 11. offsets in range
        if cs is None or ce is None:
            report.error(
                "missing_offsets",
                f"{fname}: source_location missing char_start/char_end",
                fname,
            )
            continue
        if cs < 0 or ce < 0 or cs >= ce or ce > total_chars:
            report.error(
                "offset_out_of_range",
                f"{fname}: char offsets [{cs},{ce}) outside [0,{total_chars}]",
                fname,
            )
            continue

        # 12. original_text matches slice
        expected = obj.get("original_text", "")
        actual = source_text[cs:ce]
        if actual != expected:
            report.error(
                "original_text_mismatch",
                f"{fname}: original_text does not match source_normalized.txt[{cs}:{ce}]",
                fname,
            )

    return report


# --- helpers --------------------------------------------------------------

def load_json(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# --- CLI ------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate a Book-Scribe Historical Knowledge Package v1."
    )
    parser.add_argument(
        "book_dir",
        help="Path to the book directory (containing manifest.json, book.json, source_normalized.txt, etc.)",
    )
    parser.add_argument("--verbose", action="store_true",
                        help="Print warnings in addition to errors.")
    args = parser.parse_args()

    book_dir = Path(args.book_dir).resolve()
    if not book_dir.exists():
        print(f"ERROR: book_dir not found: {book_dir}", file=sys.stderr)
        return 2

    print(f"Validating: {book_dir}\n")
    report = validate_package(book_dir)

    if not report.issues:
        print("OK — package is valid.")
        return 0

    print(f"Errors:   {len(report.errors)}")
    print(f"Warnings: {len(report.warnings)}\n")

    if report.errors:
        print("=== ERRORS ===")
        for i, issue in enumerate(report.errors, 1):
            loc = f"[{issue.file}] " if issue.file else ""
            print(f"{i:3}. {loc}{issue.code}: {issue.message}")

    if args.verbose and report.warnings:
        print("\n=== WARNINGS ===")
        for i, issue in enumerate(report.warnings, 1):
            loc = f"[{issue.file}] " if issue.file else ""
            print(f"{i:3}. {loc}{issue.code}: {issue.message}")

    return 1 if report.errors else 0


if __name__ == "__main__":
    sys.exit(main())
