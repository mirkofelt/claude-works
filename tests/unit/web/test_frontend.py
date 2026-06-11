import json
import re
import subprocess
from pathlib import Path

import pytest

HTML = Path("claude_works/web/static/index.html")

_CRITICAL_FUNCTIONS = [
    "doLogin",
    "showTab",
    "loadTab",
    "loadTokens",
    "loadSettings",
    "saveSettings",
    "sendAdminChat",
    "loadUplink",
    "setMode",
    "_renderModeBar",
    "handleUplinkKey",
    "drawTokenChart",
    "drawCostChart",
    "drawUsageChart",
]

_CRITICAL_ELEMENTS = [
    'id="authOverlay"',
    'id="tokenInput"',
    'id="tab-tokens"',
    'id="tab-chat"',
    'id="tab-settings"',
    'id="chatHistory"',
    'id="chatInput"',
    'id="modeBtn-run"',
    'id="modeBtn-repair"',
    'id="limitGaugeCard"',
    'id="costChartCard"',
    'id="usageChartCard"',
    'id="s_dev_mode"',
    'id="deployGuardSection"',
]


def _js_blocks(html: str) -> list[str]:
    return re.findall(r"<script>([\s\S]*?)</script>", html)


@pytest.fixture(scope="module")
def html_content() -> str:
    return HTML.read_text(encoding="utf-8")


def test_html_file_exists():
    assert HTML.exists(), f"{HTML} not found"


def test_js_syntax(html_content):
    """All <script> blocks must parse without syntax errors."""
    blocks = _js_blocks(html_content)
    assert blocks, "No <script> blocks found in index.html"

    node = subprocess.run(["node", "--version"], capture_output=True)
    if node.returncode != 0:
        pytest.skip("node not available")

    for i, js in enumerate(blocks):
        check = subprocess.run(
            ["node", "-e", f"new Function({json.dumps(js)})"],
            capture_output=True,
            text=True,
        )
        assert check.returncode == 0, (
            f"Syntax error in <script> block {i}:\n{check.stderr.strip()}"
        )


def test_critical_functions_defined(html_content):
    """Key JS functions must be present — catches accidental deletions."""
    for fn in _CRITICAL_FUNCTIONS:
        pattern = rf"(?:async\s+)?function\s+{re.escape(fn)}\s*\("
        assert re.search(pattern, html_content), f"Missing JS function: {fn}()"


def test_critical_elements_present(html_content):
    """Key DOM element IDs must be present — catches template regressions."""
    for el in _CRITICAL_ELEMENTS:
        assert el in html_content, f"Missing DOM element: {el}"


def test_no_incomplete_ternaries(html_content):
    """Detect `x ? 'val';` style incomplete ternaries in JS blocks."""
    for block in _js_blocks(html_content):
        for lineno, line in enumerate(block.splitlines(), 1):
            # Match: `? 'something';` or `? "something";` without a `:` else branch on the same line
            if re.search(r"\?\s*'[^']*'\s*;", line) or re.search(r'\?\s*"[^"]*"\s*;', line):
                if " : " not in line and "? '" not in line.replace(line, ""):
                    # Heuristic — only flag if no colon appears after the `?`
                    after_q = line[line.index("?") + 1 :]
                    if ":" not in after_q:
                        pytest.fail(
                            f"Likely incomplete ternary at JS line {lineno}: {line.strip()}"
                        )
