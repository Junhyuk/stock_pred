"""Large index-direction transformer sized for a 24 GB GPU.

This is a research shadow. It does not replace the linear next-session forecast.
"""
from __future__ import annotations

from typing import Any


def create_large_index_model(feature_dim: int, config: dict[str, Any] | None = None):
    """Build a deep patch transformer for next-session index direction."""
    import torch
    from torch import nn

    settings = config or {}

    class LargeIndexTransformer(nn.Module):
        def __init__(self):
            super().__init__()
            self.patch_len = int(settings.get("patch_len", 8))
            self.stride = int(settings.get("stride", 4))
            d_model = int(settings.get("d_model", 768))
            n_heads = int(settings.get("n_heads", 12))
            num_layers = int(settings.get("num_layers", 12))
            dropout = float(settings.get("dropout", 0.1))
            if d_model % n_heads != 0:
                raise ValueError("d_model must be divisible by n_heads")
            self.patch_proj = nn.Linear(int(feature_dim) * self.patch_len, d_model)
            self.cls = nn.Parameter(torch.zeros(1, 1, d_model))
            self.pos_embedding = nn.Parameter(torch.zeros(1, 256, d_model))
            layer = nn.TransformerEncoderLayer(
                d_model=d_model,
                nhead=n_heads,
                dim_feedforward=d_model * 4,
                dropout=dropout,
                activation="gelu",
                batch_first=True,
                norm_first=True,
            )
            self.encoder = nn.TransformerEncoder(layer, num_layers=num_layers)
            self.norm = nn.LayerNorm(d_model)
            self.head = nn.Sequential(
                nn.Dropout(dropout),
                nn.Linear(d_model, d_model // 2),
                nn.GELU(),
                nn.Dropout(dropout),
                nn.Linear(d_model // 2, 1),
            )

        def forward(self, x):
            if x.ndim != 3:
                raise ValueError("input must have shape [batch, lookback, feature_dim]")
            patches = x.unfold(dimension=1, size=self.patch_len, step=self.stride)
            patches = patches.contiguous().view(patches.shape[0], patches.shape[1], -1)
            hidden = self.patch_proj(patches)
            cls = self.cls.expand(hidden.shape[0], -1, -1)
            hidden = torch.cat([cls, hidden], dim=1)
            if hidden.shape[1] > self.pos_embedding.shape[1]:
                raise ValueError("too many patches for positional embedding")
            hidden = hidden + self.pos_embedding[:, : hidden.shape[1], :]
            encoded = self.norm(self.encoder(hidden)[:, 0, :])
            return self.head(encoded).squeeze(-1)

    return LargeIndexTransformer()
