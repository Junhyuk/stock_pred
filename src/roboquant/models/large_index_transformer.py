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


def create_series_token_model(feature_dim: int, lookback: int, config: dict[str, Any] | None = None):
    """Treat every input series as its own sequence of time tokens.

    Each feature is patched along time. Those patches are the tokens, and
    every series is presented to the same encoder.
    """
    import torch
    from torch import nn

    settings = config or {}

    class SeriesTokenTransformer(nn.Module):
        def __init__(self):
            super().__init__()
            self.patch_len = int(settings.get("patch_len", 16))
            self.stride = int(settings.get("stride", 16))
            if self.patch_len > int(lookback):
                raise ValueError("patch_len cannot exceed lookback")
            self.n_patches = (int(lookback) - self.patch_len) // self.stride + 1
            d_model = int(settings.get("d_model", 512))
            n_heads = int(settings.get("n_heads", 8))
            num_layers = int(settings.get("num_layers", 12))
            dropout = float(settings.get("dropout", 0.1))
            if d_model % n_heads != 0:
                raise ValueError("d_model must be divisible by n_heads")
            self.patch_proj = nn.Linear(self.patch_len, d_model)
            self.series_embed = nn.Embedding(int(feature_dim), d_model)
            self.pos_embed = nn.Parameter(torch.zeros(1, self.n_patches, d_model))
            self.cls = nn.Parameter(torch.zeros(1, 1, d_model))
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
            if x.ndim != 3 or x.shape[1] < self.patch_len or x.shape[2] != self.series_embed.num_embeddings:
                raise ValueError("input must have shape [batch, lookback, feature_dim]")
            series = x.transpose(1, 2)
            patches = series.unfold(dimension=2, size=self.patch_len, step=self.stride)
            tokens = self.patch_proj(patches.contiguous())
            tokens = tokens + self.pos_embed[:, : tokens.shape[2], :]
            ids = torch.arange(tokens.shape[1], device=x.device)
            tokens = tokens + self.series_embed(ids)[None, :, None, :]
            tokens = tokens.flatten(1, 2)
            cls = self.cls.expand(tokens.shape[0], -1, -1)
            hidden = torch.cat([cls, tokens], dim=1)
            encoded = self.norm(self.encoder(hidden)[:, 0, :])
            return self.head(encoded).squeeze(-1)

    return SeriesTokenTransformer()
