"""Simulation execution service wrapping public behaviorsim==1.0.1 API."""

import logging
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from behaviorsim import Simulator

from app.core.errors import BehaviorSimAPIError

logger = logging.getLogger("behaviorsim_api.services.simulation")

# Explicit mapping of supported public presets to internal identifiers
# and public domain metadata.
SUPPORTED_PRESETS: Dict[str, Dict[str, Any]] = {
    "education": {
        "canonical_name": "education",
        "description": "Adaptive learning telemetry modeling cognitive load, accuracy, response times, and student mastery.",
        "default_profile": "average",
        "supported_profiles": [
            "average",
            "fast_accurate",
            "fast_inaccurate",
            "slow_accurate",
            "slow_inaccurate",
        ],
        "supported_states": ["Optimal", "Overload", "Underload"],
    },
    "finance": {
        "canonical_name": "finance",
        "description": "Synthetic behavioral telemetry for financial trading, risk alerts, drawdowns, and portfolio volatility.",
        "default_profile": "balanced_investor",
        "supported_profiles": [
            "conservative_investor",
            "balanced_investor",
            "growth_investor",
            "active_trader",
        ],
        "supported_states": [
            "Stable",
            "Active",
            "Volatile",
            "Drawdown",
            "Recovered",
            "Closed",
        ],
    },
    "healthcare": {
        "canonical_name": "healthcare",
        "description": "Synthetic patient monitoring telemetry tracking vital trends, alerts, and mobility trajectories.",
        "default_profile": "stable_patient",
        "supported_profiles": [
            "stable_patient",
            "chronic_risk",
            "post_operative",
            "geriatric_frail",
        ],
        "supported_states": [
            "Baseline",
            "Elevated",
            "Critical",
            "Recovery",
            "Discharged",
        ],
    },
    "mobile_app": {
        "canonical_name": "mobile_app",
        "description": "User engagement simulation tracking session duration, navigation depth, actions, and checkout flows.",
        "default_profile": "casual_browser",
        "supported_profiles": [
            "power_user",
            "casual_browser",
            "deal_seeker",
            "infrequent_visitor",
        ],
        "supported_states": [
            "Browsing",
            "ActiveSession",
            "CheckoutFlow",
            "Idle",
            "Churned",
        ],
    },
}

# Supported aliases for developer convenience
PRESET_ALIASES: Dict[str, str] = {
    "mobile": "mobile_app",
}


def normalize_preset_name(preset_name: str) -> str:
    """Normalize input preset name resolving aliases, or return the normalized name."""
    cleaned = preset_name.strip().lower()
    return PRESET_ALIASES.get(cleaned, cleaned)


def list_available_presets() -> List[Dict[str, Any]]:
    """Return public metadata summaries for all supported simulation presets."""
    summaries = []
    for name, meta in SUPPORTED_PRESETS.items():
        summaries.append({
            "name": name,
            "description": meta["description"],
            "available": True,
            "default_profile": meta["default_profile"],
            "supported_profiles": meta["supported_profiles"],
            "supported_states": meta["supported_states"],
        })
    return summaries


def get_preset_metadata(preset_name: str) -> Dict[str, Any]:
    """Retrieve detailed metadata for a specific preset name or alias."""
    canonical = normalize_preset_name(preset_name)
    if canonical not in SUPPORTED_PRESETS:
        raise BehaviorSimAPIError(
            message=f"Preset '{preset_name}' is not recognized. Supported presets: {list(SUPPORTED_PRESETS.keys()) + list(PRESET_ALIASES.keys())}",
            status_code=404,
            details={
                "code": "preset_not_found",
                "requested": preset_name,
                "supported": list(SUPPORTED_PRESETS.keys()) + list(PRESET_ALIASES.keys()),
            },
        )
    meta = SUPPORTED_PRESETS[canonical]
    return {
        "name": canonical,
        "description": meta["description"],
        "available": True,
        "default_profile": meta["default_profile"],
        "supported_profiles": meta["supported_profiles"],
        "supported_states": meta["supported_states"],
    }


def execute_simulation(
    preset: str,
    num_interactions: int,
    seed: Optional[int] = None,
    profile: Optional[str] = None,
    initial_state: Optional[str] = None,
) -> Tuple[str, List[Dict[str, Any]], int]:
    """Execute a simulation using public behaviorsim.Simulator and return serializable records.

    Returns:
        Tuple of (simulation_id: str, data_records: List[Dict[str, Any]], compute_ms: int)
    """
    canonical_preset = normalize_preset_name(preset)
    if canonical_preset not in SUPPORTED_PRESETS:
        raise BehaviorSimAPIError(
            message=f"Unsupported preset '{preset}'. Available presets: {list(SUPPORTED_PRESETS.keys()) + list(PRESET_ALIASES.keys())}",
            status_code=400,
            details={
                "code": "unsupported_preset",
                "requested": preset,
                "supported": list(SUPPORTED_PRESETS.keys()) + list(PRESET_ALIASES.keys()),
            },
        )

    preset_meta = SUPPORTED_PRESETS[canonical_preset]

    # Validate profile if provided
    if profile is not None and profile not in preset_meta["supported_profiles"]:
        raise BehaviorSimAPIError(
            message=f"Unknown profile '{profile}' for preset '{canonical_preset}'. Supported: {preset_meta['supported_profiles']}",
            status_code=400,
            details={
                "code": "invalid_profile",
                "preset": canonical_preset,
                "requested_profile": profile,
                "supported_profiles": preset_meta["supported_profiles"],
            },
        )

    # Validate initial_state if provided
    if initial_state is not None and initial_state not in preset_meta["supported_states"]:
        raise BehaviorSimAPIError(
            message=f"Unknown initial_state '{initial_state}' for preset '{canonical_preset}'. Supported: {preset_meta['supported_states']}",
            status_code=400,
            details={
                "code": "invalid_initial_state",
                "preset": canonical_preset,
                "requested_state": initial_state,
                "supported_states": preset_meta["supported_states"],
            },
        )

    simulator_kwargs: Dict[str, Any] = {}
    if profile is not None:
        simulator_kwargs["profile"] = profile
    if initial_state is not None:
        simulator_kwargs["initial_state"] = initial_state

    simulation_id = str(uuid.uuid4())
    start_time = time.perf_counter()

    try:
        simulator = Simulator.from_preset(canonical_preset, **simulator_kwargs)
        df = simulator.generate(num_interactions=num_interactions, seed=seed)
    except (ValueError, TypeError) as exc:
        logger.warning(
            "BehaviorSim configuration validation failed for preset=%s: %s",
            canonical_preset,
            exc,
        )
        raise BehaviorSimAPIError(
            message=f"Invalid simulation parameters: {exc}",
            status_code=400,
            details={"code": "invalid_simulation_request", "reason": str(exc)},
        ) from exc
    except Exception as exc:
        logger.error(
            "Unexpected error during BehaviorSim simulation execution: %s",
            exc,
            exc_info=True,
        )
        raise BehaviorSimAPIError(
            message="Internal simulation generation failed. Please try again or contact support.",
            status_code=500,
            details={"code": "simulation_generation_failed"},
        ) from exc

    elapsed_ms = max(1, int((time.perf_counter() - start_time) * 1000))

    # Convert pandas DataFrame into pure Python native JSON-serializable records
    records = df.to_dict(orient="records")

    logger.info(
        "Simulation %s completed: preset=%s, interactions=%s, seed=%s, compute_ms=%s",
        simulation_id,
        canonical_preset,
        len(records),
        seed,
        elapsed_ms,
    )

    return simulation_id, records, elapsed_ms
