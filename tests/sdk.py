"""Skip tests that drive Fluigent's simulator when its SDK is not installed."""

import unittest

from admet.engines.acquisition.fluidics.sdk import FluigentSDK

needs_fluigent_sdk = unittest.skipUnless(FluigentSDK().preflight().available, "Fluigent SDK not installed")
