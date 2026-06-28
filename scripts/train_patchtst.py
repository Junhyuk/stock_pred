#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from roboquant.config import ensure_project_dirs, get_database_path, load_config
from roboquant.data.loaders import load_modeling_dataset
from roboquant.datasets.sequence_dataset import (
    build_sequence_arrays,
    chronological_split_ranges,
    fit_sequence_normalizer,
    prepare_sequence_frame,
)
from roboquant.db import connect_database
from roboquant.models.patchtst import create_patchtst_model
from roboquant.registry.model_registry import register_feature_set, register_model


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train PatchTST shadow model.")
    parser.add_argument("--config", default="configs/train_patchtst.yaml")
    parser.add_argument("--feature-set-config", default="configs/feature_set_v1.yaml")
    parser.add_argument("--horizon", default="3M")
    parser.add_argument("--max-epochs", type=int, default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        import torch
        from torch.utils.data import DataLoader, TensorDataset
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "PyTorch is not installed. Install GPU extras first: "
            "pip install torch --index-url https://download.pytorch.org/whl/cu121 && "
            "pip install -e '.[gpu]'"
        ) from exc

    train_config = _load_yaml(args.config)
    feature_set = _load_yaml(args.feature_set_config)
    project_config = load_config(_rooted(train_config.get("config", "configs/poc.yaml")))
    ensure_project_dirs(project_config)
    conn = connect_database(get_database_path(project_config))

    model_cfg = train_config.get("model", {})
    split_cfg = train_config.get("split", {})
    model_name = str(model_cfg.get("model_name", "patchtst_v1_lookback252"))
    feature_columns = list(feature_set.get("features", []))
    feature_set_name = str(feature_set.get("feature_set_name", model_cfg.get("feature_set_name", "feature_set_v1")))
    horizon = args.horizon
    lookback = int(model_cfg.get("lookback", 252))

    dataset = load_modeling_dataset(conn, horizon)
    frame = prepare_sequence_frame(dataset, horizon, feature_columns, label_column=str(model_cfg.get("label_name", "is_top20pct")))
    ranges = chronological_split_ranges(frame, split_cfg)
    if not ranges:
        raise ValueError("No chronological split ranges could be created")
    fill_values, scale_values = fit_sequence_normalizer(
        frame,
        feature_columns=[column for column in feature_columns if column in frame.columns],
        start_date=ranges["train"]["start"],
        end_date=ranges["train"]["end"],
    )
    train_arrays = build_sequence_arrays(
        frame,
        feature_columns,
        lookback,
        ranges["train"]["start"],
        ranges["train"]["end"],
        fill_values,
        scale_values,
    )
    valid_arrays = build_sequence_arrays(
        frame,
        feature_columns,
        lookback,
        ranges["valid"]["start"],
        ranges["valid"]["end"],
        fill_values,
        scale_values,
    )
    test_arrays = build_sequence_arrays(
        frame,
        feature_columns,
        lookback,
        ranges["test"]["start"],
        ranges["test"]["end"],
        fill_values,
        scale_values,
    )
    min_train_samples = int(model_cfg.get("min_train_samples", 200))
    if len(train_arrays.y) < min_train_samples:
        raise ValueError(f"Not enough train samples for PatchTST: {len(train_arrays.y)} < {min_train_samples}")

    device = _device(torch, str(model_cfg.get("device", "auto")))
    use_amp = device.type == "cuda" and str(model_cfg.get("precision", "")).startswith("16")
    model = create_patchtst_model(len(train_arrays.feature_columns), model_cfg).to(device)
    optimizer = torch.optim.AdamW(
        model.parameters(),
        lr=float(model_cfg.get("learning_rate", 5e-4)),
        weight_decay=float(model_cfg.get("weight_decay", 0.01)),
    )
    criterion = torch.nn.BCEWithLogitsLoss()
    scaler = torch.cuda.amp.GradScaler(enabled=use_amp)
    max_epochs = int(args.max_epochs or model_cfg.get("max_epochs", 50))
    patience = int(model_cfg.get("early_stopping_patience", 7))
    batch_size = int(model_cfg.get("batch_size", 128))
    train_loader = _loader(torch, TensorDataset, DataLoader, train_arrays, batch_size, shuffle=False)
    valid_loader = _loader(torch, TensorDataset, DataLoader, valid_arrays, batch_size, shuffle=False)

    best_loss = float("inf")
    best_state = None
    wait = 0
    for epoch in range(1, max_epochs + 1):
        train_loss = _train_epoch(torch, model, train_loader, optimizer, criterion, scaler, device, use_amp)
        valid_loss = _eval_loss(torch, model, valid_loader, criterion, device, use_amp)
        print(f"epoch={epoch} train_loss={train_loss:.6f} valid_loss={valid_loss:.6f}")
        if valid_loss < best_loss:
            best_loss = valid_loss
            best_state = {key: value.detach().cpu() for key, value in model.state_dict().items()}
            wait = 0
        else:
            wait += 1
            if wait >= patience:
                break
    if best_state is not None:
        model.load_state_dict(best_state)

    test_loader = _loader(torch, TensorDataset, DataLoader, test_arrays, batch_size, shuffle=False)
    test_prob = _predict_prob(torch, model, test_loader, device, use_amp)
    metrics = _metrics(test_arrays.y, test_prob)
    metrics.update(
        {
            "model_name": model_name,
            "horizon": horizon,
            "best_valid_loss": best_loss,
            "train_samples": int(len(train_arrays.y)),
            "valid_samples": int(len(valid_arrays.y)),
            "test_samples": int(len(test_arrays.y)),
            "device": str(device),
        }
    )

    artifact_dir = _rooted(train_config.get("paths", {}).get("artifact_dir", "models/dnn")) / model_name
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / "model.pt"
    metadata = {
        "model_name": model_name,
        "horizon": horizon,
        "feature_columns": train_arrays.feature_columns,
        "fill_values": train_arrays.fill_values,
        "scale_values": train_arrays.scale_values,
        "lookback": lookback,
        "model_config": dict(model_cfg),
        "split": _serializable_ranges(ranges),
        "metrics": metrics,
    }
    torch.save({"state_dict": model.state_dict(), "metadata": metadata}, artifact_path)
    (artifact_dir / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")

    register_feature_set(
        conn,
        feature_set_name=feature_set_name,
        feature_list=train_arrays.feature_columns,
        status=str(feature_set.get("status", "production")),
        description=feature_set.get("description"),
    )
    register_model(
        conn,
        model_name=model_name,
        model_type=str(model_cfg.get("model_type", "patchtst")),
        feature_set_name=feature_set_name,
        label_name=str(model_cfg.get("label_name", "is_top20pct")),
        horizons=[horizon],
        artifact_path=str(artifact_path),
        metrics=metrics,
        split={key + "_" + bound: value[bound] for key, value in ranges.items() for bound in ("start", "end")},
        status="experimental",
        production_weight=0.0,
        shadow_mode=True,
    )
    print(f"saved artifact: {artifact_path}")
    print(metrics)


def _train_epoch(torch, model, loader, optimizer, criterion, scaler, device, use_amp: bool) -> float:
    model.train()
    losses = []
    for x, y in loader:
        x = x.to(device)
        y = y.to(device)
        optimizer.zero_grad(set_to_none=True)
        with torch.amp.autocast(device_type=device.type, enabled=use_amp):
            logits = model(x)
            loss = criterion(logits, y)
        scaler.scale(loss).backward()
        scaler.step(optimizer)
        scaler.update()
        losses.append(float(loss.detach().cpu()))
    return float(sum(losses) / max(len(losses), 1))


def _eval_loss(torch, model, loader, criterion, device, use_amp: bool) -> float:
    if len(loader.dataset) == 0:
        return float("inf")
    model.eval()
    losses = []
    with torch.no_grad():
        for x, y in loader:
            x = x.to(device)
            y = y.to(device)
            with torch.amp.autocast(device_type=device.type, enabled=use_amp):
                losses.append(float(criterion(model(x), y).detach().cpu()))
    return float(sum(losses) / max(len(losses), 1))


def _predict_prob(torch, model, loader, device, use_amp: bool):
    model.eval()
    probs = []
    with torch.no_grad():
        for x, _ in loader:
            x = x.to(device)
            with torch.amp.autocast(device_type=device.type, enabled=use_amp):
                probs.append(torch.sigmoid(model(x)).detach().cpu())
    if not probs:
        return []
    return torch.cat(probs).numpy()


def _loader(torch, tensor_dataset_cls, data_loader_cls, arrays, batch_size: int, shuffle: bool):
    x = torch.tensor(arrays.x, dtype=torch.float32)
    y = torch.tensor(arrays.y, dtype=torch.float32)
    return data_loader_cls(tensor_dataset_cls(x, y), batch_size=batch_size, shuffle=shuffle)


def _metrics(y_true, pred_prob) -> dict[str, float | int | None]:
    if len(y_true) == 0:
        return {"rows": 0, "precision_at20": None, "hit_ratio": None}
    order = sorted(range(len(pred_prob)), key=lambda idx: pred_prob[idx], reverse=True)[:20]
    precision_at20 = float(sum(float(y_true[idx]) for idx in order) / max(len(order), 1))
    return {
        "rows": int(len(y_true)),
        "precision_at20": precision_at20,
        "hit_ratio": float(sum(float(value) for value in y_true) / len(y_true)),
    }


def _device(torch, requested: str):
    if requested == "auto":
        return torch.device("cuda" if torch.cuda.is_available() else "cpu")
    return torch.device(requested)


def _serializable_ranges(ranges: dict) -> dict:
    return {
        split: {bound: str(value[bound].date()) for bound in ("start", "end")}
        for split, value in ranges.items()
    }


def _load_yaml(path: str | Path) -> dict:
    with _rooted(path).open("r", encoding="utf-8") as file:
        return yaml.safe_load(file) or {}


def _rooted(path: str | Path) -> Path:
    path = Path(path)
    return path if path.is_absolute() else ROOT / path


if __name__ == "__main__":
    main()
