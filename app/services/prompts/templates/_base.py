"""模板基础结构。

`ScriptTemplate` 是行业模板的统一容器，所有字段冻结（frozen=True）。
- `id`：英文短标识，作为 API 和 WebUI 的索引键。
- `name`：用户可见的中文标题。
- `category`：分类标签（policy / promo / case / insider / compare 等），便于 WebUI 分组。
- `description`：一句话描述用途。
- `default_paragraph_number`：推荐段落数。
- `system_prompt`：套用模板时覆盖 `DEFAULT_SCRIPT_SYSTEM_PROMPT` 的提示词。
- `few_shot_examples`：稳定输出风格用的少量示例（每条 dict 至少含 `subject` 和 `script`）。
- `suggested_keywords_hint`：调用 LLM 生成关键词时的额外提示。

设计要点：
- 模板只规定结构和风格，不写政治/合规敏感内容；
- 通过 `system_prompt` 复用 `app.services.llm.generate_script()` 现有签名，避免重复实现。
"""

from dataclasses import dataclass, field
from typing import Tuple


@dataclass(frozen=True)
class FewShotExample:
    """单条 few-shot 例子。

    使用 frozen dataclass 而不是 dict 是为了在加载时就能拒绝缺字段，
    避免运行期才发现 prompt 拼接失败。
    """

    subject: str
    script: str


@dataclass(frozen=True)
class ScriptTemplate:
    id: str
    name: str
    category: str
    description: str
    default_paragraph_number: int
    system_prompt: str
    suggested_keywords_hint: str = ""
    few_shot_examples: Tuple[FewShotExample, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        # 强校验关键字段，加载阶段就把脏数据挡掉。
        if not self.id or not self.id.replace("_", "").isalnum():
            raise ValueError(
                f"ScriptTemplate.id must be non-empty alphanumeric/underscore: {self.id!r}"
            )
        if not self.name:
            raise ValueError(f"ScriptTemplate.name is required for id={self.id}")
        if self.default_paragraph_number < 1 or self.default_paragraph_number > 10:
            raise ValueError(
                f"default_paragraph_number out of range [1,10]: {self.default_paragraph_number}"
            )
        if not self.system_prompt.strip():
            raise ValueError(f"system_prompt cannot be empty for id={self.id}")
