from ..prompts import load as _load

CLARIFYING_QUESTIONS_ADDENDUM = _load("clarifying_questions")
SYSTEM_PROMPT = _load("generalist")
USER_CONTEXT_TEMPLATE = "## User Context\nBackground: {background}"
_DEV_STANDARDS_ADDENDUM = _load("dev_standards")
CAVEMAN_ADDENDUM = _load("caveman")
