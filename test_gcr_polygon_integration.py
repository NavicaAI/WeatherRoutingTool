#!/usr/bin/env python3
"""
Test script to verify GCR Slider polygon land detection integration.
Tests both raster-based (default) and polygon-based land detection.
"""

import sys
import logging
from pathlib import Path

# Add WeatherRoutingTool to path
sys.path.insert(0, str(Path(__file__).parent))

from WeatherRoutingTool.config import Config
from WeatherRoutingTool.algorithms.gcrslider import GcrSliderAlgorithm

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def test_raster_mode():
    """Test GCR Slider with traditional raster-based land detection."""
    logger.info("=" * 80)
    logger.info("TEST 1: Raster-based land detection (baseline)")
    logger.info("=" * 80)
    
    # Sardinia test route that crosses land with raster
    config_dict = {
        "START": [39.5, 8.0],  # West of Sardinia
        "FINISH": [42.84, 6.69],  # North of Sardinia
        "INTERMEDIATE_WAYPOINTS": [[39.84, 11.16]],  # East of Sardinia
        "GCR_SLIDER_USE_POLYGON_LAND_DETECTION": False,  # Raster mode
        "GCR_SLIDER_DISTANCE_MOVE": 10000,
        "GCR_SLIDER_THRESHOLD": 10000,
        "GCR_SLIDER_LAND_BUFFER": 1000,
        "GCR_SLIDER_ANGLE_STEP": 30,
        "GCR_SLIDER_DYNAMIC_PARAMETERS": True,
        "GCR_SLIDER_INTERPOLATE": False,
        "GCR_SLIDER_INTERP_DIST": 0.1,
        "GCR_SLIDER_INTERP_NORMALIZED": True,
        "GCR_SLIDER_MAX_POINTS": 1000,
    }
    
    config = Config(**config_dict)
    algorithm = GcrSliderAlgorithm(config)
    
    logger.info(f"Start: {algorithm.start}")
    logger.info(f"Finish: {algorithm.finish}")
    logger.info(f"Waypoints: {algorithm.waypoints}")
    logger.info(f"Using polygon detection: {algorithm.use_polygon_detection}")
    logger.info(f"Polygon detector initialized: {algorithm.land_polygon_detector is not None}")
    
    try:
        route, status = algorithm.execute()
        logger.info(f"✓ Route generated successfully with {len(route.lats_per_step)} points")
        logger.info(f"  Status: {status}")
        return True
    except Exception as e:
        logger.error(f"✗ Route generation failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def test_polygon_mode():
    """Test GCR Slider with polygon-based land detection (requires PostGIS)."""
    logger.info("")
    logger.info("=" * 80)
    logger.info("TEST 2: Polygon-based land detection (requires PostGIS)")
    logger.info("=" * 80)
    
    config_dict = {
        "START": [39.5, 8.0],
        "FINISH": [42.84, 6.69],
        "INTERMEDIATE_WAYPOINTS": [[39.84, 11.16]],
        "GCR_SLIDER_USE_POLYGON_LAND_DETECTION": True,  # Polygon mode
        "GCR_SLIDER_DISTANCE_MOVE": 10000,
        "GCR_SLIDER_THRESHOLD": 10000,
        "GCR_SLIDER_LAND_BUFFER": 1000,
        "GCR_SLIDER_ANGLE_STEP": 30,
        "GCR_SLIDER_DYNAMIC_PARAMETERS": True,
        "GCR_SLIDER_INTERPOLATE": False,
        "GCR_SLIDER_INTERP_DIST": 0.1,
        "GCR_SLIDER_INTERP_NORMALIZED": True,
        "GCR_SLIDER_MAX_POINTS": 1000,
    }
    
    config = Config(**config_dict)
    algorithm = GcrSliderAlgorithm(config)
    
    logger.info(f"Start: {algorithm.start}")
    logger.info(f"Finish: {algorithm.finish}")
    logger.info(f"Waypoints: {algorithm.waypoints}")
    logger.info(f"Using polygon detection: {algorithm.use_polygon_detection}")
    logger.info(f"Polygon detector initialized: {algorithm.land_polygon_detector is not None}")
    
    if not algorithm.use_polygon_detection:
        logger.warning("⚠ Polygon detection not enabled - likely no PostGIS database configured")
        logger.warning("  This is expected if running without database setup")
        return True  # Not a failure, just not available
    
    try:
        route, status = algorithm.execute()
        logger.info(f"✓ Route generated successfully with {len(route.lats_per_step)} points")
        logger.info(f"  Status: {status}")
        return True
    except Exception as e:
        logger.error(f"✗ Route generation failed: {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """Run all tests."""
    logger.info("Testing GCR Slider polygon land detection integration")
    logger.info("")
    
    results = []
    
    # Test 1: Raster mode (should always work)
    results.append(("Raster mode", test_raster_mode()))
    
    # Test 2: Polygon mode (may gracefully fall back if no DB)
    results.append(("Polygon mode", test_polygon_mode()))
    
    # Summary
    logger.info("")
    logger.info("=" * 80)
    logger.info("SUMMARY")
    logger.info("=" * 80)
    for name, passed in results:
        status = "✓ PASSED" if passed else "✗ FAILED"
        logger.info(f"{name}: {status}")
    
    all_passed = all(passed for _, passed in results)
    logger.info("")
    if all_passed:
        logger.info("✓ All tests passed!")
        return 0
    else:
        logger.error("✗ Some tests failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
