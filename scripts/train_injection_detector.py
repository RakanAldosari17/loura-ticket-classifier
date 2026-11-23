"""Train the prompt-injection detector and report how well it does (9.6, 9.7).

Run from the project root:

    python scripts/train_injection_detector.py

It reads the committed CSVs, trains with a fixed seed, writes the model file,
then prints the test-set report and the score for each of the ten sample
tickets. The model file is committed, so running the service never needs this
script; it exists so the model can be rebuilt and checked.

--- Provenance of the training data -----------------------------------------

The dataset is `deepset/prompt-injections` from Hugging Face (Apache-2.0):
546 training rows and 116 test rows, columns `text` and `label`, where label 1
is an injection and label 0 is not. It was downloaded once with the commands
below and the CSVs were committed, so neither training nor running the service
needs the internet. `datasets` is deliberately not a project dependency, so the
download happens in a throwaway environment:

    python3 -m venv /tmp/hf-download
    /tmp/hf-download/bin/pip install datasets
    /tmp/hf-download/bin/python - <<'PY'
    from datasets import load_dataset
    dataset = load_dataset("deepset/prompt-injections")
    dataset["train"].to_csv("data/injection/train.csv", index=False)
    dataset["test"].to_csv("data/injection/test.csv", index=False)
    PY
    rm -rf /tmp/hf-download

Run on 23 Nov 2026; it reported 546 train and 116 test rows, matching the
dataset page.
"""

import csv
import json
import sys
from pathlib import Path

import joblib
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import confusion_matrix, precision_recall_fscore_support
from sklearn.pipeline import FeatureUnion, Pipeline

# The project root, so the script works from anywhere.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import scoring  # noqa: E402
from app.injection_detector import (  # noqa: E402
    DETECTOR_PATH,
    TRAIN_CSV,
    TEST_CSV,
    detector_points,
)

# Fixed, so the same data always produces the same model. Without it, two runs
# of this script would give slightly different models and the numbers in the
# README would not be reproducible.
#
# Reproducible means the model *behaves* identically: two runs give byte-for-byte
# identical probabilities (checked on the 116 test rows and the 10 samples, max
# difference 0.0). The saved .joblib files are not byte-identical, which is a
# detail of how joblib serialises, not a difference in the model.
RANDOM_SEED = 42


def load_rows(path: Path) -> tuple[list[str], list[int]]:
    """Read one CSV into texts and labels."""
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    return [row["text"] for row in rows], [int(row["label"]) for row in rows]


def build_model() -> Pipeline:
    """TF-IDF over words and characters, feeding a logistic regression (9.6).

    Two vectorisers side by side:
    - word 1-2 grams catch the phrasing of an instruction ("ignore all")
    - character 3-5 grams catch the same thing through spacing tricks, typos and
      words the model never saw in training, because they look at overlapping
      runs of characters rather than whole words

    `class_weight="balanced"` matters because the training data has more
    legitimate rows than injections; without it the model would learn to favour
    the commoner answer.
    """
    features = FeatureUnion(
        [
            ("word", TfidfVectorizer(analyzer="word", ngram_range=(1, 2), min_df=1)),
            ("char", TfidfVectorizer(analyzer="char", ngram_range=(3, 5), min_df=2)),
        ]
    )
    classifier = LogisticRegression(
        max_iter=2000,
        class_weight="balanced",
        random_state=RANDOM_SEED,
    )
    return Pipeline([("features", features), ("classifier", classifier)])


def report_test_set(model: Pipeline) -> None:
    """Print precision, recall, F1 and the confusion matrix (9.7, step 1)."""
    texts, labels = load_rows(TEST_CSV)
    predicted = model.predict(texts)

    precision, recall, f1, _ = precision_recall_fscore_support(
        labels, predicted, average="binary", pos_label=1, zero_division=0
    )
    true_neg, false_pos, false_neg, true_pos = confusion_matrix(
        labels, predicted, labels=[0, 1]
    ).ravel()

    print(f"\nTest set ({len(texts)} rows), injection = positive")
    print("-" * 56)
    print(f"  precision  {precision:.3f}   of those called injections, how many were")
    print(f"  recall     {recall:.3f}   of the real injections, how many were caught")
    print(f"  f1         {f1:.3f}")
    print("\n  confusion matrix")
    print("                    predicted clean   predicted injection")
    print(f"    actual clean          {true_neg:>5}               {false_pos:>5}")
    print(f"    actual injection      {false_neg:>5}               {true_pos:>5}")


def report_samples(model: Pipeline) -> list[str]:
    """Score the ten sample tickets (9.7, step 2).

    Returns the ids of any legitimate sample that would be rejected, which is
    the condition the gate fails on.
    """
    samples = json.loads((ROOT / "data" / "sample_tickets.json").read_text())

    print(f"\nSample tickets ({len(samples)}), L1b score = phrases + detector")
    print("-" * 72)
    print(f"  {'id':8} {'prob':>6} {'det':>4} {'phr':>4} {'tot':>4}  verdict")

    wrongly_rejected = []
    for sample in samples:
        text = scoring.ticket_text(sample["subject"], sample["body"])
        probability = float(model.predict_proba([text])[0][1])
        detector = detector_points(probability)
        phrases = scoring.phrase_score(text)
        total = detector + phrases

        verdict = (
            "REJECT"
            if scoring.is_rejected(total)
            else ("flag" if scoring.is_flagged(total) else "clean")
        )
        marker = ""
        if sample["id"] == "t-1005":
            marker = "  <- must be REJECT"
            if not scoring.is_rejected(total):
                wrongly_rejected.append("t-1005 was NOT rejected")
        elif scoring.is_rejected(total):
            marker = "  <- LEGITIMATE TICKET REJECTED"
            wrongly_rejected.append(sample["id"])

        print(
            f"  {sample['id']:8} {probability:>6.3f} {detector:>4} "
            f"{phrases:>4} {total:>4}  {verdict}{marker}"
        )

    return wrongly_rejected


def main() -> int:
    """Train, save, report, and check the gate in 9.7."""
    texts, labels = load_rows(TRAIN_CSV)
    print(f"Training on {len(texts)} rows from {TRAIN_CSV.relative_to(ROOT)}")

    model = build_model()
    model.fit(texts, labels)

    DETECTOR_PATH.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, DETECTOR_PATH)
    print(f"Model written to {DETECTOR_PATH.relative_to(ROOT)}")

    report_test_set(model)
    problems = report_samples(model)

    print()
    if problems:
        print("GATE FAILED (9.7):", ", ".join(problems))
        print(
            "Stop and show Rakan the probabilities above. Do not tune the"
            " cut-offs, the features or the thresholds to make this pass."
        )
        return 1

    print("Gate passed (9.7): t-1005 rejected, no legitimate sample rejected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
