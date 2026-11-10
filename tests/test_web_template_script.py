"""The setup page's inline script must parse; a syntax error disables the whole page."""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

TEMPLATE = Path(__file__).resolve().parents[1] / "src" / "claudapter" / "web_template.html"


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")
def test_setup_page_script_parses(tmp_path):
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", TEMPLATE.read_text(encoding="utf-8"), re.DOTALL)
    assert scripts
    source = tmp_path / "setup.js"
    source.write_text("\n;\n".join(scripts), encoding="utf-8")
    result = subprocess.run([shutil.which("node"), "--check", str(source)], capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr
