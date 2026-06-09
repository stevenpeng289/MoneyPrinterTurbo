"""行业脚本模板注册表。

新增模板的步骤：
1. 在本目录下新增 `<id>.py`，按 `_base.ScriptTemplate` 结构定义 `TEMPLATE`。
2. 在下方 `_REGISTERED_MODULES` 列表里追加 `<id>` 字符串。
3. `TEMPLATE_REGISTRY` 在导入时会自动校验所有模板字段。

`TEMPLATE_REGISTRY` 设计为 `MappingProxyType`（只读视图），
避免运行期被外部代码意外修改。
"""

from __future__ import annotations

import importlib
from types import MappingProxyType
from typing import Dict, Mapping

from app.services.prompts.templates._base import (
    FewShotExample,
    ScriptTemplate,
)

# 注册表来源单一：模块名 = 模板 id。
# 显式列出而不是扫描目录，避免开发期临时文件被误识别为模板。
_REGISTERED_MODULES = (
    "cross_border_policy",
    "cloud_warehouse_promo",
    "industry_insider",
    "customer_case",
    "product_compare",
)


def _load_registry() -> Mapping[str, ScriptTemplate]:
    registry: Dict[str, ScriptTemplate] = {}
    for module_name in _REGISTERED_MODULES:
        module = importlib.import_module(
            f"app.services.prompts.templates.{module_name}"
        )
        template = getattr(module, "TEMPLATE", None)
        if not isinstance(template, ScriptTemplate):
            raise TypeError(
                f"templates.{module_name}.TEMPLATE must be ScriptTemplate, "
                f"got {type(template).__name__}"
            )
        if template.id != module_name:
            raise ValueError(
                f"templates.{module_name}.TEMPLATE.id={template.id!r} "
                f"does not match module name {module_name!r}"
            )
        if template.id in registry:
            raise ValueError(f"Duplicate template id detected: {template.id}")
        registry[template.id] = template
    return MappingProxyType(registry)


TEMPLATE_REGISTRY: Mapping[str, ScriptTemplate] = _load_registry()


__all__ = [
    "FewShotExample",
    "ScriptTemplate",
    "TEMPLATE_REGISTRY",
]
