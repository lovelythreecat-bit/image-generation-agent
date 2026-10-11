import importlib
import importlib.util

import pytest

from image_agent.errors import ProviderError


def formatter():
    assert importlib.util.find_spec("image_agent.user_errors"), "中文错误接口尚未实现"
    return importlib.import_module("image_agent.user_errors").format_user_error


@pytest.mark.parametrize(
    "error,expected",
    [
        (ProviderError("provider HTTP 401", kind="http", status_code=401), "密钥"),
        (ProviderError("provider HTTP 429", kind="http", status_code=429), "限流"),
        (ProviderError("provider HTTP 400", kind="http", status_code=400), "参数"),
        (ProviderError("provider connection or timeout exhausted", kind="transport"), "超时"),
        (ProviderError("invalid image base64"), "图片数据"),
        (ProviderError("plan_product_long_image: source_quote missing"), "返回格式"),
        (RuntimeError("something unexpected token=private-value"), "未分类异常"),
    ],
)
def test_errors_explain_known_causes_and_keep_unknowns_honest(error, expected):
    shown = formatter()(error, stage="long_plan")
    assert "长图策划" in shown.summary
    assert expected in shown.reason
    assert shown.action
    assert "private-value" not in shown.details
    assert "source_quote" not in shown.reason + shown.action


def test_uncertain_generation_never_suggests_resubmission():
    shown = formatter()(
        {"status": "generation_uncertain", "stop_reason": "generation_uncertain"}
    )
    assert "尚未确认" in shown.reason
    assert "不要" in shown.action and "重复" in shown.action
    assert "重试" not in shown.action and "可继续" not in shown.action


def test_history_asset_preserves_error_stage_and_audit_reason():
    shown = formatter()(
        {
            "status": "audit_error",
            "error_info": {"code": "provider_http", "kind": "http", "status_code": 503,
                           "message": "provider HTTP 503"},
            "image": b"saved",
        }
    )
    assert "审核" in shown.summary and "暂时" in shown.reason
    assert "可继续" not in shown.action


def test_quality_failure_shows_specific_chinese_difference():
    shown = formatter()(
        {"status": "quality_failed", "stop_reason": "quality_failed",
         "quality": {"passed": False, "reason": "商品按钮位置改变，缺少文字：无线连接"}}
    )
    assert "按钮位置" in shown.reason and "无线连接" in shown.reason


def test_empty_service_failure_does_not_invent_a_cause():
    shown = formatter()({"status": "failed", "error": ""})
    assert "未返回具体原因" in shown.reason


def test_long_segment_failure_is_located_without_claiming_saved_parts_are_resumable():
    shown = formatter()(
        {"status": "audit_error", "error_info": {"code": "provider_protocol",
         "kind": "protocol", "message": "audit_image: invalid JSON"}}, stage="第 2 段审核"
    )
    assert "第 2 段审核" in shown.summary
    assert "返回格式" in shown.reason
    assert "可继续" not in shown.action


def test_plain_historical_english_error_is_not_labeled_unknown():
    shown = formatter()("invalid image base64; data[0].b64_json empty")
    assert "图片数据" in shown.reason


def test_copy_failure_names_missing_required_text():
    shown = formatter()({"status": "quality_failed", "copy_audit": {
        "passed": False, "text_matches": False, "issues": ["缺少指定文字：无线连接"]
    }}, stage="第 2 段审核")
    assert "无线连接" in shown.reason


def test_progress_uncertainty_without_error_info_never_appears_generic():
    shown = formatter()({"stage": "generation_uncertain", "message": "submitted response missing"})
    assert "尚未确认" in shown.reason


def test_validation_from_model_format_does_not_blame_user_inputs():
    shown = formatter()({"code": "validation", "kind": "validation",
                         "message": "source_quote: Field required"}, stage="plan")
    assert "返回格式" in shown.reason and "无需" in shown.action


def test_preview_keeps_specific_size_error_without_translating_valid_chinese():
    shown = formatter()(ValueError("图片尺寸超出预览上限"), stage="preview")
    assert "尺寸" in shown.reason


def test_model_refusal_has_its_own_reason_not_a_json_error():
    shown = formatter()(ProviderError("vision request refused", code="vision_refusal"))
    assert "拒绝" in shown.reason


def test_saved_structured_stage_survives_service_error_without_stage_in_message():
    shown = formatter()({"error_info": {"code": "provider_http", "kind": "http",
        "status_code": 401, "message": "provider HTTP 401", "stage": "long_plan"}})
    assert "长图策划" in shown.summary


def test_authentication_failure_explains_endpoint_key_pairing():
    shown = formatter()(ProviderError("provider HTTP 401", kind="http", status_code=401))
    assert "API 地址" in shown.action and "对应" in shown.action
