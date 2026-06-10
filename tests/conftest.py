import os
import sys
import pytest


@pytest.hookimpl(hookwrapper=True, trylast=True)
def pytest_sessionfinish(session, exitstatus):
    # Force-exit to avoid pytest hanging after async tests complete.
    # anyio/ASGITransport worker threads prevent normal process shutdown.
    yield  # let all other sessionfinish hooks (incl. terminal reporter) run first
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(int(exitstatus))
