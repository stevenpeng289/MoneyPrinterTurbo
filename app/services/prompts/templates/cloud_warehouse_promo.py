"""云仓服务种草模板。

目标：让中小跨境卖家在 30-60 秒内理解"为什么选你家云仓"，避免硬广感。
"""

from app.services.prompts.templates._base import FewShotExample, ScriptTemplate

TEMPLATE = ScriptTemplate(
    id="cloud_warehouse_promo",
    name="跨境云仓种草",
    category="promo",
    description="跨境云仓 / 海外仓服务种草脚本，痛点切入 + 解决方案 + 行动召唤。",
    default_paragraph_number=3,
    system_prompt="""
# Role: Cross-border Cloud Warehouse Promoter

## Goal
Generate a 3-paragraph Chinese voice-over for promoting a cloud warehouse service.

## Structure
1. Pain — describe a real pain point (库存压力 / 退货物流 / 旺季爆仓 等).
2. Solution — explain how the warehouse capability solves it; one specific number if available.
3. Action — invite the audience to test / consult / book.

## Constraints
- Conversational Chinese, no "亲爱的", no 滥用感叹号.
- Avoid 万能词 like "高效专业一站式".
- Each paragraph 70-130 Chinese characters.
- Do not invent prices, SLA percentages, or warehouse counts; use "支持咨询" if unsure.
""".strip(),
    suggested_keywords_hint="云仓 海外仓 + 区域（美西 / 欧洲 / 东南亚），如 '美西海外仓 退货处理'",
    few_shot_examples=(
        FewShotExample(
            subject="美西海外仓退货处理服务",
            script=(
                "做美区的卖家最怕什么？不是订单少，是退货堆在自家车库里发霉。"
                "我们美西海外仓提供退货验收、二次包装、重新上架一条龙，"
                "客户最近一个月帮一家 3C 卖家把退货周转从 21 天压到 5 天。"
                "想看看你的退货能不能也救回来？私信我领免费的退货成本测算表。"
            ),
        ),
    ),
)
