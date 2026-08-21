from typing import Literal


Workspace = Literal["general", "legal", "finance"]
DEFAULT_WORKSPACE: Workspace = "general"

WORKSPACES: dict[Workspace, dict] = {
    "general": {
        "label": "通用文档",
        "description": "围绕个人资料进行可追溯的问答与总结。",
        "boundary": "请核对关键事实与来源。",
        "quick_actions": [
            "总结当前知识库的核心内容",
            "列出资料中的关键事实和待办事项",
            "指出资料中信息不足或相互矛盾的地方",
        ],
    },
    "legal": {
        "label": "合同与合规",
        "description": "审阅合同、政策和合规材料，提取条款、义务与风险。",
        "boundary": "仅作文件审阅辅助，不构成正式法律意见。",
        "quick_actions": [
            "提取付款、续约、终止、违约和保密条款，并逐项注明来源",
            "列出各方义务、负责人、期限和未明确事项",
            "生成一份按严重程度排序的合同风险检查清单",
            "总结这份材料涉及的合规要求和证据缺口",
        ],
    },
    "finance": {
        "label": "金融研究",
        "description": "研究财报、公告和投资材料，提取指标并进行可复核计算。",
        "boundary": "仅供研究与信息分析，不构成个性化投资建议。",
        "quick_actions": [
            "提取收入、利润、现金流和资产负债表中的关键指标并注明来源",
            "比较不同期间的增长、利润率和资本结构变化",
            "找出管理层陈述中的主要风险、假设与不确定性",
            "根据资料生成一份有来源依据的金融研究摘要",
        ],
    },
}


def validate_workspace(value: str | None) -> Workspace:
    workspace = (value or DEFAULT_WORKSPACE).strip().lower()
    if workspace not in WORKSPACES:
        raise ValueError(f"Unsupported workspace: {workspace}")
    return workspace  # type: ignore[return-value]


def public_workspace_config() -> list[dict]:
    return [
        {"id": workspace, **config}
        for workspace, config in WORKSPACES.items()
    ]
