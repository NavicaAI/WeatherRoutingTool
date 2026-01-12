#!/usr/bin/env python3
"""
Test GCR Slider with the exact Sardinia route that's crossing land
"""
import os
import sys
import json
import tempfile
from pathlib import Path
sys.path.insert(0, os.path.dirname(__file__))

from WeatherRoutingTool.execute_routing import execute_routing
from WeatherRoutingTool.config import Config

print("\n" + "="*70)
print("Testing GCR Slider with Sardinia Route")
print("="*70)

# Your exact route
start = [39.5, 8]
end = [41.73852846935917, 6.788675619519636]
waypoint = [39.19820534889482, 10.337048965117496]

print(f"\nStart: {start}")
print(f"Waypoint: {waypoint}")
print(f"End: {end}")

with tempfile.TemporaryDirectory() as tmpdir:
    route_path = tmpdir
    courses_file = os.path.join(tmpdir, "dummy_courses.nc")
    depth_file = os.path.join(tmpdir, "dummy_depth.nc")
    weather_file = os.path.join(tmpdir, "dummy_weather.nc")
    
    # Create empty dummy files
    for f in [courses_file, depth_file, weather_file]:
        Path(f).touch()
    
    config_dict = {
        "DEFAULT_ROUTE": start + end,
        "DEPARTURE_TIME": "2023-11-11T11:11Z",
        "DEFAULT_MAP": [38.0, 6.0, 43.0, 11.0],  # Covers region
        "BOAT_DRAUGHT_AFT": 10,
        "BOAT_DRAUGHT_FORE": 10,
        "INTERMEDIATE_WAYPOINTS": [waypoint],
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
        "GCR_SLIDER_USE_POLYGON_LAND_DETECTION": True,  # ENABLED
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
        "BOAT_SPEED": 12.5,
        
        "DIJKSTRA_MASK_FILE": "",
        "_DATA_MODE_WEATHER": "dummy",
        "_DATA_MODE_DEPTH": "dummy",
    }
    
    try:
        print("\nInitializing configuration...")
        config = Config(**config_dict)
        
        print("\nRunning GCR Slider with POLYGON DETECTION ENABLED...")
        print("-"*70)
        
        execute_routing(config)
        
        print("-"*70)
        
        # Check output
        route_file = os.path.join(route_path, "gcrslider.json")
        if os.path.exists(route_file):
            with open(route_file, 'r') as f:
                route_data = json.load(f)
            
            if 'features' in route_data and len(route_data['features']) > 0:
                coordinates = route_data['features'][0]['geometry']['coordinates']
                waypoints = [(lat, lon) for lon, lat in coordinates]
                
                print(f"\n✓ Route generated with {len(waypoints)} waypoints")
                print("\nRoute waypoints:")
                for i, (lat, lon) in enumerate(waypoints):
                    print(f"  [{lat:.4f}, {lon:.4f}]{',' if i < len(waypoints)-1 else ''}")
                
                # Now check if these waypoints cross land
                print("\n" + "="*70)
                print("Checking generated route for land crossings...")
                print("="*70)
                
                from WeatherRoutingTool.constraints.constraints import LandPolygonsCrossing
                from WeatherRoutingTool.utils.maps import Map
                import numpy as np
                
                map_size = Map(lat1=38.0, lat2=43.0, lon1=6.0, lon2=11.0)
                detector = LandPolygonsCrossing(map_size=map_size)
                
                if detector.initialization_successful:
                    land_crossing_count = 0
                    for i in range(len(waypoints) - 1):
                        lat1, lon1 = waypoints[i]
                        lat2, lon2 = waypoints[i+1]
                        
                        result = detector.check_crossing(
                            lat_start=np.array([lat1]),
                            lon_start=np.array([lon1]),
                            lat_end=np.array([lat2]),
                            lon_end=np.array([lon2])
                        )
                        
                        if result[0]:
                            land_crossing_count += 1
                            print(f"  ✗ Segment {i+1}: ({lat1:.2f}, {lon1:.2f}) -> ({lat2:.2f}, {lon2:.2f}) CROSSES LAND")
                    
                    print("\nResult:")
                    print(f"  Total segments: {len(waypoints) - 1}")
                    print(f"  Segments crossing land: {land_crossing_count}")
                    
                    if land_crossing_count == 0:
                        print("\n✓✓✓ SUCCESS! Route avoids all land crossings.")
                    else:
                        print(f"\n✗✗✗ FAILURE! {land_crossing_count} segments still cross land.")
                        print("    GCR Slider is not properly respecting polygon detection.")
            else:
                print("✗ No route geometry found")
        else:
            print(f"✗ No route file created at {route_file}")
        
    except Exception as e:
        print(f"\n✗ Test failed with error: {e}")
        import traceback
        traceback.print_exc()

print("\n" + "="*70)
