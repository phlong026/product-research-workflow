#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from research_workflow.golden_case.manifest import build_archive_manifest, write_archive_manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Freeze a Golden Case ZIP into a SHA256 manifest.")
    parser.add_argument("archive", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    manifest = build_archive_manifest(args.archive)
    write_archive_manifest(manifest, args.output)
    print(f"Wrote {manifest.entry_count} entries to {args.output}")


if __name__ == "__main__":
    main()
