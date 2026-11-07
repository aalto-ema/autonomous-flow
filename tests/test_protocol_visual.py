import unittest

from engine.protocol_visual import ACTIONS, ProtocolTracker, classify, preview_protocol


class ProtocolVisualTests(unittest.TestCase):
    def test_every_action_has_a_valid_vector_icon(self):
        from xml.etree import ElementTree

        from ui.qt.protocol_view import ASSETS

        for action in ACTIONS:
            self.assertEqual(
                ElementTree.parse(ASSETS / (action + ".svg")).getroot().tag,
                "{http://www.w3.org/2000/svg}svg",
            )

    def test_brief_steps_remain_visible_and_liquid_survives_route_change(self):
        tracker = ProtocolTracker()
        tracker.routes = {
            "Stock A": "Recorded reagent • batch 12",
            "Mixing vessel": "Mixture",
        }
        for name in [
            "select Stock A (stroke 1)",
            "aspirate liquid (mixture transfer 1/2)",
            "select Mixing vessel",
            "dispense liquid (mixture transfer 1/2)",
        ]:
            tracker.observe(name, "sy01b", "started")
            tracker.observe(name, "sy01b", "completed")
        steps = tracker.snapshot()
        self.assertEqual(len(steps), 4)
        self.assertTrue(all(s.status == "completed" for s in steps))
        self.assertEqual(steps[-1].liquid, "Recorded reagent • batch 12")
        self.assertEqual(steps[-1].route, "Stock A → Mixing vessel")

    def test_failure_does_not_mark_delivery_completed(self):
        tracker = ProtocolTracker()
        tracker.observe("dispense liquid (mixture transfer 1/2)", "sy01b", "started")
        tracker.observe("dispense liquid (mixture transfer 1/2)", "sy01b", "failed")
        self.assertEqual(tracker.snapshot()[-1].status, "failed")

    def test_water_air_and_measurement_are_distinct(self):
        self.assertEqual(classify("aspirate stopped-flow chase water"), "chase_water")
        self.assertEqual(
            classify("aspirate flow-cell chase air from outlet"), "chase_air"
        )
        self.assertEqual(classify("Measure cyclic voltammetry"), "measure")

    def test_stopped_preview_includes_repeated_measurements_and_final_water_rinse(self):
        from types import SimpleNamespace

        p = SimpleNamespace(
            sources=(),
            mixture_transfers=(),
            workflow_kind="stopped_flow",
            measurement_count=3,
            between_measurement_displacement_volume_ml=0.25,
            measurement_type="chronoamperometry",
            measurement_settings=SimpleNamespace(equilibration_time_s=1),
            cleanup_settings=SimpleNamespace(
                mixing_rinse_cycles=2,
                mixing_rinse_volume_ml=1,
                flow_cell_rinse_through_volume_ml=1,
            ),
        )
        steps = preview_protocol(p)
        actions = [s.action for s in steps]
        self.assertEqual(actions.count("measure"), 3)
        self.assertEqual(actions.count("displace"), 2)
        self.assertIn("rinse_cell", actions)
        self.assertNotIn("chase_air", actions)
