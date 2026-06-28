from __future__ import annotations

from typing import Any


def create_patchtst_model(feature_dim: int, config: dict[str, Any]):
    """Create a lightweight PatchTST-style classifier.

    Torch is imported lazily so the rest of the project works without the optional
    GPU extra installed.
    """
    import torch
    from torch import nn

    class PatchTSTClassifier(nn.Module):
        def __init__(self):
            super().__init__()
            self.patch_len = int(config.get("patch_len", 16))
            self.stride = int(config.get("stride", 8))
            d_model = int(config.get("d_model", 128))
            n_heads = int(config.get("n_heads", 8))
            num_layers = int(config.get("num_layers", 4))
            dropout = float(config.get("dropout", 0.2))
            self.patch_proj = nn.Linear(int(feature_dim) * self.patch_len, d_model)
            self.pos_embedding = nn.Parameter(torch.zeros(1, 512, d_model))
            encoder_layer = nn.TransformerEncoderLayer(
                d_model=d_model,
                nhead=n_heads,
                dim_feedforward=d_model * 4,
                dropout=dropout,
                activation="gelu",
                batch_first=True,
                norm_first=True,
            )
            self.encoder = nn.TransformerEncoder(encoder_layer, num_layers=num_layers)
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
                raise ValueError("PatchTST input must have shape [batch, lookback, feature_dim]")
            patches = x.unfold(dimension=1, size=self.patch_len, step=self.stride)
            # unfold returns [batch, patches, feature_dim, patch_len].
            patches = patches.contiguous().view(patches.shape[0], patches.shape[1], -1)
            hidden = self.patch_proj(patches)
            if hidden.shape[1] > self.pos_embedding.shape[1]:
                raise ValueError("Too many patches for positional embedding")
            hidden = hidden + self.pos_embedding[:, : hidden.shape[1], :]
            encoded = self.encoder(hidden)
            pooled = self.norm(encoded.mean(dim=1))
            return self.head(pooled).squeeze(-1)

    return PatchTSTClassifier()
