"""产品/服务对比模板。

目标：在 30-60 秒内做两个对比对象的横评，避免一边倒，让观众感觉客观。
"""

from app.services.prompts.templates._base import FewShotExample, ScriptTemplate

TEMPLATE = ScriptTemplate(
    id="product_compare",
    name="产品/服务对比",
    category="compare",
    description="两个产品/服务对比脚本，维度对齐，最后给『哪种场景选哪个』的条件式结论。",
    default_paragraph_number=3,
    system_prompt="""
# Role: Comparative Analyst

## Goal
Generate a 3-paragraph Chinese voice-over comparing two options on the same dimensions.

## Structure
1. Setup — name both options and pick 2-3 dimensions to compare.
2. Compare — go through dimensions side by side; no one-sided wording.
3. Recommendation — "如果你 X 场景，选 A；如果你 Y 场景，选 B"，给条件式结论而非绝对结论.

## Constraints
- Spoken Chinese, no markdown.
- Use 通用名 like "海运整柜 vs 海运拼箱" 或 "FBA 头程 vs 海外仓中转".
- No exaggerated cost numbers; use 范围 or "需根据线路而定".
- Each paragraph 80-130 Chinese characters.
""".strip(),
    suggested_keywords_hint="对比两个对象，如 'FBA vs 海外仓' 或 '海运整柜 vs 拼箱'",
    few_shot_examples=(
        FewShotExample(
            subject="FBA 直发 vs 海外仓中转，跨境电商怎么选",
            script=(
                "今天对比两个最常见的发货方式：FBA 直发和海外仓中转。"
                "成本看，FBA 头程稳定但仓储费按峰值算，海外仓头程低一点、仓储费按实际占用算；"
                "时效看，FBA 上架快，海外仓多一道中转但调拨更灵活；风险看，FBA 受平台规则约束更紧。"
                "如果你单一爆款、动销快，选 FBA 省事；如果 SKU 多、退货率高，海外仓更划算。"
            ),
        ),
    ),
)
