"""Logical monitoring labels only; private conversation mappings stay local."""

ACTIVE_CHATGPT_SOURCE_IDS = (
    "chatgpt-radar-01",
    "chatgpt-radar-02",
    "chatgpt-radar-03",
    "chatgpt-radar-06",
    "chatgpt-radar-07",
    "chatgpt-radar-08",
    "chatgpt-radar-09",
)
HISTORICAL_CHATGPT_SOURCE_IDS = frozenset(
    f"chatgpt-radar-{index:02d}" for index in range(1, 10)
)
RETIRED_CHATGPT_SOURCE_IDS = HISTORICAL_CHATGPT_SOURCE_IDS - set(ACTIVE_CHATGPT_SOURCE_IDS)


def is_inactive_chatgpt_source(source_id: str) -> bool:
    """Reserve the monitoring namespace without limiting other import sources."""
    return source_id.startswith("chatgpt-radar-") and source_id not in ACTIVE_CHATGPT_SOURCE_IDS
