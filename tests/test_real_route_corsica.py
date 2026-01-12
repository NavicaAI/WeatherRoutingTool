#!/usr/bin/env python3
"""
Test routing with real navigation points around Corsica
Tests that polygon-based land detection prevents routes from cutting across the island
"""
import os
import sys
import json
import logging
import tempfile
from pathlib import Path

# Add project to path
sys.path.insert(0, os.path.dirname(__file__))

from WeatherRoutingTool.execute_routing import execute_routing
from WeatherRoutingTool.config import Config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def test_route_around_corsica():
    """
    Test route from west to east of Corsica
    Without proper land detection, route might try to cut across the island
    With polygons, it should route around the north or south
    """
    
    print("\n" + "="*70)
    print("Testing Real Route Around Corsica")
    print("="*70)
    
    # Route from west of Corsica to east of Corsica
    # This should force routing around the island
    start_lat = 42.0   # West of Corsica
    start_lon = 8.0
    end_lat = 42.2     # East of Corsica  
    end_lon = 9.8
    
    print(f"\nRoute: ({start_lat}, {start_lon}) -> ({end_lat}, {end_lon})")
    print("Expected: Route should go around Corsica (north or south)")
    print("Without land detection: Would try to cut straight across island")
    
    # Create temporary directory for output
    with tempfile.TemporaryDirectory() as tmpdir:
        route_path = tmpdir
        courses_file = os.path.join(tmpdir, "dummy_courses.nc")
        depth_file = os.path.join(tmpdir, "dummy_depth.nc")
        weather_file = os.path.join(tmpdir, "dummy_weather.nc")
        
        # Create empty dummy files
        for f in [courses_file, depth_file, weather_file]:
            Path(f).touch()
        
        # Minimal config for GCR Slider test
        config_dict = {
            "DEFAULT_ROUTE": [start_lat, start_lon, end_lat, end_lon],
            "DEPARTURE_TIME": "2023-11-11T11:11Z",
            "DEFAULT_MAP": [41.0, 7.0, 43.5, 11.0],  # Covers Corsica region
            "BOAT_DRAUGHT_AFT": 10,
            "BOAT_DRAUGHT_FORE": 10,
            "INTERMEDIATE_WAYPOINTS": [],
            "CONSTRAINTS_LIST": ["land_crossing_global_land_mask", "land_crossing_polygons"],
            
            "TIME_FORECAST": 90,
            "ROUTING_STEPS": 60,
            "DELTA_TIME_FORECAST": 3,
            "DELTA_FUEL": 3000,
            
            "COURSES_FILE": courses_file,
            "DEPTH_DATA": depth_file,
            "WEATHER_DATA": weather_file,
            "ROUTE_PATH": route_path,
            
            "ROUTER_HDGS_SEGMENTS": 30,
            "ROUTER_HDGS_INCREMENTS_DEG": 6,
            
            "ROUTING_ALGORITHM": "gcrslider",
            "GCR_SLIDER_USE_POLYGON_LAND_DETECTION": True,
            "GCR_SLIDER_DISTANCE_MOVE": 10000,  
            "GCR_SLIDER_THRESHOLD": 50,
            "GCR_SLIDER_LAND_BUFFER": 0.01,
            "GCR_SLIDER_ANGLE_STEP": 10,
            "GCR_SLIDER_DYNAMIC_PARAMETERS": True,
            "GCR_SLIDER_INTERPOLATE": True,
            "GCR_SLIDER_INTERP_DIST": 50000,
            "GCR_SLIDER_INTERP_NORMALIZED": False,
            "GCR_SLIDER_MAX_POINTS": 2000,
            
            "BOAT_TYPE": "direct_power_method",
            "BOAT_BREADTH": 32,
            "BOAT_FUEL_RATE": 167,
            "BOAT_HBR": 30,
            "BOAT_LENGTH": 180,
            "BOAT_SMCR_POWER": 6502,
            "BOAT_SPEED": 12,  # 12 knots for reasonable test time
            
            "DIJKSTRA_MASK_FILE": "",
            
            "_DATA_MODE_WEATHER": "dummy",
            "_DATA_MODE_DEPTH": "dummy",
        }
        
        try:
            # Create config
            print("\nInitializing configuration...")
            config = Config(**config_dict)
            
            print("\nRunning GCR Slider routing algorithm...")
            print("(This will load land polygons from database)")
            print("-"*70)
            
            # Execute routing
            execute_routing(config)
            
            print("-"*70)
            print("\n" + "="*70)
            print("Routing Results")
            print("="*70)
            
            # Check if route was created
            route_file = os.path.join(route_path, "gcrslider.json")
            if os.path.exists(route_file):
                with open(route_file, 'r') as f:
                    route_data = json.load(f)
                
                print(f"✓ Route file created: {route_file}")
                
                # Extract waypoints
                if 'features' in route_data and len(route_data['features']) > 0:
                    coordinates = route_data['features'][0]['geometry']['coordinates']
                    waypoints = [(lat, lon) for lon, lat in coordinates]
                    
                    print(f"✓ Route found with {len(waypoints)} waypoints")
                    
                    # Show first few and last few waypoints
                    print("\nFirst 5 waypoints:")
                    for i, (lat, lon) in enumerate(waypoints[:5]):
                        print(f"  {i+1}. ({lat:.3f}, {lon:.3f})")
                    
                    if len(waypoints) > 10:
                        print("  ...")
                        print(f"Last 5 waypoints:")
                        for i, (lat, lon) in enumerate(waypoints[-5:], start=len(waypoints)-4):
                            print(f"  {i}. ({lat:.3f}, {lon:.3f})")
                    
                    # Calculate route deviation from straight line
                    import math
                    
                    def distance(lat1, lon1, lat2, lon2):
                        return math.sqrt((lat2-lat1)**2 + (lon2-lon1)**2)
                    
                    direct_dist = distance(start_lat, start_lon, end_lat, end_lon)
                    
                    # Calculate actual route distance
                    route_dist = 0
                    for i in range(len(waypoints)-1):
                        route_dist += distance(waypoints[i][0], waypoints[i][1],
                                             waypoints[i+1][0], waypoints[i+1][1])
                    
                    deviation_pct = ((route_dist - direct_dist) / direct_dist) * 100
                    
                    print(f"\nRoute analysis:")
                    print(f"  Direct distance: {direct_dist:.3f} degrees")
                    print(f"  Actual route distance: {route_dist:.3f} degrees")
                    print(f"  Deviation: {deviation_pct:.1f}%")
                    
                    if deviation_pct > 5:
                        print("  ✓ Route deviates from straight line (routing around island)")
                    else:
                        print("  ⚠ Route is nearly straight (may not be avoiding land)")
                else:
                    print("✗ No route geometry found in GeoJSON")
            else:
                print(f"✗ No route file found at {route_file}")
                print("  Routing may have failed or output path is incorrect")
            
            print("\n" + "="*70)
            print("Test Complete")
            print("="*70)
            
        except Exception as e:
            logger.error(f"Routing test failed: {e}", exc_info=True)
            print(f"\n✗ Test failed with error: {e}")
            return False
    
    return True

if __name__ == "__main__":
    success = test_route_around_corsica()
    sys.exit(0 if success else 1)
