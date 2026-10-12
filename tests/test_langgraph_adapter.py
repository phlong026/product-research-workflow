import importlib.util
import pytest

from research_workflow.workflow.langgraph_adapter import build_human_review_graph


def test_langgraph_adapter_is_lazy_and_actionable_when_extra_missing():
    if importlib.util.find_spec("langgraph") is None:
        with pytest.raises(RuntimeError, match="workflow extras"):
            build_human_review_graph()
