"""行业内幕揭秘模板。

目标：用"反常识 + 证据 + 启示"的钩子结构做行业科普，适合做账号人设。
"""

from app.services.prompts.templates._base import FewShotExample, ScriptTemplate

TEMPLATE = ScriptTemplate(
    id="industry_insider",
    name="行业内幕揭秘",
    category="insider",
    description="跨境物流 / 电商行业内幕，'反常识 + 证据 + 启示'的钩子结构，强调干货。",
    default_paragraph_number=4,
    system_prompt="""
# Role: Cross-border Industry Insider

## Goal
Generate a 4-paragraph Chinese voice-over that reveals a counterintuitive industry insight.

## Structure
1. Hook — counterintuitive claim in one sentence ("你以为...，其实...").
2. Evidence — concrete example or mechanism (一个真实场景 / 一段链路解释).
3. Why it matters — what this means for someone in the audience.
4. Takeaway — single actionable insight.

## Constraints
- Spoken Chinese, no markdown.
- Cite no fake brand names; use 通用代称 like "某 3C 大卖" / "一家头部货代".
- Keep each paragraph 80-130 Chinese characters.
- Do not exaggerate; if a claim cannot be verified, hedge with "据从业者反馈".
""".strip(),
    suggested_keywords_hint="行业 + 现象名，如 '跨境支付 美元收款' 或 '海运 缺柜'",
    few_shot_examples=(
        FewShotExample(
            subject="跨境电商旺季为什么提前 2 个月备货反而亏更多",
            script=(
                "你以为旺季提前备货稳赚，其实今年提前两个月发柜的卖家，毛利反而被海运吃掉一半。"
                "原因不在销量，在汇率和柜价：9 月签的柜价，到 11 月销售时已经掉了 30%，账面就是浮亏。"
                "对没囤现金流的中小卖家来说，这意味着同样的销量，今年现金流可能比去年更紧。"
                "真要备货，按周做滚动 forecast，别一把梭把库存压在海上。"
            ),
        ),
    ),
)
