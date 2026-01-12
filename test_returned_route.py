#!/usr/bin/env python3
"""
Test the route returned by GCR Slider
"""
import os
import sys
import json
sys.path.insert(0, os.path.dirname(__file__))

# Set database credentials
os.environ.update({
    "WRT_DB_HOST": "localhost",
    "WRT_DB_PORT": "5433",
    "WRT_DB_DATABASE": "gis_db",
    "WRT_DB_USERNAME": "insectile",
    "WRT_DB_PASSWORD": "",
    "POSTGRES_SCHEMA": "public"
})

from WeatherRoutingTool.constraints.constraints import LandPolygonsCrossing
from WeatherRoutingTool.utils.maps import Map
from shapely.geometry import LineString

print("\n" + "="*70)
print("Testing GCR Slider Route")
print("="*70)

# Load route
with open('/tmp/route_response.json') as f:
    route = json.load(f)

waypoints = route['optimized_route']['waypoints']
print(f"\nRoute has {len(waypoints)} waypoints, {route['optimized_route']['total_distance_nm']}nm")

# Initialize polygon detector
map_size = Map(lat1=38, lat2=43, lon1=5, lon2=11)
print("\n1. Initializing polygon detector...")
detector = LandPolygonsCrossing(map_size=map_size)

if not detector.initialization_successful:
    print("✗ Failed to initialize polygon detector")
    sys.exit(1)

print("✓ Polygon detector initialized")

# Test each segment
print("\n2. Testing each segment...")
land_crossings = 0
for i in range(len(waypoints) - 1):
    start = waypoints[i]
    end = waypoints[i + 1]
    
    # Create line (lon, lat format for shapely)
    line = LineString([[start[1], start[0]], [end[1], end[0]]])
    
    # Check crossing
    crosses = detector.check_crossing([line])[0]
    status = "❌ CROSSES LAND" if crosses else "✓ OK"
    print(f"  Segment {i+1:2d}: ({start[0]:6.2f}, {start[1]:7.3f}) → ({end[0]:6.2f}, {end[1]:7.3f}) {status}")
    
    if crosses:
        land_crossings += 1

print("\n" + "="*70)
print(f"RESULT: {land_crossings}/{len(waypoints)-1} segments cross land ({land_crossings*100/(len(waypoints)-1):.0f}%)")
if land_crossings == 0:
    print("✓ SUCCESS: Route does not cross land!")
else:
    print(f"✗ FAILURE: {land_crossings} segments still cross land")
print("="*70 + "\n")
