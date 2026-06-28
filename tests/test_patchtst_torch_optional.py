from __future__ import annotations

import pytest


def test_patchtst_tiny_cpu_training_step_if_torch_available() -> None:
    torch = pytest.importorskip("torch")

    from roboquant.models.patchtst import create_patchtst_model

    model = create_patchtst_model(
        feature_dim=3,
        config={
            "patch_len": 2,
            "stride": 1,
            "d_model": 16,
            "n_heads": 4,
            "num_layers": 1,
            "dropout": 0.1,
        },
    )
    x = torch.randn(4, 6, 3)
    y = torch.tensor([0.0, 1.0, 0.0, 1.0])
    optimizer = torch.optim.AdamW(model.parameters(), lr=1e-3)
    criterion = torch.nn.BCEWithLogitsLoss()

    optimizer.zero_grad()
    loss = criterion(model(x), y)
    loss.backward()
    optimizer.step()

    assert float(loss.detach()) > 0
