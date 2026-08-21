import unittest

from admet.engines.acquisition.fluidics.config import FLUIDIC_CHANNEL_UNITS, FLUIDIC_CHANNELS
from admet.engines.acquisition.fluidics.liquids import (
    load_profiles,
    profile_by_id,
    profiles_for_unit,
)
from admet.engines.acquisition.settings import (
    CORRECTION_SETTINGS,
    LIQUID_PROFILE_PARAM_NAMES,
)
from admet.workflows.control import create_control_workflow


class LiquidProfileTests(unittest.TestCase):
    def test_shipped_profiles_load(self):
        profiles = load_profiles()

        self.assertTrue(profiles)
        for profile in profiles:
            self.assertIn(profile.unit, {"L", "M"})
            self.assertGreater(profile.scale, 0)

    def test_profiles_are_offered_per_flow_unit(self):
        for unit in ("L", "M"):
            for profile in profiles_for_unit(unit):
                self.assertEqual(profile.unit, unit)
        self.assertNotEqual(profiles_for_unit("L"), profiles_for_unit("M"))

    def test_channel_defaults_match_a_profile(self):
        # Introducing profiles must not silently change any channel's corrections.
        for prefix, _label, calibration, scale, _offset, _quadratic in FLUIDIC_CHANNELS:
            param = next(p for p in CORRECTION_SETTINGS.params if p.name == f"{prefix}_profile")
            profile = profile_by_id(str(param.default))

            self.assertIsNotNone(profile, prefix)
            self.assertEqual(profile.unit, FLUIDIC_CHANNEL_UNITS[prefix])
            self.assertEqual(profile.calibration, calibration)
            self.assertEqual(profile.scale, scale)

    def test_channel_only_offers_profiles_for_its_own_unit(self):
        for prefix, *_rest in FLUIDIC_CHANNELS:
            param = next(p for p in CORRECTION_SETTINGS.params if p.name == f"{prefix}_profile")
            offered = {option.value for option in param.options}

            self.assertEqual(
                offered,
                {p.id for p in profiles_for_unit(FLUIDIC_CHANNEL_UNITS[prefix])},
            )

    def test_profile_writes_every_correction_term(self):
        profile = profile_by_id("ipa_l")

        self.assertEqual(
            profile.corrections("oil_l"),
            {
                "oil_l_calibration": profile.calibration,
                "oil_l_scale": profile.scale,
                "oil_l_offset": profile.offset,
                "oil_l_quadratic": profile.quadratic,
            },
        )

    def test_run_and_wash_declare_different_liquids(self):
        stages = {stage.id: stage for stage in create_control_workflow().stages}
        runs = stages["runs"].settings_options["liquids"]
        wash = stages["wash"].settings_options["liquids"]

        self.assertNotEqual(runs, wash)
        for declared in (runs, wash):
            for prefix, profile_id in declared.items():
                profile = profile_by_id(profile_id)
                self.assertIsNotNone(profile, profile_id)
                self.assertEqual(profile.unit, FLUIDIC_CHANNEL_UNITS[prefix])
        # the point of the feature: the oil line runs oil, then washes with IPA
        self.assertEqual(profile_by_id(runs["oil_l"]).scale, 2.25)
        self.assertEqual(profile_by_id(wash["oil_l"]).scale, 1.0)

    def test_profile_params_cover_every_channel(self):
        self.assertEqual(
            set(LIQUID_PROFILE_PARAM_NAMES),
            {f"{prefix}_profile" for prefix, *_rest in FLUIDIC_CHANNELS},
        )


if __name__ == "__main__":
    unittest.main()
