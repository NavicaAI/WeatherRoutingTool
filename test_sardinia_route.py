#!/usr/bin/env python3
"""
Test the specific route that's crossing land
"""
import os
import sys
import numpy as np
sys.path.insert(0, os.path.dirname(__file__))

from WeatherRoutingTool.constraints.constraints import LandPolygonsCrossing
from WeatherRoutingTool.utils.maps import Map

print("\n" + "="*70)
print("Testing Sardinia Route That's Crossing Land")
print("="*70)

# Your route points
start = (39.5, 8)
waypoint = (39.19820534889482, 10.337048965117496)
end = (41.73852846935917, 6.788675619519636)

# The problematic returned waypoints
returned_waypoints = [
    [39.5, 8],
    [38.86154759219938, 8.612889963999871],
    [39.102918909064535, 9.452768286078705],
    [39.24780908372212, 10.30537344236829],
    [40.05180662363445, 9.791963666784575],
    [40.90683827412561, 9.706992216764105],
    [41.15165109847867, 9.031001169769569],
    [40.87006055582829, 8.138033638941064],
    [40.366500222188456, 8.114399971097175],
    [41.052514345773815, 7.451537795308404],
    [41.73852846935917, 6.788675619519636]
]

# Define map covering the region
map_size = Map(lat1=38.5, lat2=42.5, lon1=6.0, lon2=11.0)

# Initialize polygon detector
print("\n1. Initializing polygon detector...")
detector = LandPolygonsCrossing(map_size=map_size)

if not detector.initialization_successful:
    print("✗ Failed to initialize polygon detector")
    sys.exit(1)

print("✓ Polygon detector initialized")

# Test each segment
print("\n2. Testing route segments for land crossings...")
print("-" * 70)

land_crossing_count = 0
for i in range(len(returned_waypoints) - 1):
    lat1, lon1 = returned_waypoints[i]
    lat2, lon2 = returned_waypoints[i+1]
    
    result = detector.check_crossing(
        lat_start=np.array([lat1]),
        lon_start=np.array([lon1]),
        lat_end=np.array([lat2]),
        lon_end=np.array([lon2])
    )
    
    status = "✗ CROSSES LAND" if result[0] else "✓ over water"
    if result[0]:
        land_crossing_count += 1
    
    print(f"Segment {i+1}: ({lat1:.2f}, {lon1:.2f}) -> ({lat2:.2f}, {lon2:.2f}) : {status}")

print("-" * 70)
print(f"\nSummary:")
print(f"  Total segments: {len(returned_waypoints) - 1}")
print(f"  Segments crossing land: {land_crossing_count}")
print(f"  Percentage crossing: {(land_crossing_count/(len(returned_waypoints)-1)*100):.1f}%")

if land_crossing_count > 0:
    print(f"\n⚠️  {land_crossing_count} segments are crossing land!")
    print("   The polygon detection is working, but GCR Slider is not respecting it.")
else:
    print("\n✓ All segments avoid land.")

print("="*70)
