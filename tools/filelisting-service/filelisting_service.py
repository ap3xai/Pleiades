#!/usr/bin/env python3
"""Pre-ingestion discovery: append newly seen Drive files into source_entities lists."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
LOCATIONS_FILE = REPO_ROOT / "Locations.gitignore"
INGESTION_ROOT = REPO_ROOT / "ingestion" / "source_entities"

ENTITY_KINDS = ("spreadsheets", "documents", "mediaLinks")

SPREADSHEET_EXTS = {".gsheet", ".csv", ".xlsx", ".xls"}
MEDIA_EXTS = {
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".heic",
    ".arw",
    ".gif",
    ".bmp",
    ".tif",
    ".tiff",
    ".mp4",
    ".mov",
    ".m4a",
    ".mp3",
    ".wav",
    ".aac",
    ".avi",
    ".mkv",
    ".webm",
}

SKIP_NAMES = {"desktop.ini"}
LOCATIONS_DRIVE_RE = re.compile(r'WDS Drive:\s*"(?P<path>.+)"')


def parse_drive_root(locations_path: Path = LOCATIONS_FILE) -> Path:
    text = locations_path.read_text(encoding="utf-8")
    match = LOCATIONS_DRIVE_RE.search(text)
    if not match:
        raise SystemExit(f"Could not find WDS Drive path in {locations_path}")
    root = Path(match.group("path"))
    if not root.exists():
        raise SystemExit(f"WDS Drive path does not exist: {root}")
    return root


def list_path_for(kind: str) -> Path:
    return INGESTION_ROOT / kind / "list"


def ensure_layout() -> None:
    for kind in ENTITY_KINDS:
        directory = INGESTION_ROOT / kind
        directory.mkdir(parents=True, exist_ok=True)
        path = list_path_for(kind)
        if not path.exists():
            path.write_text("", encoding="utf-8")


def load_known_paths() -> set[str]:
    known: set[str] = set()
    for kind in ENTITY_KINDS:
        path = list_path_for(kind)
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            entry = line.strip()
            if entry:
                known.add(entry)
    return known


def classify(ext: str) -> str:
    lowered = ext.lower()
    if lowered in SPREADSHEET_EXTS:
        return "spreadsheets"
    if lowered in MEDIA_EXTS:
        return "mediaLinks"
    return "documents"


def scan_drive(drive_root: Path) -> list[tuple[str, str]]:
    """Return (relative_path, kind) for every file under the Drive root."""
    found: list[tuple[str, str]] = []
    for path in drive_root.rglob("*"):
        if not path.is_file():
            continue
        if path.name in SKIP_NAMES:
            continue
        try:
            relative = path.relative_to(drive_root).as_posix()
        except ValueError:
            continue
        found.append((relative, classify(path.suffix)))
    found.sort(key=lambda item: item[0].lower())
    return found


def append_entries(entries_by_kind: dict[str, list[str]], dry_run: bool) -> int:
    added = 0
    for kind, entries in entries_by_kind.items():
        if not entries:
            continue
        path = list_path_for(kind)
        if dry_run:
            for entry in entries:
                print(f"[dry-run] {kind}: {entry}")
            added += len(entries)
            continue
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            for entry in entries:
                handle.write(entry + "\n")
                added += 1
    return added


def run(dry_run: bool = False) -> int:
    ensure_layout()
    drive_root = parse_drive_root()
    known = load_known_paths()
    scanned = scan_drive(drive_root)

    new_by_kind: dict[str, list[str]] = {kind: [] for kind in ENTITY_KINDS}
    for relative, kind in scanned:
        if relative in known:
            continue
        new_by_kind[kind].append(relative)
        known.add(relative)

    new_count = sum(len(v) for v in new_by_kind.values())
    appended = append_entries(new_by_kind, dry_run=dry_run)

    print(f"drive_root: {drive_root}")
    print(f"scanned: {len(scanned)}")
    print(f"already_known: {len(scanned) - new_count}")
    print(f"new: {new_count}")
    for kind in ENTITY_KINDS:
        print(f"  {kind}: +{len(new_by_kind[kind])}")
    if dry_run:
        print("dry-run: no files written")
    else:
        print(f"appended: {appended}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Discover new WDS Drive files and append them to ingestion source_entities lists."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be appended without writing.",
    )
    args = parser.parse_args(argv)
    return run(dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
