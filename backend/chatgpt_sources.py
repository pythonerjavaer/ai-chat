"""Logical monitoring slots only; private source mappings stay on the host."""

ACTIVE_CHATGPT_SOURCE_IDS = tuple(
    f"chatgpt-radar-{index:02d}" for index in (1, 2, 3, 6, 7, 8, 9)
)
# Retired slots remain recognizable for historical provenance and redaction.
KNOWN_CHATGPT_SOURCE_IDS = frozenset(
    f"chatgpt-radar-{index:02d}" for index in range(1, 10)
)
