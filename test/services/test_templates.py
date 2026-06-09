"""改造 B 模板库相关单元测试。

覆盖：
- TEMPLATE_REGISTRY 注册表完整性、唯一性、字段校验
- llm.list_templates / load_template / generate_script_from_template
- ScriptTemplate frozen + 字段校验
- controller GET /api/v1/templates 与 GET /api/v1/templates/{id}
- /scripts 接口的 template_id 分支
"""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from fastapi.testclient import TestClient

from app.asgi import app
from app.models.schema import VideoParams
from app.services import llm
from app.services.prompts.templates import (
    TEMPLATE_REGISTRY,
    FewShotExample,
    ScriptTemplate,
)


# ----------------------------------------------------------------------
# 注册表完整性
# ----------------------------------------------------------------------


class TestTemplateRegistry:
    def test_registry_is_non_empty(self):
        assert len(TEMPLATE_REGISTRY) >= 5

    def test_registry_ids_unique(self):
        ids = [t.id for t in TEMPLATE_REGISTRY.values()]
        assert len(ids) == len(set(ids))

    def test_each_template_has_required_fields(self):
        for tpl in TEMPLATE_REGISTRY.values():
            assert tpl.id
            assert tpl.name
            assert tpl.category
            assert tpl.description
            assert 1 <= tpl.default_paragraph_number <= 10
            assert tpl.system_prompt.strip()

    def test_few_shot_examples_well_formed(self):
        for tpl in TEMPLATE_REGISTRY.values():
            for ex in tpl.few_shot_examples:
                assert isinstance(ex, FewShotExample)
                assert ex.subject
                assert ex.script

    def test_required_templates_present(self):
        # 5 个 plan 中要求的模板必须在
        required_ids = {
            "cross_border_policy",
            "cloud_warehouse_promo",
            "industry_insider",
            "customer_case",
            "product_compare",
        }
        assert required_ids.issubset(TEMPLATE_REGISTRY.keys())


# ----------------------------------------------------------------------
# ScriptTemplate dataclass 校验
# ----------------------------------------------------------------------


class TestScriptTemplateValidation:
    def test_frozen_dataclass(self):
        tpl = next(iter(TEMPLATE_REGISTRY.values()))
        with pytest.raises((AttributeError, Exception)):  # FrozenInstanceError
            tpl.name = "新名字"  # type: ignore[misc]

    def test_invalid_id_rejected(self):
        with pytest.raises(ValueError, match="must be non-empty alphanumeric"):
            ScriptTemplate(
                id="bad id!",
                name="x",
                category="y",
                description="z",
                default_paragraph_number=1,
                system_prompt="p",
            )

    def test_missing_name_rejected(self):
        with pytest.raises(ValueError, match="name is required"):
            ScriptTemplate(
                id="ok",
                name="",
                category="y",
                description="z",
                default_paragraph_number=1,
                system_prompt="p",
            )

    def test_paragraph_number_out_of_range_rejected(self):
        with pytest.raises(ValueError, match="paragraph_number out of range"):
            ScriptTemplate(
                id="ok",
                name="x",
                category="y",
                description="z",
                default_paragraph_number=99,
                system_prompt="p",
            )

    def test_empty_system_prompt_rejected(self):
        with pytest.raises(ValueError, match="system_prompt cannot be empty"):
            ScriptTemplate(
                id="ok",
                name="x",
                category="y",
                description="z",
                default_paragraph_number=1,
                system_prompt="   ",
            )


# ----------------------------------------------------------------------
# llm 服务层接口
# ----------------------------------------------------------------------


class TestLLMTemplateInterface:
    def test_list_templates_sorted(self):
        templates = llm.list_templates()
        ids = [t.id for t in templates]
        assert ids == sorted(ids)
        assert len(templates) == len(TEMPLATE_REGISTRY)

    def test_load_template_returns_same_instance(self):
        for tpl_id in TEMPLATE_REGISTRY:
            loaded = llm.load_template(tpl_id)
            assert loaded is TEMPLATE_REGISTRY[tpl_id]

    def test_load_template_unknown_raises(self):
        with pytest.raises(llm.UnknownTemplateError):
            llm.load_template("nope_xxx")

    def test_generate_script_from_template_invokes_generate_script(self):
        """套用模板必须复用 generate_script()，而不是重写生成逻辑。"""
        with patch.object(llm, "generate_script", return_value="生成内容") as gen:
            result = llm.generate_script_from_template(
                template_id="cross_border_policy",
                video_subject="跨境关税新政",
                language="zh-CN",
                paragraph_number=None,  # 走模板 default
                extra_prompt="语气要严肃",
            )
        assert result == "生成内容"
        gen.assert_called_once()
        kwargs = gen.call_args.kwargs
        assert kwargs["video_subject"] == "跨境关税新政"
        assert kwargs["language"] == "zh-CN"
        # 模板 default_paragraph_number = 3
        assert kwargs["paragraph_number"] == 3
        assert kwargs["video_script_prompt"] == "语气要严肃"
        # custom_system_prompt 应该是模板渲染后的完整 prompt
        assert "Cross-border Logistics Policy" in kwargs["custom_system_prompt"]
        # few-shot 例子应被注入
        assert "Few-shot Examples" in kwargs["custom_system_prompt"]

    def test_generate_script_from_template_explicit_paragraph_overrides_default(self):
        with patch.object(llm, "generate_script", return_value="x") as gen:
            llm.generate_script_from_template(
                template_id="cross_border_policy",
                video_subject="x",
                paragraph_number=7,
            )
        assert gen.call_args.kwargs["paragraph_number"] == 7

    def test_generate_script_from_template_unknown_raises(self):
        with pytest.raises(llm.UnknownTemplateError):
            llm.generate_script_from_template(
                template_id="nope", video_subject="x"
            )


# ----------------------------------------------------------------------
# Pydantic schema：VideoParams.template_id
# ----------------------------------------------------------------------


class TestVideoParamsTemplateField:
    def test_template_id_defaults_to_none(self):
        params = VideoParams(video_subject="测试")
        assert params.template_id is None

    def test_template_id_accepts_valid_string(self):
        params = VideoParams(
            video_subject="测试", template_id="cross_border_policy"
        )
        assert params.template_id == "cross_border_policy"

    def test_template_id_too_long_rejected(self):
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            VideoParams(video_subject="测试", template_id="x" * 100)


# ----------------------------------------------------------------------
# FastAPI controller 端到端（用 TestClient）
# ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


class TestTemplateEndpoints:
    def test_list_templates_returns_brief(self, client: TestClient):
        r = client.get("/api/v1/templates")
        assert r.status_code == 200
        payload = r.json()
        assert payload["status"] == 200
        templates = payload["data"]["templates"]
        assert len(templates) >= 5
        # brief 不应暴露 system_prompt
        assert all("system_prompt" not in t for t in templates)
        for t in templates:
            assert set(t.keys()) == {
                "id",
                "name",
                "category",
                "description",
                "default_paragraph_number",
            }

    def test_get_template_detail(self, client: TestClient):
        r = client.get("/api/v1/templates/cross_border_policy")
        assert r.status_code == 200
        detail = r.json()["data"]
        assert detail["id"] == "cross_border_policy"
        assert detail["system_prompt"]
        assert isinstance(detail["few_shot_examples"], list)

    def test_get_unknown_template_returns_404(self, client: TestClient):
        r = client.get("/api/v1/templates/nope_xxx")
        assert r.status_code == 404

    def test_scripts_endpoint_with_template_id_uses_template(
        self, client: TestClient
    ):
        with patch.object(
            llm, "generate_script_from_template", return_value="模板生成的脚本"
        ) as gen_tpl, patch.object(
            llm, "generate_script", return_value="不应被调用"
        ) as gen_plain:
            r = client.post(
                "/api/v1/scripts",
                json={
                    "video_subject": "跨境物流新政",
                    "video_language": "zh-CN",
                    "paragraph_number": 3,
                    "template_id": "cross_border_policy",
                },
            )
        assert r.status_code == 200
        assert r.json()["data"]["video_script"] == "模板生成的脚本"
        gen_tpl.assert_called_once()
        gen_plain.assert_not_called()

    def test_scripts_endpoint_without_template_id_uses_plain(
        self, client: TestClient
    ):
        with patch.object(
            llm, "generate_script", return_value="普通生成的脚本"
        ) as gen_plain, patch.object(
            llm, "generate_script_from_template", return_value="x"
        ) as gen_tpl:
            r = client.post(
                "/api/v1/scripts",
                json={
                    "video_subject": "随便什么",
                    "video_language": "",
                    "paragraph_number": 1,
                },
            )
        assert r.status_code == 200
        assert r.json()["data"]["video_script"] == "普通生成的脚本"
        gen_plain.assert_called_once()
        gen_tpl.assert_not_called()

    def test_scripts_endpoint_with_unknown_template_returns_404(
        self, client: TestClient
    ):
        r = client.post(
            "/api/v1/scripts",
            json={
                "video_subject": "x",
                "template_id": "nope_xxx",
            },
        )
        assert r.status_code == 404
