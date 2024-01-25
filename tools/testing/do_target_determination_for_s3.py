import pathlib
import sys
import json
from typing import Dict, List

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))
from tools.testing.test_run import TestRun, TestRuns
from tools.stats.import_test_stats import (
    get_test_class_times,
    get_test_file_ratings,
    get_test_times,
    get_test_class_ratings,
    get_td_heuristic_historial_edited_files_json,
    get_td_heuristic_profiling_json,
    copy_pytest_cache,
)

from tools.testing.discover_tests import TESTS
from tools.testing.target_determination.determinator import (
    AggregatedHeuristics,
    get_test_prioritizations,
    TestPrioritizations,
)


def to_json_writeable_dict(
    test_prioritizations: TestPrioritizations,
) -> Dict[str, List[str]]:
    def testrun_to_json_serializable(testruns: TestRuns) -> Dict[str, List[str]]:
        return [
            {
                "test_file": testrun.test_file,
                "excluded": list(testrun.excluded()),
                "included": list(testrun.included()),
            }
            for testrun in testruns
        ]

    return {
        "high": testrun_to_json_serializable(
            test_prioritizations.get_high_relevance_tests()
        ),
        "probable": testrun_to_json_serializable(
            test_prioritizations.get_probable_relevance_tests()
        ),
        "unranked": testrun_to_json_serializable(
            test_prioritizations.get_unranked_relevance_tests()
        ),
        "unlikely": testrun_to_json_serializable(
            test_prioritizations.get_unlikely_relevance_tests()
        ),
        "none": testrun_to_json_serializable(
            test_prioritizations.get_none_relevance_tests()
        ),
    }


def main() -> None:
    selected_tests = TESTS

    aggregated_heuristics: AggregatedHeuristics = AggregatedHeuristics(
        unranked_tests=selected_tests
    )

    get_test_times()
    get_test_class_times()
    get_test_file_ratings()
    get_test_class_ratings()
    get_td_heuristic_historial_edited_files_json()
    get_td_heuristic_profiling_json()
    copy_pytest_cache()

    aggregated_heuristics = get_test_prioritizations(selected_tests)

    test_prioritizations = aggregated_heuristics.get_aggregated_priorities()

    with open(REPO_ROOT / "td_results.json", "w") as f:
        f.write(json.dumps(to_json_writeable_dict(test_prioritizations), indent=2))


if __name__ == "__main__":
    main()
