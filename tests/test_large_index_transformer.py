from __future__ import annotations

import pytest


def test_large_index_transformer_learns_one_cpu_step() -> None:
    torch = pytest.importorskip("torch")
    from roboquant.models.large_index_transformer import create_large_index_model

    model = create_large_index_model(
        3,
        {"patch_len": 2, "stride": 1, "d_model": 12, "n_heads": 3, "num_layers": 1, "dropout": 0.0},
    )
    features = torch.randn(4, 6, 3)
    labels = torch.tensor([0.0, 1.0, 0.0, 1.0])
    optimizer = torch.optim.AdamW(model.parameters(), lr=1e-3)
    loss_fn = torch.nn.BCEWithLogitsLoss()
    optimizer.zero_grad()
    loss = loss_fn(model(features), labels)
    loss.backward()
    optimizer.step()
    assert float(loss.detach()) > 0
    assert sum(parameter.numel() for parameter in model.parameters()) > 100
