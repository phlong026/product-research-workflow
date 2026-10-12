from __future__ import annotations

import hashlib
import json
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class ArchiveEntry:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class ArchiveManifest:
    archive_name: str
    archive_size: int
    archive_sha256: str
    entry_count: int
    entries: tuple[ArchiveEntry, ...]

    def to_dict(self) -> dict:
        return asdict(self)


def _sha256_stream(stream, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    while chunk := stream.read(chunk_size):
        digest.update(chunk)
    return digest.hexdigest()


def _sha256_file(path: Path) -> str:
    with path.open("rb") as handle:
        return _sha256_stream(handle)


def build_archive_manifest(archive_path: str | Path) -> ArchiveManifest:
    archive = Path(archive_path)
    entries: list[ArchiveEntry] = []
    with zipfile.ZipFile(archive) as zf:
        for info in sorted(zf.infolist(), key=lambda item: item.filename):
            if info.is_dir():
                continue
            with zf.open(info, "r") as stream:
                entries.append(
                    ArchiveEntry(
                        path=info.filename,
                        size=info.file_size,
                        sha256=_sha256_stream(stream),
                    )
                )
    return ArchiveManifest(
        archive_name=archive.name,
        archive_size=archive.stat().st_size,
        archive_sha256=_sha256_file(archive),
        entry_count=len(entries),
        entries=tuple(entries),
    )


def write_archive_manifest(manifest: ArchiveManifest, output_path: str | Path) -> None:
    Path(output_path).write_text(
        json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def verify_archive_manifest(archive_path: str | Path, manifest_path: str | Path) -> list[str]:
    expected = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    actual = build_archive_manifest(archive_path).to_dict()
    errors: list[str] = []
    for key in ("archive_size", "archive_sha256", "entry_count"):
        if actual[key] != expected[key]:
            errors.append(f"{key}: expected={expected[key]!r}, actual={actual[key]!r}")

    expected_entries = {item["path"]: item for item in expected["entries"]}
    actual_entries = {item["path"]: item for item in actual["entries"]}
    if expected_entries.keys() != actual_entries.keys():
        missing = sorted(expected_entries.keys() - actual_entries.keys())
        extra = sorted(actual_entries.keys() - expected_entries.keys())
        if missing:
            errors.append(f"missing entries: {missing}")
        if extra:
            errors.append(f"unexpected entries: {extra}")

    for path in sorted(expected_entries.keys() & actual_entries.keys()):
        for key in ("size", "sha256"):
            if expected_entries[path][key] != actual_entries[path][key]:
                errors.append(
                    f"{path} {key}: expected={expected_entries[path][key]!r}, "
                    f"actual={actual_entries[path][key]!r}"
                )
    return errors
