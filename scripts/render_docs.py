#!/usr/bin/env python3
"""Render docs/*.md to docs/*.html using the shared site template.

Run: python scripts/render_docs.py
Also called by CI on push to docs/** or scripts/render_docs.py.
"""
import re
import sys
from pathlib import Path

DOCS = Path("docs")

# Nav entries: (label, filename) — add new docs here
NAV_ENTRIES = [
    ("Home", "index.html"),
    ("Setup", "setup.html"),
    ("Architecture", "architecture.html"),
    ("Requirements", "requirements.html"),
    ("Dev Mode", "dev-mode.html"),
]
GITHUB_URL = "https://github.com/mirkofelt/claude-works"

CSS = """\
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
:root { --bg: #0d1117; --surface: #161b22; --border: #21262d; --text: #e6edf3; --muted: #8b949e; --accent: #58a6ff; --accent2: #79c0ff; }
body { background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif; line-height: 1.7; }
a { color: var(--accent); text-decoration: none; } a:hover { text-decoration: underline; }
header { border-bottom: 1px solid var(--border); padding: 1rem 2rem; display: flex; align-items: center; gap: .75rem; }
header nav { margin-left: auto; display: flex; gap: 1.5rem; font-size: .9rem; }
header nav a { color: var(--muted); } header nav a:hover { color: var(--text); text-decoration: none; }
header nav a.active { color: var(--text); font-weight: 600; }
header nav a.ext { color: var(--accent); }
.content { max-width: 860px; margin: 0 auto; padding: 3rem 2rem 5rem; }
h1 { font-size: 2rem; font-weight: 700; margin-bottom: 1.5rem; letter-spacing: -.02em; }
h2 { font-size: 1.35rem; font-weight: 600; margin: 2.5rem 0 .75rem; padding-bottom: .4rem; border-bottom: 1px solid var(--border); }
h3 { font-size: 1.05rem; font-weight: 600; margin: 1.5rem 0 .5rem; color: var(--accent2); }
h4 { font-size: .95rem; font-weight: 600; margin: 1.2rem 0 .4rem; color: var(--muted); }
p { margin-bottom: .75rem; } ul, ol { margin: .5rem 0 .75rem 1.5rem; } li { margin-bottom: .25rem; }
code { background: var(--surface); border: 1px solid var(--border); border-radius: 4px; padding: .1em .4em; font-size: .875em; font-family: "SF Mono", Consolas, monospace; }
pre { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 1.25rem 1.5rem; overflow-x: auto; margin: 1rem 0; }
pre code { background: none; border: none; padding: 0; font-size: .85rem; }
table { width: 100%; border-collapse: collapse; margin: 1rem 0; font-size: .9rem; }
th { background: var(--surface); padding: .6rem 1rem; text-align: left; border: 1px solid var(--border); font-weight: 600; color: var(--muted); }
td { padding: .55rem 1rem; border: 1px solid var(--border); }
tr:nth-child(even) td { background: rgba(255,255,255,.02); }
hr { border: none; border-top: 1px solid var(--border); margin: 2rem 0; }
footer { border-top: 1px solid var(--border); padding: 1.5rem 2rem; text-align: center; font-size: .85rem; color: var(--muted); }
.badge { display:inline-block; background:var(--surface); border:1px solid var(--border); border-radius:4px; padding:.1em .5em; font-size:.8em; font-family:monospace; }"""


def _nav_html(active_file: str) -> str:
    parts = []
    for label, fname in NAV_ENTRIES:
        cls = ' class="active"' if fname == active_file else ""
        parts.append(f'    <a href="{fname}"{cls}>{label}</a>')
    parts.append(f'    <a href="{GITHUB_URL}" target="_blank" rel="noopener" class="ext">GitHub ↗</a>')
    return "<nav>\n" + "\n".join(parts) + "\n  </nav>"


def _md_to_html_body(md: str) -> str:
    """Minimal Markdown → HTML converter for headings, code blocks, bold, links, lists, tables."""
    lines = md.split("\n")
    out = []
    in_code = False
    in_table = False
    code_lang = ""

    def flush_table():
        nonlocal in_table
        if in_table:
            out.append("</tbody></table>")
            in_table = False

    for line in lines:
        # fenced code block
        if line.startswith("```"):
            if in_code:
                out.append("</code></pre>")
                in_code = False
            else:
                flush_table()
                code_lang = line[3:].strip()
                out.append(f'<pre><code class="language-{code_lang}">' if code_lang else "<pre><code>")
                in_code = True
            continue
        if in_code:
            out.append(line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
            continue

        # table rows
        if "|" in line and line.strip().startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(re.match(r"^[-:]+$", c) for c in cells):
                continue  # skip separator
            if not in_table:
                out.append("<table><thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in cells) + "</tr></thead><tbody>")
                in_table = True
            else:
                out.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in cells) + "</tr>")
            continue
        flush_table()

        # headings
        m = re.match(r"^(#{1,4})\s+(.*)", line)
        if m:
            lvl = len(m.group(1))
            out.append(f"<h{lvl}>{_inline(m.group(2))}</h{lvl}>")
            continue

        # hr
        if re.match(r"^---+$", line.strip()):
            out.append("<hr>")
            continue

        # list items
        m = re.match(r"^(\s*)[*\-]\s+(.*)", line)
        if m:
            out.append(f"<li>{_inline(m.group(2))}</li>")
            continue
        m = re.match(r"^\d+\.\s+(.*)", line)
        if m:
            out.append(f"<li>{_inline(m.group(1))}</li>")
            continue

        # blank line
        if not line.strip():
            out.append("")
            continue

        out.append(f"<p>{_inline(line)}</p>")

    flush_table()

    # wrap adjacent <li> in <ul>
    result = []
    i = 0
    while i < len(out):
        if out[i].startswith("<li>"):
            result.append("<ul>")
            while i < len(out) and out[i].startswith("<li>"):
                result.append(out[i])
                i += 1
            result.append("</ul>")
        else:
            result.append(out[i])
            i += 1
    return "\n".join(result)


def _inline(text: str) -> str:
    """Convert inline markdown: bold, code, links."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r"`([^`]+)`", r'<code>\1</code>', text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"\*([^*]+)\*", r"<em>\1</em>", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    return text


def render_md(md_path: Path) -> None:
    html_path = md_path.with_suffix(".html")
    md = md_path.read_text(encoding="utf-8")
    # extract title from first H1
    m = re.search(r"^#\s+(.+)", md, re.MULTILINE)
    title = m.group(1) if m else md_path.stem.replace("-", " ").title()
    body = _md_to_html_body(md)
    nav = _nav_html(html_path.name)
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title} — Claude Works</title>
  <link rel="icon" type="image/png" href="icon.png">
  <style>
{CSS}
  </style>
</head>
<body>

<header>
  <a href="index.html" style="display:flex;align-items:center;gap:.75rem;text-decoration:none;">
    <img src="icon.png" alt="" style="width:32px;height:32px;border-radius:50%;">
    <span style="font-weight:600;font-size:1.1rem;color:#e6edf3;">Claude Works</span>
  </a>
  {nav}
</header>

<div class="content">
{body}
</div>

<footer>
  <a href="{GITHUB_URL}" style="color:var(--muted)">claude-works</a>
</footer>
</body>
</html>"""
    html_path.write_text(html, encoding="utf-8")
    print(f"  rendered: {html_path.name}")


def update_existing_nav(html_path: Path) -> None:
    """Patch nav in existing hand-crafted HTML files."""
    content = html_path.read_text(encoding="utf-8")
    new_nav = _nav_html(html_path.name)
    # replace the <nav>...</nav> block inside <header>
    updated = re.sub(r"<nav>[\s\S]*?</nav>", new_nav, content, count=1)
    if updated != content:
        html_path.write_text(updated, encoding="utf-8")
        print(f"  nav updated: {html_path.name}")


if __name__ == "__main__":
    md_files = sorted(DOCS.glob("*.md"))
    print(f"Rendering {len(md_files)} markdown file(s)…")
    for md in md_files:
        render_md(md)

    # patch nav in all existing HTML files (including hand-crafted ones)
    for html in sorted(DOCS.glob("*.html")):
        if html.name != "index.html":  # index has custom structure, skip nav-only update
            update_existing_nav(html)

    print("Done.")
