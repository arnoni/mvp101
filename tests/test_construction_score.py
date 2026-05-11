from app.services.construction_score import construction_score


class TestConstructionScore:
    """Unit tests for the pure construction_score function."""

    def test_all_zeros(self):
        assert construction_score([0, 0, 0, 0], 0, 0.0) == 0

    def test_max_score(self):
        assert construction_score([100, 100, 100, 100], 500, 100.0) == 100

    def test_only_close_pois(self):
        # 5 POIs within 0-10km: raw_weight=20, tier_score=min(70,200)=70, ambient=0
        assert construction_score([5, 0, 0, 0], 0, 0.0) == 70

    def test_only_ambient(self):
        # tier_counts all zero, grid_count=50, p99=100
        # ambient = min(30, round(30 * 50 / 100)) = 15
        assert construction_score([0, 0, 0, 0], 50, 100.0) == 15

    def test_full_ambient(self):
        # grid_count = 3 * p99 → ambient capped at 30
        assert construction_score([0, 0, 0, 0], 300, 100.0) == 30

    def test_zero_p99_protection(self):
        assert construction_score([10, 10, 0, 0], 50, 0.0) == 70

    def test_combined_capped_at_100(self):
        # tier=70 + ambient=30 = 100
        assert construction_score([10, 0, 0, 0], 300, 100.0) == 100

    def test_fractional_ambient(self):
        # round(30 * 25/100) = round(7.5) = 8
        assert construction_score([0, 0, 0, 0], 25, 100.0) == 8

    def test_tier_score_caps_before_ambient(self):
        # raw tier score caps at 70, then ambient adds round(30 * 10 / 100) = 3.
        assert construction_score([20, 5, 2, 1], 10, 100.0) == 73
