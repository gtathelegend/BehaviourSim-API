"""Tests verifying BehaviorSim core dependency availability."""

import pytest


def test_behaviorsim_import():
    """Verify behaviorsim package can be imported."""
    import behaviorsim

    assert hasattr(behaviorsim, "__version__")
    assert behaviorsim.__version__ == "1.0.1"


def test_behaviorsim_exports():
    """Verify expected core simulation classes are exposed by behaviorsim."""
    from behaviorsim import (
        Simulator,
        State,
        Profile,
        FeatureDistribution,
        TransitionRule,
        SimulationConfig,
    )

    assert Simulator is not None
    assert State is not None
    assert Profile is not None
    assert FeatureDistribution is not None
    assert TransitionRule is not None
    assert SimulationConfig is not None
