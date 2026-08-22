import hashlib
import json
import math
from typing import Any, Literal


SpaceRunMode = Literal["local", "lean", "deep"]

MODE_MAX_OUTPUT_TOKENS: dict[SpaceRunMode, int] = {
    "local": 0,
    "lean": 320,
    "deep": 800,
}

SPACE_RUNTIME_PREAMBLE = (
    "You are operating inside a user-created AI Space. Respect its rules, "
    "distinguish facts from assumptions, and never claim tool use or external "
    "actions that did not occur."
)


def normalize_message(message: str) -> str:
    """Normalize line endings without changing indentation or blank-line meaning.

    Whitespace can be semantically significant in code, Markdown, tables, and
    legal text. Only transport-level CRLF/CR line endings are normalized.
    """
    return message.replace("\r\n", "\n").replace("\r", "\n")


def compile_space_system_prompt(system_prompt: str, mode: SpaceRunMode) -> str:
    mode_instruction = (
        "Use a compact answer. Prioritize the requested deliverable and omit "
        "repetition."
        if mode == "lean"
        else "Perform a deeper review, but keep every section relevant and auditable."
    )
    return f"{SPACE_RUNTIME_PREAMBLE} {mode_instruction}\n\n{system_prompt.strip()}"


def estimate_text_tokens(text: str) -> int:
    """Return a deliberately conservative, dependency-free token estimate.

    ASCII prose is estimated near four characters per token. Non-ASCII input is
    counted at up to three tokens per code point, which protects budgets for CJK,
    emoji, and unusual byte sequences. A small fixed reserve covers chat framing.
    """
    ascii_count = sum(1 for character in text if ord(character) < 128)
    non_ascii_count = len(text) - ascii_count
    return max(1, math.ceil(ascii_count / 4) + (non_ascii_count * 3) + 32)


def estimate_space_input_tokens(
    system_prompt: str,
    message: str,
    mode: SpaceRunMode,
) -> int:
    compiled = compile_space_system_prompt(system_prompt, mode)
    return estimate_text_tokens(compiled) + estimate_text_tokens(message) + 16


def reserve_space_input_tokens(
    system_prompt: str,
    message: str,
    mode: SpaceRunMode,
) -> int:
    """Return a conservative input reservation from UTF-8 bytes plus framing."""
    compiled = compile_space_system_prompt(system_prompt, mode)
    return len(compiled.encode("utf-8")) + len(message.encode("utf-8")) + 64


def build_run_fingerprint(
    space: dict[str, Any],
    message: str,
    mode: SpaceRunMode,
    model: str,
) -> str:
    payload = {
        "space_id": space["id"],
        "space_updated_at": space["updated_at"],
        "system_prompt": space["system_prompt"],
        "message": normalize_message(message),
        "mode": mode,
        "model": model,
        "engine_version": 1,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_preflight(
    space: dict[str, Any],
    message: str,
    mode: SpaceRunMode,
    remaining_tokens: int,
    model: str,
    cache_hit: bool = False,
    cached_tokens: int = 0,
) -> dict[str, Any]:
    fingerprint = build_run_fingerprint(space, message, mode, model)
    if mode == "local":
        baseline_input = reserve_space_input_tokens(
            space["system_prompt"], message, "lean"
        )
        baseline_output = MODE_MAX_OUTPUT_TOKENS["lean"]
        return {
            "mode": mode,
            "fingerprint": fingerprint,
            "execution_path": "local",
            "cache_hit": False,
            "allowed": True,
            "model_calls": 0,
            "estimated_input_tokens": 0,
            "reserved_input_tokens": 0,
            "max_output_tokens": 0,
            "estimated_total_tokens": 0,
            "budget_reservation_tokens": 0,
            "estimated_tokens_saved": baseline_input + baseline_output,
            "tokens_saved_kind": "upper_bound_estimate",
            "remaining_tokens": max(0, remaining_tokens),
            "explanation": "由应用服务器按确定性规则整理并保存，不发送给 OpenAI，不消耗模型 Token。",
        }

    estimated_input = estimate_space_input_tokens(
        space["system_prompt"], message, mode
    )
    max_output = MODE_MAX_OUTPUT_TOKENS[mode]
    estimated_total = estimated_input + max_output
    if cache_hit:
        return {
            "mode": mode,
            "fingerprint": fingerprint,
            "execution_path": "cache",
            "cache_hit": True,
            "allowed": True,
            "model_calls": 0,
            "estimated_input_tokens": 0,
            "reserved_input_tokens": 0,
            "max_output_tokens": 0,
            "estimated_total_tokens": 0,
            "budget_reservation_tokens": 0,
            "estimated_tokens_saved": max(0, cached_tokens),
            "tokens_saved_kind": "actual_previous_usage",
            "remaining_tokens": max(0, remaining_tokens),
            "explanation": "规则、模式和输入指纹一致，直接复用已有成果。",
        }
    reserved_input = reserve_space_input_tokens(
        space["system_prompt"], message, mode
    )
    budget_reservation = reserved_input + max_output
    allowed = budget_reservation <= remaining_tokens
    return {
        "mode": mode,
        "fingerprint": fingerprint,
        "execution_path": mode,
        "cache_hit": False,
        "allowed": allowed,
        "model_calls": 1 if allowed else 0,
        "estimated_input_tokens": estimated_input,
        "reserved_input_tokens": reserved_input,
        "max_output_tokens": max_output,
        "estimated_total_tokens": estimated_total,
        "budget_reservation_tokens": budget_reservation,
        "estimated_tokens_saved": 0,
        "tokens_saved_kind": "none",
        "remaining_tokens": max(0, remaining_tokens),
        "explanation": (
            f"调用前已按保守预算预留 {budget_reservation} Tokens；执行时最多发起一次模型请求。"
            if allowed
            else f"剩余额度不足以覆盖保守预算预留 {budget_reservation} Tokens，本次不会调用模型。"
        ),
    }


_CAPSULE_PREFIXES = {
    "事实": "facts",
    "fact": "facts",
    "假设": "assumptions",
    "assumption": "assumptions",
    "待确认": "open_questions",
    "问题": "open_questions",
    "question": "open_questions",
    "行动": "next_actions",
    "下一步": "next_actions",
    "action": "next_actions",
    "todo": "next_actions",
}


def build_local_capsule(space: dict[str, Any], message: str) -> dict[str, Any]:
    """Compile explicitly labelled notes into a useful zero-model-token artifact."""
    normalized = normalize_message(message)
    artifact: dict[str, Any] = {
        "title": space["name"],
        "facts": [],
        "assumptions": [],
        "open_questions": [],
        "next_actions": [],
        "notes": [],
    }
    for raw_line in normalized.splitlines():
        line = raw_line.lstrip("-*• ").strip()
        prefix, separator, value = line.partition(":")
        if not separator:
            prefix, separator, value = line.partition("：")
        bucket = _CAPSULE_PREFIXES.get(prefix.strip().lower()) if separator else None
        if bucket and value.strip():
            artifact[bucket].append(value.strip())
        elif line:
            artifact["notes"].append(line)
    return artifact


def build_model_capsule(
    space: dict[str, Any],
    message: str,
    reply: str,
    mode: SpaceRunMode,
) -> dict[str, Any]:
    return {
        "title": space["name"],
        "mode": mode,
        "input": normalize_message(message),
        "result": reply,
    }


def render_local_capsule(artifact: dict[str, Any]) -> str:
    labels = (
        ("facts", "事实"),
        ("assumptions", "假设"),
        ("open_questions", "待确认"),
        ("next_actions", "下一步"),
        ("notes", "记录"),
    )
    sections = [f"# {artifact['title']}"]
    for key, label in labels:
        values = artifact.get(key) or []
        if values:
            sections.append(f"## {label}\n" + "\n".join(f"- {item}" for item in values))
    if len(sections) == 1:
        sections.append("尚未提供可整理的内容。")
    return "\n\n".join(sections)
