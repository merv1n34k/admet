import unittest

from admet.engines.acquisition.fluidics.config import FLUIDIC_CHANNEL_UNITS, FLUIDIC_CHANNELS
from admet.engines.acquisition.fluidics.liquids import (
    load_profiles,
    profile_by_id,
    profiles_for_unit,
    dead_volume,
    new_liquid,
    parse_profiles,
    remember_corrections,
    remember_dead_volume,
    rig_corrections,
)
from admet.engines.acquisition.settings import (
    CORRECTION_SETTINGS,
    LIQUID_PROFILE_PARAM_NAMES,
)


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

    def test_profile_params_cover_every_channel(self):
        self.assertEqual(
            set(LIQUID_PROFILE_PARAM_NAMES),
            {f"{prefix}_profile" for prefix, *_rest in FLUIDIC_CHANNELS},
        )


if __name__ == "__main__":
    unittest.main()


class RigCorrectionTests(unittest.TestCase):
    def test_each_liquid_keeps_its_own_values_on_a_channel(self):
        water, oil = profile_by_id("water_m"), profile_by_id("oil_m")
        edited = {**water.corrections("cells_m"), "cells_m_scale": 1.07}
        saved = remember_corrections({}, "cells_m", "water_m", edited, "2026-10-02T20:00:00")
        saved = remember_corrections(saved, "cells_m", "oil_m")            # switch liquid, no edit

        self.assertEqual(saved["cells_m"]["profile"], "oil_m")
        self.assertEqual(rig_corrections(saved, "cells_m", water),
                         ({**water.corrections("cells_m"), "cells_m_scale": 1.07}, "2026-10-02T20:00:00"))
        self.assertEqual(rig_corrections(saved, "cells_m", oil), (oil.corrections("cells_m"), ""))
        self.assertEqual(rig_corrections(saved, "beads_m", water), (water.corrections("beads_m"), ""))

    def test_dead_volume_belongs_to_the_channel_not_the_liquid(self):
        saved = remember_corrections({}, "cells_m", "water_m")
        saved = remember_dead_volume(saved, "cells_m", 85.0)
        saved = remember_corrections(saved, "cells_m", "oil_m")            # liquid changes, tubing does not

        self.assertEqual(dead_volume(saved, "cells_m"), 85.0)
        self.assertEqual(dead_volume(saved, "beads_m"), 0.0)


class ProjectLiquidTests(unittest.TestCase):
    def test_a_new_liquid_is_an_uncorrected_profile_for_its_unit(self):
        entry = new_liquid("dSurf", "m", "H2O", 1.0, 1.1, taken={"water_m"})
        profile = parse_profiles([entry])[0]

        self.assertEqual((profile.id, profile.name, profile.unit), ("dsurf_m", "dSurf", "M"))
        self.assertEqual((profile.calibration, profile.scale, profile.density), ("H2O", 1.0, 1.0))
        self.assertEqual(new_liquid("dSurf", "M", "H2O", 1.0, 1.1, taken={"dsurf_m"})["id"], "dsurf_m_2")

    def test_a_new_liquid_is_checked(self):
        for args in (("", "M", "H2O", 1.0), ("x", "S", "H2O", 1.0), ("x", "M", "Honey", 1.0), ("x", "M", "H2O", 0)):
            with self.assertRaises(ValueError):
                new_liquid(*args, 0.0, taken=set())
