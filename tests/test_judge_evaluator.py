"""Unit tests for the LLM-as-a-Judge evaluator configuration and harness."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evaluation"))
import json
import pytest
from judge_evaluator import (
    load_evaluator_config, run_judge_evaluation, JUDGE_PROMPT_TEMPLATE, SAMPLE_AGENT_SUMMARIES
)

def test_evaluator_config_loaded():
    config = load_evaluator_config()
    assert config["name"] == "SummaryQuality"
    assert config["level"] == "SESSION"
    assert "llmAsAJudge" in config["config"]
    assert "instructions" in config["config"]["llmAsAJudge"]
    assert config["config"]["llmAsAJudge"]["model"] == "us.anthropic.claude-haiku-4-5-20251001-v1:0"

def test_prompt_template_formatting():
    prompt = JUDGE_PROMPT_TEMPLATE.format(
        instructions="Evaluate case summary completeness.",
        assertions="- Date of loss is correct\n- Amount matches",
        expected_response="Summary saved with no missing info.",
        agent_output=json.dumps({"caseId": "TEST-01"})
    )
    assert "[EVALUATION CRITERIA]" in prompt
    assert "[RATING SCALE]" in prompt
    assert "1 - Very Poor" in prompt
    assert "5 - Excellent" in prompt
    assert "TEST-01" in prompt

def test_judge_evaluation_scenarios():
    assert run_judge_evaluation("motor-clean", try_live=False) is True
    assert run_judge_evaluation("missing-photo", try_live=False) is True
    assert run_judge_evaluation("degraded-ocr", try_live=False) is True

def test_thresholds_configuration():
    thresholds_file = Path(__file__).resolve().parents[1] / "evaluation" / "thresholds.json"
    data = json.loads(thresholds_file.read_text(encoding="utf-8"))
    assert "SummaryQuality" in data
    assert data["SummaryQuality"] == 0.8
