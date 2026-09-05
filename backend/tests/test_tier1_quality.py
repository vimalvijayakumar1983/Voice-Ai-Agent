from tests.quality.tier1_fixtures import tier1_exact_fact_index, tier1_quality_cases
from tests.quality.tier1_harness import Tier1QualityHarness


def test_tier1_exact_fact_quality_matrix():
    index = tier1_exact_fact_index()
    harness = Tier1QualityHarness(index)

    for case in tier1_quality_cases(index):
        harness.assert_case(case)
