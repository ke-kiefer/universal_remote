"""Leave-one-out evaluation over data/labeled/.

    uv run python -m app.eval                       # current .env settings
    uv run python -m app.eval --input-size 448x336  # try other settings (index untouched)
    uv run python -m app.eval --scales 1.0,0.5 --pooling cls

Each labeled image is used as a query against all other labeled images. Note: images
shot in quick succession are near-duplicates and make the numbers look better than real
usage. The more varied the training images, the more meaningful the result.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime

import numpy as np

from .config import Settings
from .index import Thresholds, VectorIndex, classify
from .storage import is_feedback_image

TARGET_PRECISION = 0.95


def evaluate(index: VectorIndex, settings: Settings) -> dict:
    counts = Counter(dict(zip(index.files, index.entities, strict=True)).values())
    usable = {e for e, n in counts.items() if n >= 2}
    if len(counts) < 2 or not usable:
        return {"error": "Zu wenig Daten: mindestens 2 Entities, davon eine mit ≥ 2 Bildern."}

    file_rows: dict[str, list[int]] = {}
    for i, f in enumerate(index.files):
        file_rows.setdefault(f, []).append(i)

    th = Thresholds(settings.match_threshold, settings.margin_threshold, settings.unknown_threshold)
    results = []  # (true, predicted, top_score, margin, top3, status)
    for f, rows in file_rows.items():
        true = index.entities[rows[0]]
        if true not in usable:
            continue
        cands = index.entity_scores(index.vectors[rows], settings.top_m, exclude={f})
        if not cands:
            continue
        margin = cands[0].score - (cands[1].score if len(cands) > 1 else 0.0)
        results.append(
            (
                true,
                cands[0].entity_id,
                cands[0].score,
                margin,
                [c.entity_id for c in cands[:3]],
                classify(cands, th),
            )
        )

    n = len(results)
    correct = np.array([r[0] == r[1] for r in results])
    scores = np.array([r[2] for r in results])
    margins = np.array([r[3] for r in results])
    top3 = np.mean([r[0] in r[4] for r in results])

    confusions = Counter((r[0], r[1]) for r in results if r[0] != r[1])
    per_entity = {}
    for e in sorted(usable):
        mask = np.array([r[0] == e for r in results])
        if mask.any():
            per_entity[e] = {"n": int(mask.sum()), "accuracy": float(correct[mask].mean())}

    current = {}
    for status in ("match", "uncertain", "unknown"):
        mask = np.array([r[5] == status for r in results])
        current[status] = {
            "share": float(mask.mean()),
            "correct": float(correct[mask].mean()) if mask.any() else None,
        }

    return {
        "created": datetime.now().isoformat(timespec="seconds"),
        "config": index.config,
        "top_m": settings.top_m,
        "n_images": n,
        "n_feedback": sum(1 for f in set(index.files) if is_feedback_image(f)),
        "n_entities": len(usable),
        "top1_accuracy": float(correct.mean()),
        "top3_accuracy": float(top3),
        "per_entity": per_entity,
        "confusions": [
            {"true": t, "predicted": p, "count": c} for (t, p), c in confusions.most_common(20)
        ],
        "score_stats": {
            "correct": _stats(scores[correct]),
            "wrong": _stats(scores[~correct]),
        },
        "current_thresholds": current,
        "suggestion": suggest_thresholds(scores, margins, correct),
    }


def _stats(x: np.ndarray) -> dict | None:
    if not len(x):
        return None
    return {
        "min": round(float(x.min()), 3),
        "p10": round(float(np.percentile(x, 10)), 3),
        "median": round(float(np.median(x)), 3),
        "max": round(float(x.max()), 3),
    }


def suggest_thresholds(scores: np.ndarray, margins: np.ndarray, correct: np.ndarray) -> dict:
    """Pick (match, margin) that maximizes auto-match coverage at >= TARGET_PRECISION."""
    best = None
    for t in np.arange(0.30, 0.96, 0.01):
        for m in np.arange(0.0, 0.151, 0.005):
            sel = (scores >= t) & (margins >= m)
            if sel.sum() == 0:
                continue
            prec = correct[sel].mean()
            cov = sel.mean()
            if prec >= TARGET_PRECISION and (best is None or cov > best[2] + 1e-9):
                best = (t, m, cov, prec)
    # Without "nothing known in the picture" samples, unknown can only be estimated from
    # the low end of correct scores.
    unknown = float(np.percentile(scores[correct], 2)) - 0.05 if correct.any() else 0.4
    if best is None:
        return {
            "match_threshold": None,
            "margin_threshold": None,
            "unknown_threshold": round(unknown, 2),
            "note": f"keine Einstellung erreicht {TARGET_PRECISION:.0%} Präzision",
        }
    t, m, cov, prec = best
    return {
        "match_threshold": round(float(t), 2),
        "margin_threshold": round(float(m), 3),
        "unknown_threshold": round(unknown, 2),
        "note": f"{cov:.0%} direkte Treffer bei {prec:.0%} Präzision; "
        "unknown nur geschätzt (keine Negativbeispiele)",
    }


def save_report(report: dict, storage) -> None:
    (storage.model / "eval.json").write_text(json.dumps(report, indent=1), encoding="utf-8")


def print_report(r: dict) -> None:
    if r.get("error"):
        print(r["error"])
        return
    print(f"Config: {r['config']}  top_m={r['top_m']}")
    print(
        f"{r['n_images']} images, {r['n_entities']} entities "
        f"({r.get('n_feedback', 0)} feedback images in the index)"
    )
    print(f"Top-1 accuracy: {r['top1_accuracy']:.1%}   Top-3: {r['top3_accuracy']:.1%}")
    print(f"Scores correct: {r['score_stats']['correct']}")
    print(f"Scores wrong:   {r['score_stats']['wrong']}")
    print("Current thresholds:")
    for k, v in r["current_thresholds"].items():
        c = f"{v['correct']:.0%}" if v["correct"] is not None else "-"
        print(f"  {k:9s} {v['share']:6.1%}  correct {c}")
    print(f"Suggestion: {r['suggestion']}")
    if r["confusions"]:
        print("Confusions (true -> predicted):")
        for c in r["confusions"]:
            print(f"  {c['count']:3d}  {c['true']} -> {c['predicted']}")
    print("Per entity:")
    for e, v in r["per_entity"].items():
        print(f"  {v['accuracy']:6.1%}  n={v['n']:3d}  {e}")


def main() -> None:
    from .embedder import OnnxEmbedder, ensure_model
    from .recognizer import Recognizer
    from .storage import Storage, migrate_legacy_layout

    ap = argparse.ArgumentParser(description="Leave-one-out evaluation")
    ap.add_argument("--input-size", help="override MODEL_INPUT_SIZE, e.g. 448x336")
    ap.add_argument("--scales", help="override CROP_SCALES, e.g. 1.0,0.5")
    ap.add_argument("--pooling", choices=["cls", "cls_mean"])
    ap.add_argument("--top-m", type=int)
    ap.add_argument("--no-save", action="store_true", help="do not write data/model/eval.json")
    ap.add_argument(
        "--manual-only",
        action="store_true",
        help="ignore training images that came from the remote's feedback (*_fb.jpg)",
    )
    args = ap.parse_args()

    overrides = {
        "model_input_size": args.input_size,
        "crop_scales": args.scales,
        "pooling": args.pooling,
        "top_m": args.top_m,
    }
    base = Settings()
    settings = Settings(**{**base.model_dump(), **{k: v for k, v in overrides.items() if v}})
    migrate_legacy_layout(settings.data_dir)
    storage = Storage(settings.data_dir / settings.adapter)
    model_path = ensure_model(settings.model_name, settings.data_dir / "model" / "weights")
    emb = OnnxEmbedder(
        settings.model_name, model_path, settings.input_wh, settings.scales, settings.pooling
    )
    saved = VectorIndex.load(storage.model)
    if saved is not None and saved.config == emb.config_key:
        index = saved
    else:
        print("Building temporary index for this configuration ...")
        rec = Recognizer(settings, storage, emb)
        items = [(e, storage.rel(p), p) for e, p in storage.labeled_images()]
        index = VectorIndex.build(items, rec.embed, emb.config_key, emb.scales)
    if args.manual_only:
        index = index.without_files(is_feedback_image)
    report = evaluate(index, settings)
    print_report(report)
    is_current = all(v is None for v in overrides.values()) and not args.manual_only
    if not args.no_save and is_current:
        save_report(report, storage)


if __name__ == "__main__":
    main()
