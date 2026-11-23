from pathlib import Path

from roboquant.models.incremental_timeseries import block_score, iter_learning_blocks

ROOT = Path(__file__).resolve().parents[1]


def test_each_block_is_scored_before_it_can_be_learned():
    blocks = list(iter_learning_blocks(10, 4, 3))
    assert blocks == [(4, 7), (7, 10)]
    learned_through = 4
    for start, end in blocks:
        assert start == learned_through
        learned_through = end
    assert learned_through == 10


def test_daily_runner_does_not_train_model_b():
    workflow = (ROOT / ".github/workflows/premarket-research-forecast.yml").read_text()
    assert "run_incremental_index_learner.py" not in workflow
    assert "run_reliable_index_forecast.py" in workflow
    assert "scripts/collect_applied_model_inputs.py" in workflow
    assert workflow.index("Collect applied model inputs") < workflow.index("Freeze actual preparation inputs")


def test_block_score_reports_probability_error():
    scored = block_score([True, False], [0.8, 0.2])
    assert scored["n"] == 2
    assert scored["direction_hit"] == 1.0
    assert scored["brier"] < 0.05
