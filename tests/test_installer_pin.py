"""The installer installs exactly the Protoverta release the package depends on."""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_installer_pins_the_dependency_version_and_a_hash():
    installer = (ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
    dependencies = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    pinned = next(item.split("==", 1)[1] for item in dependencies if item.startswith("protoverta=="))
    version = re.search(r"\$protovertaVersion = '([^']+)'", installer).group(1)
    digest = re.search(r"\$protovertaSha256 = '([^']+)'", installer).group(1)
    assert version == pinned
    # A released installer carries the SHA-256 of the published wheel; an
    # unreleased build refuses to download anything unverified.
    # PENDING-RELEASE until the matching Protoverta release exists; the release workflow refuses it.
    assert re.fullmatch(r"[0-9a-f]{64}", digest) or digest == "PENDING-RELEASE"


def test_remote_install_downloads_the_release_tag_of_this_version():
    installer = (ROOT / "scripts" / "install.ps1").read_text(encoding="utf-8")
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert re.search(r"\$adapterVersion = '([^']+)'", installer).group(1) == project["version"]
    assert "archive/refs/tags/v$adapterVersion.zip" in installer
