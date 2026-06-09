"""跨境物流政策解读模板。

目标：把政策文件翻译成口语化的解读视频脚本，强调"政策→影响→对策"三段式。
"""

from app.services.prompts.templates._base import FewShotExample, ScriptTemplate

TEMPLATE = ScriptTemplate(
    id="cross_border_policy",
    name="跨境物流政策解读",
    category="policy",
    description="把跨境物流相关政策（关税、报关、合规、跨境支付）翻译成口语化解读，三段式结构。",
    default_paragraph_number=3,
    system_prompt="""
# Role: Cross-border Logistics Policy Interpreter

## Goal
Generate a Chinese voice-over script (3 paragraphs by default) that interprets a cross-border logistics policy.

## Structure (must follow strictly)
1. Paragraph 1 — Policy What: state the policy in plain language, no jargon.
2. Paragraph 2 — Practical Impact: who is affected, what changes for sellers / freight forwarders / customs brokers.
3. Paragraph 3 — Action: concrete next step the audience can take this week.

## Constraints
- Spoken Chinese, no markdown, no headings.
- No political commentary, no speculation about regulator intent.
- Cite no fake numbers; if specifics are unknown, say "需以官方文件为准".
- Keep each paragraph 80-150 Chinese characters.
""".strip(),
    suggested_keywords_hint="政策名 + 行业关键词，如 '海运 报关 合规'",
    few_shot_examples=(
        FewShotExample(
            subject="美国对小额包裹 800 美元免税门槛调整",
            script=(
                "美国海关宣布对原本免税的 800 美元小额包裹政策做出调整，"
                "部分品类不再享受零关税通关。"
                "对跨境电商卖家来说，这意味着以前靠拆单避税的玩法走不通了，"
                "中小卖家的物流成本要重新算账。"
                "建议本周就和你的货代复盘一遍 SKU 清单，"
                "把受影响品类切换到海外仓发货，先把履约链路保住。"
            ),
        ),
    ),
)
