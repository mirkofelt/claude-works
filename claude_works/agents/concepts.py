from ..prompts import load as _load

USER_CONTEXT_TEMPLATE = "## User Context\nBackground: {background}"


def get_system_prompt() -> str:
    return _load("generalist")


def get_clarifying_questions() -> str:
    return _load("clarifying_questions")


def get_caveman_addendum() -> str:
    return _load("caveman")


def get_dev_standards() -> str:
    return _load("dev_standards")
