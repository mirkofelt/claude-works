"""Telegram message rendering utilities."""
import re


def md_to_html(text: str) -> str:
    """Convert Markdown subset to Telegram HTML. Escapes & < > in text nodes.

    Also normalizes literal \\n and \\t escape sequences from LLM output.
    """
    text = text.replace('\\n', '\n').replace('\\t', '\t')

    def esc(s: str) -> str:
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    parts = re.split(r"```(?:[^\n`]*)\n?([\s\S]*?)```", text)
    result = []
    for i, part in enumerate(parts):
        if i % 2 == 1:
            result.append(f"<pre>{esc(part.strip())}</pre>")
        else:
            segs = re.split(r"`([^`\n]+)`", part)
            for j, seg in enumerate(segs):
                if j % 2 == 1:
                    result.append(f"<code>{esc(seg)}</code>")
                else:
                    s = esc(seg)
                    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s, flags=re.DOTALL)
                    result.append(s)
    return "".join(result)
