from dataclasses import asdict

from fastapi import HTTPException, Path, Request

from app.controllers.v1.base import new_router
from app.models.schema import (
    TemplateDetailResponse,
    TemplateListResponse,
    VideoScriptRequest,
    VideoScriptResponse,
    VideoSocialMetadataRequest,
    VideoSocialMetadataResponse,
    VideoTermsRequest,
    VideoTermsResponse,
)
from app.services import llm
from app.utils import utils

# authentication dependency
# router = new_router(dependencies=[Depends(base.verify_token)])
router = new_router()


def _template_to_brief(template: llm.ScriptTemplate) -> dict:
    """转成 TemplateBrief 的 dict（不暴露 system_prompt / few_shot）。"""
    return {
        "id": template.id,
        "name": template.name,
        "category": template.category,
        "description": template.description,
        "default_paragraph_number": template.default_paragraph_number,
    }


def _template_to_detail(template: llm.ScriptTemplate) -> dict:
    """转成 TemplateDetail 的 dict（含 system_prompt 和 few-shot）。"""
    brief = _template_to_brief(template)
    brief.update(
        {
            "system_prompt": template.system_prompt,
            "suggested_keywords_hint": template.suggested_keywords_hint,
            "few_shot_examples": [asdict(ex) for ex in template.few_shot_examples],
        }
    )
    return brief


@router.post(
    "/scripts",
    response_model=VideoScriptResponse,
    summary="Create a script for the video",
)
def generate_video_script(request: Request, body: VideoScriptRequest):
    # 改造 B：填了 template_id 优先走模板通道；否则维持原有 generate_script 通道。
    if body.template_id:
        try:
            video_script = llm.generate_script_from_template(
                template_id=body.template_id,
                video_subject=body.video_subject or "",
                language=body.video_language or "",
                paragraph_number=body.paragraph_number,
                extra_prompt=body.video_script_prompt or "",
            )
        except llm.UnknownTemplateError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    else:
        video_script = llm.generate_script(
            video_subject=body.video_subject,
            language=body.video_language,
            paragraph_number=body.paragraph_number,
            video_script_prompt=body.video_script_prompt,
            custom_system_prompt=body.custom_system_prompt,
        )
    response = {"video_script": video_script}
    return utils.get_response(200, response)


@router.post(
    "/terms",
    response_model=VideoTermsResponse,
    summary="Generate video terms based on the video script",
)
def generate_video_terms(request: Request, body: VideoTermsRequest):
    video_terms = llm.generate_terms(
        video_subject=body.video_subject,
        video_script=body.video_script,
        amount=body.amount,
    )
    response = {"video_terms": video_terms}
    return utils.get_response(200, response)


@router.post(
    "/social-metadata",
    response_model=VideoSocialMetadataResponse,
    summary="Generate social publishing metadata",
)
def generate_video_social_metadata(
    request: Request, body: VideoSocialMetadataRequest
):
    metadata = llm.generate_social_metadata(
        video_subject=body.video_subject,
        video_script=body.video_script,
        language=body.language,
        platform=body.platform,
    )
    return utils.get_response(200, metadata)


@router.get(
    "/templates",
    response_model=TemplateListResponse,
    summary="List industry script templates (改造 B)",
)
def list_script_templates(request: Request):
    """返回所有可套用的行业脚本模板（不含 system_prompt 详情）。"""
    templates = [_template_to_brief(t) for t in llm.list_templates()]
    return utils.get_response(200, {"templates": templates})


@router.get(
    "/templates/{template_id}",
    response_model=TemplateDetailResponse,
    summary="Get script template detail by id (改造 B)",
)
def get_script_template(
    request: Request,
    template_id: str = Path(..., min_length=1, max_length=64),
):
    """返回单个模板详情，含 system_prompt 和 few-shot 例子。未注册返回 404。"""
    try:
        template = llm.load_template(template_id)
    except llm.UnknownTemplateError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return utils.get_response(200, _template_to_detail(template))

