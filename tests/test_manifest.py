import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "fixtures/golden_cases/ice_maker/archive_manifest.json"


def test_manifest_is_frozen_and_non_empty():
    payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    assert payload["entry_count"] >= 40
    assert len(payload["archive_sha256"]) == 64
    assert payload["archive_size"] > 100_000_000
    assert len(payload["entries"]) == payload["entry_count"]
