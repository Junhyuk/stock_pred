#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import get_database_path, load_config
from roboquant.data.loaders import load_features, load_modeling_dataset
from roboquant.datasets.sequence_dataset import build_sequence_arrays, prepare_sequence_frame
from roboquant.db import connect_database
from roboquant.models.patchtst import create_patchtst_model
from roboquant.registry.model_registry import upsert_model_predictions


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate PatchTST shadow predictions.")
    parser.add_argument("--config", default="configs/train_patchtst.yaml")
    parser.add_argument("--horizon", default="3M")
    parser.add_argument("--model-name", default=None)
    parser.add_argument("--latest", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        import torch
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "PyTorch is not installed. Install GPU extras first: "
            "pip install torch --index-url https://download.pytorch.org/whl/cu121 && "
            "pip install -e '.[gpu]'"
        ) from exc

    train_config = _load_yaml(args.config)
    project_config = load_config(_rooted(train_config.get("config", "configs/poc.yaml")))
    conn = connect_database(get_database_path(project_config))
    model_cfg = train_config.get("model", {})
    model_name = args.model_name or str(model_cfg.get("model_name", "patchtst_v1_lookback252"))
    artifact_dir = _rooted(train_config.get("paths", {}).get("artifact_dir", "models/dnn")) / model_name
    artifact_path = artifact_dir / "model.pt"
    checkpoint = torch.load(artifact_path, map_location="cpu")
    metadata = checkpoint["metadata"]
    feature_columns = metadata["feature_columns"]
    horizon = args.horizon

    if args.latest:
        raw = load_features(conn, horizon)
        raw["is_top20pct"] = 0.0
        frame = prepare_sequence_frame(raw, horizon, feature_columns)
        latest_date = pd.to_datetime(frame["date"]).max()
        arrays = build_sequence_arrays(
            frame,
            feature_columns,
            int(metadata["lookback"]),
            start_date=latest_date,
            end_date=latest_date,
            fill_values=metadata["fill_values"],
            scale_values=metadata["scale_values"],
        )
    else:
        dataset = load_modeling_dataset(conn, horizon)
        frame = prepare_sequence_frame(dataset, horizon, feature_columns)
        split = metadata["split"]["test"]
        arrays = build_sequence_arrays(
            frame,
            feature_columns,
            int(metadata["lookback"]),
            start_date=split["start"],
            end_date=split["end"],
            fill_values=metadata["fill_values"],
            scale_values=metadata["scale_values"],
        )

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = create_patchtst_model(len(feature_columns), metadata["model_config"]).to(device)
    model.load_state_dict(checkpoint["state_dict"])
    probs = _predict(torch, model, arrays.x, device)
    predictions = arrays.meta[["date", "symbol", "horizon"]].copy()
    predictions["pred_score"] = probs
    predictions["pred_prob"] = probs
    written = upsert_model_predictions(conn, predictions, model_name)
    print(f"model_predictions rows written: {len(written)}")


def _predict(torch, model, x, device):
    if len(x) == 0:
        return []
    model.eval()
    tensor = torch.tensor(x, dtype=torch.float32, device=device)
    outputs = []
    with torch.no_grad():
        for start in range(0, len(tensor), 512):
            batch = tensor[start : start + 512]
            outputs.append(torch.sigmoid(model(batch)).detach().cpu())
    return torch.cat(outputs).numpy()


def _load_yaml(path: str | Path) -> dict:
    with _rooted(path).open("r", encoding="utf-8") as file:
        return yaml.safe_load(file) or {}


def _rooted(path: str | Path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else ROOT / path


if __name__ == "__main__":
    main()
