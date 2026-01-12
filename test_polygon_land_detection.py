#!/usr/bin/env python3
"""
Test script to verify polygon-based land detection setup.

This script tests:
1. Database connectivity
2. Land polygon loading
3. Simple intersection tests
4. Integration with constraints system

Usage:
    python test_polygon_land_detection.py
"""

import os
import sys
import logging
import numpy as np

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def check_environment():
    """Check if required environment variables are set."""
    logger.info("=" * 70)
    logger.info("Checking Environment Variables")
    logger.info("=" * 70)
    
    required_vars = ['WRT_DB_HOST', 'WRT_DB_PORT', 'WRT_DB_DATABASE', 
                     'WRT_DB_USERNAME', 'WRT_DB_PASSWORD']
    
    missing = []
    for var in required_vars:
        value = os.getenv(var)
        if value:
            # Mask password
            display_value = '***' if 'PASSWORD' in var else value
            logger.info(f"✓ {var}: {display_value}")
        else:
            logger.error(f"✗ {var}: NOT SET")
            missing.append(var)
    
    if missing:
        logger.error(f"\nMissing environment variables: {', '.join(missing)}")
        logger.error("Please set them before running this test.")
        logger.error("Example: export WRT_DB_HOST=localhost")
        return False
    
    logger.info("\n✓ All required environment variables are set\n")
    return True


def test_database_connection():
    """Test PostgreSQL/PostGIS database connection."""
    logger.info("=" * 70)
    logger.info("Testing Database Connection")
    logger.info("=" * 70)
    
    try:
        import sqlalchemy
        
        host = os.getenv('WRT_DB_HOST')
        port = os.getenv('WRT_DB_PORT')
        database = os.getenv('WRT_DB_DATABASE')
        user = os.getenv('WRT_DB_USERNAME')
        password = os.getenv('WRT_DB_PASSWORD')
        
        connection_string = f"postgresql://{user}:{password}@{host}:{port}/{database}"
        
        logger.info(f"Connecting to: {host}:{port}/{database}")
        engine = sqlalchemy.create_engine(connection_string, pool_pre_ping=True)
        
        with engine.connect() as conn:
            # Test basic query
            result = conn.execute(sqlalchemy.text("SELECT version()"))
            version = result.fetchone()[0]
            logger.info(f"✓ Connection successful!")
            logger.info(f"  PostgreSQL version: {version[:50]}...")
            
            # Check PostGIS
            result = conn.execute(sqlalchemy.text("SELECT PostGIS_Version()"))
            postgis_version = result.fetchone()[0]
            logger.info(f"  PostGIS version: {postgis_version}")
            
            # Check for land_polygons table
            result = conn.execute(sqlalchemy.text(
                "SELECT EXISTS (SELECT FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = 'land_polygons')"
            ))
            has_table = result.fetchone()[0]
            
            if has_table:
                logger.info(f"✓ land_polygons table exists")
                
                # Count polygons
                result = conn.execute(sqlalchemy.text("SELECT COUNT(*) FROM public.land_polygons"))
                count = result.fetchone()[0]
                logger.info(f"  Total polygons in database: {count:,}")
            else:
                logger.warning("✗ land_polygons table NOT FOUND")
                logger.warning("  You need to load coastline data into this table.")
                return False
        
        logger.info("\n✓ Database connection test passed\n")
        return True
        
    except Exception as e:
        logger.error(f"✗ Database connection failed: {e}")
        return False


def test_polygon_loading():
    """Test loading land polygons for a specific region."""
    logger.info("=" * 70)
    logger.info("Testing Polygon Loading (Corsica Region)")
    logger.info("=" * 70)
    
    try:
        from WeatherRoutingTool.constraints.constraints import LandPolygonsCrossing
        from WeatherRoutingTool.utils.maps import Map
        
        # Corsica bounding box
        map_size = Map(41.0, 8.0, 43.5, 10.0)
        logger.info(f"Bounding box: ({map_size.lat1}, {map_size.lon1}) to ({map_size.lat2}, {map_size.lon2})")
        
        # Initialize detector
        logger.info("Initializing LandPolygonsCrossing...")
        detector = LandPolygonsCrossing(map_size=map_size)
        
        if detector.initialization_successful:
            logger.info("✓ Polygon detector initialized successfully")
            if detector.land_polygon_STRTree is not None:
                # Count loaded geometries
                # Note: STRtree doesn't expose count directly, but we logged it during init
                logger.info("✓ STRtree spatial index created")
            return detector
        else:
            logger.error("✗ Polygon detector initialization failed")
            return None
            
    except Exception as e:
        logger.error(f"✗ Polygon loading failed: {e}", exc_info=True)
        return None


def test_land_detection(detector):
    """Test actual land crossing detection."""
    logger.info("=" * 70)
    logger.info("Testing Land Crossing Detection")
    logger.info("=" * 70)
    
    if detector is None:
        logger.error("Detector not available, skipping tests")
        return False
    
    # Test cases around Corsica
    test_cases = [
        {
            'name': 'Open water (should NOT cross land)',
            'start': (41.3, 8.3),
            'end': (41.5, 8.5),
            'expected': False
        },
        {
            'name': 'Through Corsica (should cross land)',
            'start': (42.0, 8.5),
            'end': (42.0, 9.5),
            'expected': True
        },
        {
            'name': 'North of Corsica (should NOT cross land)',
            'start': (43.2, 9.0),
            'end': (43.2, 9.3),
            'expected': False
        },
        {
            'name': 'Coastal segment (may or may not cross)',
            'start': (41.35, 9.2),
            'end': (41.40, 9.25),
            'expected': None  # Unknown, just testing it doesn't crash
        }
    ]
    
    passed = 0
    failed = 0
    
    for test in test_cases:
        logger.info(f"\nTest: {test['name']}")
        logger.info(f"  From: {test['start']} to {test['end']}")
        
        try:
            lat_start = np.array([test['start'][0]])
            lon_start = np.array([test['start'][1]])
            lat_end = np.array([test['end'][0]])
            lon_end = np.array([test['end'][1]])
            
            result = detector.check_crossing(lat_start, lon_start, lat_end, lon_end)
            crosses_land = result[0] if result else False
            
            logger.info(f"  Result: {'CROSSES LAND' if crosses_land else 'open water'}")
            
            if test['expected'] is not None:
                if crosses_land == test['expected']:
                    logger.info(f"  ✓ PASS")
                    passed += 1
                else:
                    logger.warning(f"  ✗ FAIL (expected: {test['expected']})")
                    failed += 1
            else:
                logger.info(f"  ✓ Completed (no validation)")
                passed += 1
                
        except Exception as e:
            logger.error(f"  ✗ ERROR: {e}")
            failed += 1
    
    logger.info(f"\n{'=' * 70}")
    logger.info(f"Test Results: {passed} passed, {failed} failed")
    logger.info(f"{'=' * 70}\n")
    
    return failed == 0


def main():
    """Run all tests."""
    print("\n" + "=" * 70)
    print("Weather Routing Tool - Polygon Land Detection Test")
    print("=" * 70 + "\n")
    
    # Step 1: Check environment
    if not check_environment():
        logger.error("Environment check failed. Please configure environment variables.")
        sys.exit(1)
    
    # Step 2: Test database connection
    if not test_database_connection():
        logger.error("Database connection failed. Cannot continue.")
        sys.exit(1)
    
    # Step 3: Test polygon loading
    detector = test_polygon_loading()
    if detector is None:
        logger.error("Polygon loading failed. Cannot continue.")
        sys.exit(1)
    
    # Step 4: Test land detection
    if not test_land_detection(detector):
        logger.warning("Some land detection tests failed.")
        sys.exit(1)
    
    # All tests passed
    print("\n" + "=" * 70)
    print("✓ ALL TESTS PASSED")
    print("=" * 70)
    print("\nYour polygon-based land detection system is working correctly!")
    print("You can now run routing with 'land_crossing_polygons' in CONSTRAINTS_LIST.")
    print("\nNext steps:")
    print("  1. Review config.corsica_polygon_test.json")
    print("  2. Adjust route coordinates for your test case")
    print("  3. Run: python cli.py --config config.corsica_polygon_test.json")
    print()


if __name__ == '__main__':
    main()
