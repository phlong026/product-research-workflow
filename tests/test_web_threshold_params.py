"""Web 分析作业不再默认传 250:推起/留存阈值由人工给出,未给则不传。"""
from research_workflow.web import jobs


def _argv(params, tmp_path):
    return jobs.build_argv("analyze", jobs.validate_params("analyze", params), project_id="p1",
                           project_dir=tmp_path, upload_path=lambda u: tmp_path / u)


def test_analyze_defaults_pass_no_threshold_flags(tmp_path):
    argv = _argv({}, tmp_path)
    assert not any(a.startswith(("--sales-threshold", "--promotion-threshold", "--retention-threshold")) for a in argv)


def test_explicit_thresholds_are_forwarded(tmp_path):
    argv = _argv({"promotion_threshold": 300, "retention_threshold": 200, "sales_threshold": 250}, tmp_path)
    assert {"--promotion-threshold=300", "--retention-threshold=200", "--sales-threshold=250"} <= set(argv)
