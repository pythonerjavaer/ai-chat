import ast
import json
import operator
from collections.abc import Generator
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from openai import OpenAI

from . import database
from .config import settings


client = OpenAI(api_key=settings.openai_api_key)
MAX_TOOL_ROUNDS = 3
RAG_MIN_SIMILARITY = 0.30

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": "Evaluate a basic arithmetic expression safely.",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "Arithmetic expression using numbers and +, -, *, /, //, %, **, parentheses.",
                    }
                },
                "required": ["expression"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "Get the current date and time in an IANA timezone.",
            "parameters": {
                "type": "object",
                "properties": {
                    "timezone": {
                        "type": "string",
                        "description": "IANA timezone such as Australia/Sydney or Asia/Shanghai.",
                    }
                },
                "required": ["timezone"],
                "additionalProperties": False,
            },
        },
    },
]

_BINARY_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY_OPERATORS = {ast.UAdd: operator.pos, ast.USub: operator.neg}


def split_document(text: str, size: int = 900, overlap: int = 140) -> list[str]:
    normalized = "\n".join(line.strip() for line in text.splitlines() if line.strip())
    if not normalized:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(normalized):
        end = min(start + size, len(normalized))
        if end < len(normalized):
            boundary = normalized.rfind("\n", start, end)
            if boundary <= start + size // 2:
                boundary = normalized.rfind("。", start, end)
            if boundary > start + size // 2:
                end = boundary + 1
        chunks.append(normalized[start:end].strip())
        if end >= len(normalized):
            break
        start = max(end - overlap, start + 1)
    return [chunk for chunk in chunks if chunk]


def create_embeddings(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    response = client.embeddings.create(
        model=settings.embedding_model,
        input=texts,
    )
    return [item.embedding for item in sorted(response.data, key=lambda item: item.index)]


def retrieve_context(user_id: int, query: str) -> list[dict[str, Any]]:
    if not database.list_documents(user_id):
        return []
    query_embedding = create_embeddings([query])[0]
    candidates = database.search_chunks(user_id, query_embedding, limit=4)
    return [item for item in candidates if item["score"] >= RAG_MIN_SIMILARITY]


def build_messages(
    user_id: int,
    session_id: str,
    context: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    instructions = [
        "You are a helpful assistant in a persistent chat application.",
        "Answer in Chinese when the user writes Chinese.",
        "Use tools when they materially improve accuracy.",
        "Do not claim a tool was used unless a tool result appears in the conversation.",
    ]
    if context:
        excerpts = "\n\n".join(
            f"[Source: {item['name']}]\n{item['content']}" for item in context
        )
        instructions.append(
            "Use the following private knowledge-base excerpts when relevant. "
            "If they do not answer the question, say so rather than inventing facts.\n\n"
            + excerpts
        )

    stored_messages = database.list_messages(session_id, user_id, limit=40)
    return [
        {"role": "system", "content": "\n\n".join(instructions)},
        *[
            {"role": message["role"], "content": message["content"]}
            for message in stored_messages
        ],
    ]


def _evaluate_node(node: ast.AST) -> float | int:
    if isinstance(node, ast.Expression):
        return _evaluate_node(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _BINARY_OPERATORS:
        left = _evaluate_node(node.left)
        right = _evaluate_node(node.right)
        if isinstance(node.op, ast.Pow) and abs(right) > 12:
            raise ValueError("Exponent is too large.")
        result = _BINARY_OPERATORS[type(node.op)](left, right)
        if abs(result) > 1e100:
            raise ValueError("Result is too large.")
        return result
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPERATORS:
        return _UNARY_OPERATORS[type(node.op)](_evaluate_node(node.operand))
    raise ValueError("Unsupported expression.")


def calculate(expression: str) -> dict[str, Any]:
    if len(expression) > 200:
        raise ValueError("Expression is too long.")
    parsed = ast.parse(expression, mode="eval")
    return {"expression": expression, "result": _evaluate_node(parsed)}


def get_current_time(timezone_name: str) -> dict[str, str]:
    try:
        zone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("Unknown IANA timezone.") from exc
    now = datetime.now(zone)
    return {
        "timezone": timezone_name,
        "iso": now.isoformat(),
        "display": now.strftime("%Y-%m-%d %H:%M:%S %Z"),
    }


def execute_tool(name: str, arguments: str) -> str:
    try:
        payload = json.loads(arguments or "{}")
        if name == "calculate":
            result = calculate(payload.get("expression", ""))
        elif name == "get_current_time":
            result = get_current_time(payload.get("timezone", "UTC"))
        else:
            result = {"error": f"Unknown tool: {name}"}
    except (ValueError, SyntaxError, ZeroDivisionError, TypeError) as exc:
        result = {"error": str(exc)}
    return json.dumps(result, ensure_ascii=False)


def run_agent(messages: list[dict[str, Any]]) -> tuple[str, list[str]]:
    working_messages = list(messages)
    tools_used: list[str] = []

    for _ in range(MAX_TOOL_ROUNDS + 1):
        response = client.chat.completions.create(
            model=settings.ai_model,
            messages=working_messages,
            tools=TOOLS,
            tool_choice="auto",
        )
        message = response.choices[0].message
        if not message.tool_calls:
            return message.content or "", list(dict.fromkeys(tools_used))

        working_messages.append(
            {
                "role": "assistant",
                "content": message.content,
                "tool_calls": [
                    tool_call.model_dump(exclude_none=True)
                    for tool_call in message.tool_calls
                ],
            }
        )
        for tool_call in message.tool_calls:
            name = tool_call.function.name
            tools_used.append(name)
            working_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": execute_tool(name, tool_call.function.arguments),
                }
            )

    raise RuntimeError("Agent exceeded the tool-call limit.")


def stream_agent(messages: list[dict[str, Any]]) -> Generator[dict[str, Any], None, None]:
    working_messages = list(messages)
    tools_used: list[str] = []

    for _ in range(MAX_TOOL_ROUNDS + 1):
        stream = client.chat.completions.create(
            model=settings.ai_model,
            messages=working_messages,
            tools=TOOLS,
            tool_choice="auto",
            stream=True,
        )
        text_parts: list[str] = []
        tool_buffers: dict[int, dict[str, str]] = {}

        for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta.content:
                text_parts.append(delta.content)
                yield {"type": "token", "content": delta.content}
            for partial in delta.tool_calls or []:
                buffer = tool_buffers.setdefault(
                    partial.index,
                    {"id": "", "name": "", "arguments": ""},
                )
                if partial.id:
                    buffer["id"] = partial.id
                if partial.function and partial.function.name:
                    buffer["name"] += partial.function.name
                if partial.function and partial.function.arguments:
                    buffer["arguments"] += partial.function.arguments

        if not tool_buffers:
            yield {
                "type": "done",
                "reply": "".join(text_parts),
                "tools_used": list(dict.fromkeys(tools_used)),
            }
            return

        tool_calls = [
            {
                "id": item["id"],
                "type": "function",
                "function": {
                    "name": item["name"],
                    "arguments": item["arguments"],
                },
            }
            for _, item in sorted(tool_buffers.items())
        ]
        working_messages.append(
            {"role": "assistant", "content": None, "tool_calls": tool_calls}
        )
        for tool_call in tool_calls:
            name = tool_call["function"]["name"]
            tools_used.append(name)
            yield {"type": "tool", "name": name}
            working_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call["id"],
                    "content": execute_tool(
                        name,
                        tool_call["function"]["arguments"],
                    ),
                }
            )

    raise RuntimeError("Agent exceeded the tool-call limit.")
