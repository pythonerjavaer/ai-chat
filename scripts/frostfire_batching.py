"""Transport sizing is independent of the number of monitoring updates.

Large histories continue in further bounded input pages; there is no per-run
quota. These bounds protect one process/input and one HTTP request only.
"""

DEFAULT_BATCH_SIZE = 25
MAX_BATCH_SIZE = 100
MAX_INPUT_ROWS = 10_000


def validate_batch_size(value: int) -> int:
    if type(value) is not int or not 1 <= value <= MAX_BATCH_SIZE:
        raise ValueError(f"batch size must be between 1 and {MAX_BATCH_SIZE}")
    return value
