from pathlib import Path

_DIR = Path(__file__).parent


def load(name: str) -> str:
    """Load prompt text from a .md file in the prompts directory."""
    return (_DIR / f"{name}.md").read_text(encoding="utf-8").strip()
