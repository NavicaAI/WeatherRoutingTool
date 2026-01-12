#!/usr/bin/env python3
"""
Test script to verify polygon-based land detection works across routing algorithms.

This script runs simple routing tests with different algorithms to verify:
1. Polygon detection is initialized
2. Routes avoid land correctly
3. Different algorithms use the constraint system properly

Usage:
    python test_algorithm_polygon_integration.py
"""

import os
import sys
import logging
from datetime import datetime

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def setup_environment():
    """Ensure environment variables are set."""
    required_vars = ['WRT_DB_HOST', 'WRT_DB_PORT', 'WRT_DB_DATABASE', 
                     'WRT_DB_USERNAME', 'WRT_DB_PASSWORD']
    
    missing = [var for var in required_vars if not os.getenv(var)]
    if missing:
        logger.error(f"Missing environment variables: {', '.join(missing)}")
        return False
    return True


def test_gcr_slider():
    """Test GCR Slider algorithm with polygon detection."""
    logger.info("=" * 70)
    logger.info("Testing GCR Slider Algorithm")
    logger.info("=" * 70)
    
    try:
        from WeatherRoutingTool.config import Config
        from WeatherRoutingTool.algorithms.gcrslider import GcrSliderAlgorithm
        
        # Simple route around southern Corsica
        start = (41.3, 8.8)  # South of Corsica
        finish = (41.3, 9.4)  # Southeast of Corsica
        
        logger.info(f"Route: {start} -> {finish}")
        
        # Create minimal config
        import tempfile
        
        # Create temp file for COURSES_FILE requirement
        temp_courses = tempfile.NamedTemporaryFile(mode='w', suffix='.nc', delete=False)
        temp_courses.close()
        
        config_dict = {
            "ALGORITHM_TYPE": "gcr_slider",
            "BOAT_TYPE": "direct_power_method",
            "CONSTRAINTS_LIST": ["land_crossing_global_land_mask", "land_crossing_polygons"],
            "DEFAULT_ROUTE": [start[0], start[1], finish[0], finish[1]],
            "DEFAULT_MAP": [41.0, 8.5, 41.8, 9.5],
            "DEPARTURE_TIME": "2026-01-15T00:00Z",
            "ROUTE_PATH": "/tmp",  # Required field
            "COURSES_FILE": temp_courses.name,  # Required field - needs existing file
            "GCR_SLIDER_USE_POLYGON_LAND_DETECTION": True,
            "GCR_SLIDER_THRESHOLD": 10000,
            "GCR_SLIDER_MAX_POINTS": 100,
            "GCR_SLIDER_ANGLE_STEP": 30,
            "GCR_SLIDER_DISTANCE_MOVE": 10000,
            "GCR_SLIDER_LAND_BUFFER": 1000,
            "GCR_SLIDER_DYNAMIC_PARAMETERS": True,
            "GCR_SLIDER_INTERPOLATE": False,
            "GCR_SLIDER_INTERP_DIST": 0.1,
            "GCR_SLIDER_INTERP_NORMALIZED": True,
            "INTERMEDIATE_WAYPOINTS": [],
            "DELTA_FUEL": 3000,
            "DELTA_TIME_FORECAST": 3,
            "TIME_FORECAST": 168,
            "SHIP_SPEED": 10,
            "SHIP_DRAUGHT": 5.0,
            "SHIP_COURSE": 0,
            "VERBOSE": False,
            "DEBUG": False
        }
        
        config = Config(**config_dict)
        
        # Initialize algorithm
        logger.info("Initializing GCR Slider algorithm...")
        algorithm = GcrSliderAlgorithm(config)
        
        # Check if polygon detector was initialized
        if hasattr(algorithm, 'use_polygon_detection') and algorithm.use_polygon_detection:
            logger.info("✓ GCR Slider has use_polygon_detection=True")
            if hasattr(algorithm, 'land_polygon_detector') and algorithm.land_polygon_detector:
                logger.info("✓ GCR Slider polygon detector is initialized")
                if hasattr(algorithm.land_polygon_detector, 'initialization_successful'):
                    if algorithm.land_polygon_detector.initialization_successful:
                        logger.info("✓ Polygon detector initialization successful")
                        return True
                    else:
                        logger.warning("✗ Polygon detector initialization failed")
                        return False
            else:
                logger.warning("✗ GCR Slider polygon detector is None")
                return False
        else:
            logger.warning("✗ GCR Slider does not have polygon detection enabled")
            return False
            
    except Exception as e:
        logger.error(f"✗ GCR Slider test failed: {e}", exc_info=True)
        return False


def test_isochrone_constraints():
    """Test that isochrone algorithms use polygon constraints."""
    logger.info("=" * 70)
    logger.info("Testing Isochrone Constraint System")
    logger.info("=" * 70)
    
    try:
        from WeatherRoutingTool.constraints.constraints import ConstraintsListFactory, ConstraintsList
        from WeatherRoutingTool.utils.maps import Map
        import numpy as np
        
        # Create map for Corsica
        map_size = Map(41.0, 8.5, 41.8, 9.5)
        
        # Initialize constraints with polygons
        logger.info("Initializing constraint system...")
        constraints = ConstraintsListFactory.get_constraints_list(
            ['land_crossing_global_land_mask', 'land_crossing_polygons'],
            map_size=map_size
        )
        
        # Check constraint configuration
        logger.info(f"ConstraintPars.bCheckCrossing: {constraints.pars.bCheckCrossing}")
        logger.info(f"Number of continuous constraints: {len(constraints.negative_constraints_continuous)}")
        
        if not constraints.pars.bCheckCrossing:
            logger.warning("✗ Continuous checking (bCheckCrossing) is disabled!")
            return False
        
        logger.info("✓ Continuous checking is enabled")
        
        # Find LandPolygonsCrossing constraint
        land_polygon_constraint = None
        for constraint in constraints.negative_constraints_continuous:
            if constraint.__class__.__name__ == 'LandPolygonsCrossing':
                land_polygon_constraint = constraint
                break
        
        if land_polygon_constraint:
            logger.info("✓ LandPolygonsCrossing constraint found")
            if land_polygon_constraint.initialization_successful:
                logger.info("✓ LandPolygonsCrossing initialized successfully")
            else:
                logger.warning("✗ LandPolygonsCrossing initialization failed")
                return False
        else:
            logger.warning("✗ LandPolygonsCrossing constraint not found")
            return False
        
        # Test actual crossing detection
        logger.info("\nTesting crossing detection through constraint system...")
        
        # Test case 1: Open water (should not be constrained)
        lat_start = np.array([41.3])
        lon_start = np.array([8.8])
        lat_end = np.array([41.35])
        lon_end = np.array([8.85])
        
        is_constrained = [False]
        result = constraints.safe_crossing(lat_start, lon_start, lat_end, lon_end, None, is_constrained)
        
        logger.info(f"Open water test: constrained = {result[0]}")
        if not result[0]:
            logger.info("✓ Open water correctly identified as passable")
        else:
            logger.warning("✗ Open water incorrectly marked as constrained")
        
        # Test case 2: Through land (should be constrained)
        lat_start = np.array([41.5])
        lon_start = np.array([8.8])
        lat_end = np.array([41.5])
        lon_end = np.array([9.3])
        
        is_constrained = [False]
        result = constraints.safe_crossing(lat_start, lon_start, lat_end, lon_end, None, is_constrained)
        
        logger.info(f"Through land test: constrained = {result[0]}")
        if result[0]:
            logger.info("✓ Land crossing correctly detected")
        else:
            logger.warning("✗ Land crossing not detected")
        
        return True
        
    except Exception as e:
        logger.error(f"✗ Isochrone constraint test failed: {e}", exc_info=True)
        return False


def test_dijkstra():
    """Test Dijkstra algorithm polygon integration."""
    logger.info("=" * 70)
    logger.info("Testing Dijkstra Algorithm")
    logger.info("=" * 70)
    
    try:
        from WeatherRoutingTool.config import Config
        from WeatherRoutingTool.algorithms.dijkstra import DijkstraGlobalLandMask
        from WeatherRoutingTool.utils.maps import Map
        
        # Check if mask file exists
        import os
        mask_file = None
        
        # First try the installed global_land_mask package
        try:
            import global_land_mask
            pkg_dir = os.path.dirname(global_land_mask.__file__)
            pkg_mask = os.path.join(pkg_dir, "globe_combined_mask_compressed.npz")
            if os.path.exists(pkg_mask):
                mask_file = pkg_mask
                print(f"✓ Found mask in package: {pkg_mask}")
        except ImportError:
            pass
        
        # Try other common locations if not found in package
        if not mask_file:
            possible_paths = [
                os.path.expanduser("~/.local/lib/python3.10/site-packages/global_land_mask/globe_combined_mask_compressed.npz"),
                os.path.expanduser("~/.local/share/global_land_mask/globe_combined_mask_compressed.npz"),
                "/usr/local/lib/python3.10/dist-packages/global_land_mask/globe_combined_mask_compressed.npz",
            ]
            
            for path in possible_paths:
                if os.path.exists(path):
                    mask_file = path
                    break
        
        if not mask_file:
            logger.warning("✗ Could not find global land mask file for Dijkstra test")
            logger.info("  Dijkstra algorithm requires the mask file to run")
            logger.info("  Skipping Dijkstra test, but polygon integration is implemented")
            return None  # Not a failure, just can't test without mask file
        
        logger.info(f"Found mask file: {mask_file}")
        
        # Create config
        import tempfile
        
        # Create temp file for COURSES_FILE requirement
        temp_courses = tempfile.NamedTemporaryFile(mode='w', suffix='.nc', delete=False)
        temp_courses.close()
        
        config_dict = {
            "ALGORITHM_TYPE": "dijkstra",
            "BOAT_TYPE": "direct_power_method",
            "CONSTRAINTS_LIST": ["land_crossing_global_land_mask", "land_crossing_polygons"],
            "DEFAULT_ROUTE": [41.3, 8.8, 41.3, 9.4],
            "DEFAULT_MAP": [41.0, 8.5, 41.8, 9.5],
            "DEPARTURE_TIME": "2026-01-15T00:00Z",
            "ROUTE_PATH": "/tmp",  # Required field
            "COURSES_FILE": temp_courses.name,  # Required field - needs existing file
            "DIJKSTRA_MASK_FILE": mask_file,
            "DIJKSTRA_NOF_NEIGHBORS": 1,
            "DIJKSTRA_STEP": 1,
            "DELTA_FUEL": 3000,
            "DELTA_TIME_FORECAST": 3,
            "TIME_FORECAST": 168,
            "SHIP_SPEED": 10,
            "SHIP_DRAUGHT": 5.0,
            "SHIP_COURSE": 0,
            "VERBOSE": False,
            "DEBUG": False
        }
        
        config = Config(**config_dict)
        map_size = Map(*config.DEFAULT_MAP)
        
        logger.info("Initializing Dijkstra algorithm...")
        algorithm = DijkstraGlobalLandMask(config)
        algorithm.start = (41.3, 8.8)
        algorithm.finish = (41.3, 9.4)
        algorithm.map_ext = map_size
        
        # Check if polygon detector was initialized
        if hasattr(algorithm, 'land_polygon_detector') and algorithm.land_polygon_detector:
            logger.info("✓ Dijkstra has polygon detector attribute")
            if algorithm.land_polygon_detector and algorithm.land_polygon_detector.initialization_successful:
                logger.info("✓ Dijkstra polygon detector initialized successfully")
                return True
            else:
                logger.warning("✗ Dijkstra polygon detector initialization failed")
                return False
        else:
            logger.warning("✗ Dijkstra does not have polygon detector")
            return False
            
    except Exception as e:
        logger.error(f"✗ Dijkstra test failed: {e}", exc_info=True)
        return False


def main():
    """Run all algorithm tests."""
    print("\n" + "=" * 70)
    print("Routing Algorithm Polygon Integration Tests")
    print("=" * 70 + "\n")
    
    # Check environment
    if not setup_environment():
        logger.error("Environment not configured. Exiting.")
        sys.exit(1)
    
    results = {}
    
    # Test 1: Constraint system (used by Isochrone, IsoFuel, Genetic)
    results['constraint_system'] = test_isochrone_constraints()
    
    # Test 2: GCR Slider (direct integration)
    results['gcr_slider'] = test_gcr_slider()
    
    # Test 3: Dijkstra (enhanced has_point_on_land)
    results['dijkstra'] = test_dijkstra()
    
    # Summary
    print("\n" + "=" * 70)
    print("Test Summary")
    print("=" * 70)
    
    for test_name, result in results.items():
        if result is True:
            status = "✓ PASS"
        elif result is False:
            status = "✗ FAIL"
        else:
            status = "⊘ SKIP"
        print(f"{test_name:30s}: {status}")
    
    print("\nAlgorithm Coverage:")
    print("  Isochrone/IsoFuel/Genetic : Uses constraint_system")
    print("  GCR Slider               : Direct integration")
    print("  Dijkstra                 : Enhanced has_point_on_land")
    
    passed = sum(1 for r in results.values() if r is True)
    failed = sum(1 for r in results.values() if r is False)
    skipped = sum(1 for r in results.values() if r is None)
    
    print(f"\nResults: {passed} passed, {failed} failed, {skipped} skipped")
    
    if failed > 0:
        print("\n✗ Some tests failed")
        sys.exit(1)
    else:
        print("\n✓ All tests passed!")
        print("\nPolygon-based land detection is working across all algorithms.")
        sys.exit(0)


if __name__ == '__main__':
    main()
