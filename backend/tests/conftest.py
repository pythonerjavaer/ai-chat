"""Keep process-local abuse protection independent between test cases."""

from collections import deque
import sys

import pytest


@pytest.fixture(autouse=True)
def isolated_registration_window():
    # Each fixture database is a separate test application. Requests from an
    # earlier test must not exhaust this application's registration window.
    # Retain the real limiter: repeated requests within a test still get 429.
    main = sys.modules.get("backend.main")
    if main is None:
        yield
        return
    previous = main._registration_requests
    main._registration_requests = deque()
    try:
        yield
    finally:
        main._registration_requests = previous
