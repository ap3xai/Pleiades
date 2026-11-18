#!/usr/bin/env python3
"""Export Google-native Drive files to standard formats under GDrive/exports."""

from __future__ import annotations

import argparse
import io
import re
import shutil
import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

REPO_ROOT = Path(__file__).resolve().parents[2]
LOCATIONS_FILE = REPO_ROOT / "Locations.gitignore"
CREDENTIALS_DIR = REPO_ROOT / "credentials"
OAUTH_CLIENT = CREDENTIALS_DIR / "oauth_client.json"
TOKEN_PATH = CREDENTIALS_DIR / "token.json"
EXPORTS_ROOT = REPO_ROOT / "GDrive" / "exports"
INGESTION_ROOT = REPO_ROOT / "ingestion" / "source_entities"

SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
LOCATIONS_DRIVE_RE = re.compile(r'WDS Drive:\s*"(?P<path>.+)"')
SHORTCUT_ID_RE = re.compile(r"shortcut-targets-by-id[\\/](?P<id>[^\\/]+)")

GOOGLE_EXPORT = {
    ".gsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    ".gdoc": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".docx",
    ),
    ".gslides": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".pptx",
    ),
}

COPY_EXTS = {
    ".csv",
    ".xlsx",
    ".xls",
    ".docx",
    ".doc",
    ".pptx",
    ".ppt",
    ".pdf",
    ".txt",
    ".md",
    ".json",
    ".js",
}

SKIP_EXTS = {".gform"}

LIST_SOURCES = ("spreadsheets", "documents")


def parse_locations() -> tuple[Path, str]:
    text = LOCATIONS_FILE.read_text(encoding="utf-8")
    match = LOCATIONS_DRIVE_RE.search(text)
    if not match:
        raise SystemExit(f"Could not find WDS Drive path in {LOCATIONS_FILE}")
    drive_root = Path(match.group("path"))
    id_match = SHORTCUT_ID_RE.search(str(drive_root))
    if not id_match:
        raise SystemExit(f"Could not parse Drive folder id from path: {drive_root}")
    return drive_root, id_match.group("id")


def get_drive_service():
    if not OAUTH_CLIENT.exists():
        raise SystemExit(
            "Missing credentials/oauth_client.json\n"
            "Create a Google Cloud Desktop OAuth client, enable Drive API, "
            "and save the downloaded JSON as credentials/oauth_client.json"
        )

    CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(OAUTH_CLIENT), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_PATH.write_text(creds.to_json(), encoding="utf-8")

    return build("drive", "v3", credentials=creds, cache_discovery=False)


def read_list_paths(only: str | None = None) -> list[str]:
    paths: list[str] = []
    for kind in LIST_SOURCES:
        list_file = INGESTION_ROOT / kind / "list"
        if not list_file.exists():
            continue
        for line in list_file.read_text(encoding="utf-8").splitlines():
            entry = line.strip()
            if entry:
                paths.append(entry)
    # de-dupe, preserve order
    seen: set[str] = set()
    unique: list[str] = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    if only is not None:
        only_norm = only.replace("\\", "/").strip()
        unique = [p for p in unique if p == only_norm]
        if not unique:
            raise SystemExit(f"--only path not found in ingestion lists: {only_norm}")
    return unique


def classify(relative_path: str) -> str:
    ext = Path(relative_path).suffix.lower()
    if ext in SKIP_EXTS:
        return "skip"
    if ext in GOOGLE_EXPORT:
        return "export"
    if ext in COPY_EXTS:
        return "copy"
    # extensionless / unknown docs from documents list: try copy if local file readable
    return "copy"


def output_relative(relative_path: str, action: str) -> str:
    path = Path(relative_path)
    if action == "export":
        _mime, new_ext = GOOGLE_EXPORT[path.suffix.lower()]
        return path.with_suffix(new_ext).as_posix()
    return path.as_posix()


def list_children(service, parent_id: str) -> list[dict]:
    files: list[dict] = []
    page_token = None
    query = f"'{parent_id}' in parents and trashed = false"
    while True:
        response = (
            service.files()
            .list(
                q=query,
                spaces="drive",
                fields="nextPageToken, files(id, name, mimeType, shortcutDetails)",
                pageToken=page_token,
                pageSize=1000,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        files.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return files


def resolve_shortcut(service, file_meta: dict) -> dict:
    if file_meta.get("mimeType") != "application/vnd.google-apps.shortcut":
        return file_meta
    target_id = (file_meta.get("shortcutDetails") or {}).get("targetId")
    if not target_id:
        return file_meta
    return (
        service.files()
        .get(
            fileId=target_id,
            fields="id, name, mimeType, shortcutDetails",
            supportsAllDrives=True,
        )
        .execute()
    )


class PathResolver:
    def __init__(self, service, root_id: str):
        self.service = service
        self.root_id = root_id
        self._cache: dict[tuple[str, str], dict | None] = {}

    def _match_child(self, parent_id: str, segment: str) -> dict | None:
        key = (parent_id, segment.lower())
        if key in self._cache:
            return self._cache[key]

        children = list_children(self.service, parent_id)
        candidates = [segment]
        suffix = Path(segment).suffix.lower()
        if suffix in GOOGLE_EXPORT or suffix in SKIP_EXTS:
            candidates.append(Path(segment).stem)

        found = None
        lowered = {c.lower() for c in candidates}
        for child in children:
            if child.get("name", "").lower() in lowered:
                found = resolve_shortcut(self.service, child)
                break

        self._cache[key] = found
        return found

    def resolve(self, relative_path: str) -> dict:
        parent_id = self.root_id
        parts = Path(relative_path).parts
        current = None
        for segment in parts:
            current = self._match_child(parent_id, segment)
            if current is None:
                raise FileNotFoundError(
                    f"Could not resolve '{segment}' while walking '{relative_path}'"
                )
            parent_id = current["id"]
        if current is None:
            raise FileNotFoundError(f"Empty path: {relative_path}")
        return current


def export_google_file(service, file_id: str, mime: str, dest: Path) -> None:
    request = service.files().export_media(fileId=file_id, mimeType=mime)
    dest.parent.mkdir(parents=True, exist_ok=True)
    buffer = io.BytesIO()
    downloader = MediaIoBaseDownload(buffer, request)
    done = False
    while not done:
        _status, done = downloader.next_chunk()
    dest.write_bytes(buffer.getvalue())


def copy_local_file(drive_root: Path, relative_path: str, dest: Path) -> None:
    source = drive_root / Path(relative_path)
    if not source.exists():
        raise FileNotFoundError(f"Local Drive file missing: {source}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(source, dest)
    except OSError as exc:
        raise OSError(f"Could not copy local file (may be a cloud stub): {source}") from exc
    if dest.stat().st_size == 0:
        dest.unlink(missing_ok=True)
        raise OSError(f"Copied file was empty: {source}")


def process_one(
    service,
    resolver: PathResolver,
    drive_root: Path,
    relative_path: str,
    dry_run: bool,
) -> str:
    action = classify(relative_path)
    if action == "skip":
        return "skipped"

    out_rel = output_relative(relative_path, action)
    dest = EXPORTS_ROOT / out_rel
    if dest.exists() and dest.stat().st_size > 0:
        return "exists"

    if dry_run:
        print(f"[dry-run] {action}: {relative_path} -> {out_rel}")
        return action

    if action == "export":
        meta = resolver.resolve(relative_path)
        mime, _ext = GOOGLE_EXPORT[Path(relative_path).suffix.lower()]
        export_google_file(service, meta["id"], mime, dest)
        print(f"exported: {relative_path} -> {out_rel}")
        return "exported"

    copy_local_file(drive_root, relative_path, dest)
    print(f"copied: {relative_path} -> {out_rel}")
    return "copied"


def run(dry_run: bool = False, only: str | None = None) -> int:
    drive_root, root_id = parse_locations()
    paths = read_list_paths(only=only)
    EXPORTS_ROOT.mkdir(parents=True, exist_ok=True)

    needs_api = any(classify(p) == "export" for p in paths)
    service = None
    resolver = None
    if needs_api and not dry_run:
        service = get_drive_service()
        resolver = PathResolver(service, root_id)
    elif needs_api and dry_run:
        # dry-run can still mention exports without auth
        pass
    elif any(classify(p) == "export" for p in paths) is False:
        pass

    # If we only have copies/skips, API optional; for export actions in non-dry-run we need service
    if not dry_run and needs_api and service is None:
        service = get_drive_service()
        resolver = PathResolver(service, root_id)

    counts = {
        "exported": 0,
        "copied": 0,
        "skipped": 0,
        "exists": 0,
        "failed": 0,
        "export": 0,  # dry-run planned exports
        "copy": 0,  # dry-run planned copies
    }

    for relative_path in paths:
        action = classify(relative_path)
        try:
            if action == "export" and dry_run:
                out_rel = output_relative(relative_path, action)
                dest = EXPORTS_ROOT / out_rel
                if dest.exists() and dest.stat().st_size > 0:
                    print(f"[dry-run] exists: {out_rel}")
                    counts["exists"] += 1
                else:
                    print(f"[dry-run] export: {relative_path} -> {out_rel}")
                    counts["export"] += 1
                continue
            if action == "copy" and dry_run:
                out_rel = output_relative(relative_path, action)
                dest = EXPORTS_ROOT / out_rel
                if dest.exists() and dest.stat().st_size > 0:
                    print(f"[dry-run] exists: {out_rel}")
                    counts["exists"] += 1
                else:
                    print(f"[dry-run] copy: {relative_path} -> {out_rel}")
                    counts["copy"] += 1
                continue
            if action == "skip":
                print(f"skipped: {relative_path}")
                counts["skipped"] += 1
                continue

            if action == "export" and resolver is None:
                raise RuntimeError("Drive API client required for Google-native export")

            result = process_one(service, resolver, drive_root, relative_path, dry_run=False)
            counts[result] = counts.get(result, 0) + 1
        except (OSError, FileNotFoundError, HttpError, RuntimeError) as exc:
            print(f"failed: {relative_path} ({exc})", file=sys.stderr)
            counts["failed"] += 1

    print("---")
    print(f"drive_root: {drive_root}")
    print(f"root_id: {root_id}")
    print(f"considered: {len(paths)}")
    for key in ("exported", "copied", "skipped", "exists", "failed", "export", "copy"):
        if counts.get(key):
            print(f"{key}: {counts[key]}")
    return 1 if counts["failed"] else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export Google-native Drive files to GDrive/exports as standard formats."
    )
    parser.add_argument("--dry-run", action="store_true", help="Plan actions without writing.")
    parser.add_argument(
        "--only",
        help="Process a single relative path from the ingestion lists.",
    )
    args = parser.parse_args(argv)
    return run(dry_run=args.dry_run, only=args.only)


if __name__ == "__main__":
    sys.exit(main())
