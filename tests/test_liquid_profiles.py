import unittest

from admet.engines.acquisition.fluidics.config import FLUIDIC_CHANNEL_UNITS, FLUIDIC_CHANNELS
from admet.engines.acquisition.fluidics.liquids import (
    channel_profile,
    check_liquid,
    check_profile,
    dead_volume,
    default_liquids,
    default_profiles,
    parse_profiles,
    remember_dead_volume,
    remember_profile,
    unique_id,
)
from admet.engines.acquisition.settings import (
    CORRECTION_SETTINGS,
    LIQUID_PROFILE_PARAM_NAMES,
)


def default_profile(profile_id):
    return next(p for p in default_profiles() if p.id == profile_id)


class DefaultTests(unittest.TestCase):
    def test_defaults_load_and_every_profile_names_a_default_liquid(self):
        liquids = {liquid.id for liquid in default_liquids()}

        self.assertTrue(liquids and default_profiles())
        for profile in default_profiles():
            self.assertTrue(profile.default)
            self.assertIn(profile.unit, {"L", "M"})
            self.assertIn(profile.liquid, liquids)
            self.assertGreater(profile.scale, 0)

    def test_channel_defaults_match_a_profile(self):
        # A channel starts on the default profile that matches its built-in correction.
        for prefix, _label, calibration, scale, _offset, _quadratic in FLUIDIC_CHANNELS:
            param = next(p for p in CORRECTION_SETTINGS.params if p.name == f"{prefix}_profile")
            profile = default_profile(str(param.default))

            self.assertEqual(profile.unit, FLUIDIC_CHANNEL_UNITS[prefix])
            self.assertEqual((profile.calibration, profile.scale), (calibration, scale))

    def test_channel_only_offers_profiles_for_its_own_unit(self):
        for prefix, *_rest in FLUIDIC_CHANNELS:
            param = next(p for p in CORRECTION_SETTINGS.params if p.name == f"{prefix}_profile")
            offered = {option.value for option in param.options}

            self.assertEqual(offered, {p.id for p in default_profiles() if p.unit == FLUIDIC_CHANNEL_UNITS[prefix]})

    def test_profile_params_cover_every_channel(self):
        self.assertEqual(set(LIQUID_PROFILE_PARAM_NAMES), {f"{prefix}_profile" for prefix, *_rest in FLUIDIC_CHANNELS})


class ProfileTests(unittest.TestCase):
    def test_a_profile_sends_its_square_and_cube_as_the_engine_s_offset_and_quadratic(self):
        profile = parse_profiles([{"id": "l_dsurf", "name": "L dSurf", "liquid": "dsurf", "unit": "l",
                                   "calibration": "IPA", "scale": 0.739, "square": 0.0127}])[0]

        self.assertEqual((profile.unit, profile.cube, profile.default), ("L", 0.0, False))
        self.assertEqual(profile.corrections("oil_l"), {"oil_l_calibration": "IPA", "oil_l_scale": 0.739,
                                                        "oil_l_offset": 0.0127, "oil_l_quadratic": 0.0})

    def test_a_profile_is_checked(self):
        good = {"name": "M1 water", "liquid": "water", "unit": "m", "calibration": "H2O", "scale": 0.975}
        self.assertEqual(check_profile(good, {"water"})["unit"], "M")
        for change in ({"name": " "}, {"liquid": "honey"}, {"unit": "S"}, {"calibration": "Honey"},
                       {"scale": 0}, {"square": float("nan")}, {"scale": "x"}):
            with self.assertRaises(ValueError, msg=change):
                check_profile({**good, **change}, {"water"})

    def test_a_liquid_is_checked(self):
        self.assertEqual(check_liquid({"name": " dSurf ", "density": 1.61})["name"], "dSurf")
        for change in ({"name": ""}, {"density": 0}, {"density": float("inf")}, {"viscosity": -1},
                       {"viscosity": float("nan")}, {"density": "x"}):
            with self.assertRaises(ValueError, msg=change):
                check_liquid({"name": "x", "density": 1.0, "viscosity": 1.0, **change})

    def test_new_ids_never_clash(self):
        self.assertEqual(unique_id("M1 · Water", set()), "m1_water")
        self.assertEqual(unique_id("Water", {"water", "water_2"}), "water_3")


class ChannelTests(unittest.TestCase):
    def test_a_channel_keeps_its_profile_and_its_dead_volume_separately(self):
        saved = remember_profile({}, "cells_m", "water_m")
        saved = remember_dead_volume(saved, "cells_m", 85.0)
        saved = remember_profile(saved, "cells_m", "oil_m")            # liquid changes, tubing does not

        self.assertEqual(saved, {"cells_m": {"profile": "oil_m", "dead_volume_ul": 85.0}})
        self.assertEqual((channel_profile(saved, "cells_m"), dead_volume(saved, "cells_m")), ("oil_m", 85.0))
        self.assertEqual((channel_profile(saved, "beads_m"), dead_volume(saved, "beads_m")), ("", 0.0))


if __name__ == "__main__":
    unittest.main()
