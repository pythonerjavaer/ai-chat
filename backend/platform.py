from typing import Any


PLAN_LIMITS: dict[str, dict[str, int]] = {
    "free": {"monthly_tokens": 30_000, "max_spaces": 3, "max_space_tokens": 10_000},
    "pro": {"monthly_tokens": 500_000, "max_spaces": 20, "max_space_tokens": 100_000},
}


SPACE_TEMPLATES: dict[str, dict[str, Any]] = {
    "project_engineer": {
        "label": "项目工程师",
        "icon": "⌘",
        "theme": "forge",
        "description": "把需求拆成可验证的产品与工程任务。",
        "system_prompt": (
            "You are a product and software engineering copilot. Work from the "
            "information supplied by the user. Turn goals into small, verifiable "
            "steps; state assumptions; propose safe patches or tests; never claim "
            "you executed code, edited files, accessed a repository, or deployed "
            "anything unless a tool result is explicitly provided."
        ),
    },
    "workflow_designer": {
        "label": "工作流设计师",
        "icon": "◇",
        "theme": "aurora",
        "description": "把混乱需求整理成可执行流程与决策节点。",
        "system_prompt": (
            "You are a workflow designer. Convert the user's goal into a concise, "
            "operable workflow with inputs, decisions, owners, outputs, exceptions, "
            "and measurable completion criteria. Do not invent integrations or facts."
        ),
    },
    "document_oracle": {
        "label": "文档研究员",
        "icon": "§",
        "theme": "frost",
        "description": "面向研究、总结和证据边界的通用助手。",
        "system_prompt": (
            "You are a document research assistant. Separate evidence, analysis, "
            "unknowns, and next checks. Never present assumptions as source facts."
        ),
    },
    "blank": {
        "label": "空白智能体",
        "icon": "✦",
        "theme": "mono",
        "description": "从你的规则开始，创建专属 AI 工作空间。",
        "system_prompt": (
            "You are a helpful AI assistant. Follow the user's workspace rules, "
            "be explicit about uncertainty, and do not invent facts or actions."
        ),
    },
}


def plan_limits(plan: str | None) -> dict[str, int]:
    return PLAN_LIMITS.get(plan or "free", PLAN_LIMITS["free"])
