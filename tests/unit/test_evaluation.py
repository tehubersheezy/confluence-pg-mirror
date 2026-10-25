import copy
import json

from confluence_mirror import evaluation
from confluence_mirror.extraction.registry import load_registry

from ..conftest import FIXTURES

DATASET = json.loads((FIXTURES / "evaluation" / "synthetic_v1.json").read_text())


def test_rules_meet_targets_on_the_synthetic_set():
    report = evaluation.run_dataset(DATASET, load_registry())
    assert report["meets_targets"] and report["split"] == "development"
    assert report["overall"]["record_recall"] == 1.0
    assert set(report["by_family"]) == {
        "inventory",
        "key_value",
        "layout",
        "ownership_mapping",
        "relationship_mapping",
    }


def test_wrong_labels_show_up_and_compare_reports_them():
    base = evaluation.run_dataset(DATASET, load_registry())
    wrong = copy.deepcopy(DATASET)
    wrong["cases"][0]["expected"]["records"][0]["hostname"] = "bill99"
    worse = evaluation.run_dataset(wrong, load_registry())
    assert worse["overall"]["record_recall"] < 1.0 and not worse["meets_targets"]
    diff = evaluation.compare(base, worse)
    assert diff["overall_delta"]["record_recall"] < 0
    assert diff["changed_cases"][0]["case_id"] == "inventory-rowspan"
