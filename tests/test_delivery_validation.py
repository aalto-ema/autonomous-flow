import tempfile
import unittest
from pathlib import Path
from scripts.validate_delivery import summarize


class DeliveryValidationTests(unittest.TestCase):
    def test_bias_and_variation_use_measured_mass(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "measured.csv"
            path.write_text(
                "source,workflow,rpm,requested_ml,commanded_ml,mass_before_g,mass_after_g,density_g_ml\nwater,stopped_flow,10,1,1.01,10,11,1\nwater,stopped_flow,10,1,1.01,10,11.02,1\n"
            )
            row = list(summarize(path))[0]
            self.assertEqual(row["n"], 2)
            self.assertAlmostEqual(row["mean_delivered_ml"], 1.01)
            self.assertAlmostEqual(row["bias_vs_requested_percent"], 1)
            self.assertAlmostEqual(row["bias_vs_commanded_percent"], 0)
            self.assertGreater(row["cv_percent"], 0)
