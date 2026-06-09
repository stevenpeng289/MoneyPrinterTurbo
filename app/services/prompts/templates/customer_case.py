"""客户案例模板。

目标：用"客户背景 + 之前的问题 + 改变 + 结果"的标准结构讲一个可信的客户案例。
"""

from app.services.prompts.templates._base import FewShotExample, ScriptTemplate

TEMPLATE = ScriptTemplate(
    id="customer_case",
    name="客户案例分享",
    category="case",
    description="客户成功案例脚本，背景 → 痛点 → 改变 → 结果四段式，可量化数据优先。",
    default_paragraph_number=4,
    system_prompt="""
# Role: Customer Case Narrator

## Goal
Generate a 4-paragraph Chinese voice-over for a customer success story.

## Structure
1. Background — who the customer is (1 句话，行业 + 规模代称).
2. Pain — what was broken before (具体场景，不抽象).
3. Change — what changed (服务 / 流程 / 工具).
4. Result — quantified outcome if possible; otherwise qualitative.

## Constraints
- Spoken Chinese, no markdown.
- Use 化名 like "某宁波家居卖家"；never invent named brands.
- Numbers must be conservative; prefer ranges ("缩短 20%-30%") over precise fake figures.
- Each paragraph 70-120 Chinese characters.
""".strip(),
    suggested_keywords_hint="客户类型 + 解决方案，如 '家居卖家 海外仓' 或 '3C 大卖 退货处理'",
    few_shot_examples=(
        FewShotExample(
            subject="某 3C 配件卖家通过海外仓压低退货周转",
            script=(
                "这是一家年销 800 万美金的 3C 配件卖家，主战场美东，发货走自发货。"
                "他们最头疼的是退货——每个月 1 万多件退货堆在洛杉矶，二次销售周期长达三周。"
                "今年三月切到我们美西海外仓，退货件直接入仓验收、贴新标、重新上架，无需绕回中国。"
                "三个月跑下来，退货二次售卖周期从 21 天压到 5-7 天，损耗率也降了大概 30%。"
            ),
        ),
    ),
)
