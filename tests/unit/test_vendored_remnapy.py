from __future__ import annotations

import hashlib
from email.parser import BytesParser
from pathlib import Path
from zipfile import ZipFile

WHEEL_NAME = "remnapy-2.7.1.dev7+cleanpay.g06802538-py3-none-any.whl"
WHEEL_SHA256 = "f293db6ad658b948cef792fe0afb9b4c06fdc4a2d533038f45897b24baa01e61"
WHEEL_PATH = Path("vendor/remnapy") / WHEEL_NAME
SOURCE_COMMIT = "06802538e9f7671d4387c597b5f1434557ca3dc9"


def test_vendored_remnapy_wheel_is_the_reviewed_artifact() -> None:
    wheel = WHEEL_PATH.read_bytes()

    assert hashlib.sha256(wheel).hexdigest() == WHEEL_SHA256

    with ZipFile(WHEEL_PATH) as archive:
        names = archive.namelist()
        metadata_path = next(name for name in names if name.endswith(".dist-info/METADATA"))
        metadata = BytesParser().parsebytes(archive.read(metadata_path))

    assert len(names) == 91
    assert metadata["Name"] == "remnapy"
    assert metadata["Version"] == "2.7.1.dev7+cleanpay.g06802538"
    assert "cryptography<51.0.0,>=50.0.0" in metadata.get_all("Requires-Dist", [])


def test_vendored_remnapy_provenance_and_license_are_committed() -> None:
    provenance = Path("vendor/remnapy/PROVENANCE.md").read_text(encoding="utf-8")
    license_text = Path("vendor/remnapy/LICENSE").read_text(encoding="utf-8")

    assert SOURCE_COMMIT in provenance
    assert WHEEL_SHA256 in provenance
    assert "setuptools==84.0.0" in provenance
    assert "SOURCE_DATE_EPOCH=1782840709" in provenance
    assert "MIT License" in license_text
    assert "Copyright (c) 2025 sm1ky" in license_text


def test_dependency_manifests_and_containers_use_the_vendored_wheel() -> None:
    project = Path("pyproject.toml").read_text(encoding="utf-8")
    lock = Path("uv.lock").read_text(encoding="utf-8")

    assert '"remnapy==2.7.1.dev7+cleanpay.g06802538"' in project
    assert f'remnapy = {{ path = "vendor/remnapy/{WHEEL_NAME}" }}' in project
    assert "override-dependencies" not in project
    assert "https://github.com/snoups/remnapy" not in project
    assert f'filename = "{WHEEL_NAME}", hash = "sha256:{WHEEL_SHA256}"' in lock
    assert "https://github.com/snoups/remnapy" not in lock

    copy_instruction = f"COPY vendor/remnapy/{WHEEL_NAME} ./vendor/remnapy/"
    for name in ("Dockerfile", "Dockerfile.local"):
        dockerfile = Path(name).read_text(encoding="utf-8")
        assert copy_instruction in dockerfile
        assert dockerfile.index(copy_instruction) < dockerfile.index("uv sync")
