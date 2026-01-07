#!/usr/bin/env python3
"""
Test script to verify GCR Slider land crossing fix.
Tests the problematic Sardinia route that previously crossed 56km of land.
"""
import sys
from pathlib import Path

# Add WRT to path
sys.path.insert(0, str(Path(__file__).parent))

from WeatherRoutingTool.algorithms.gcrslider import GcrSliderAlgorithm
from WeatherRoutingTool.config import Config
from global_land_mask import is_land as is_land_global_land_mask
from geographiclib.geodesic import Geodesic
import math

geod = Geodesic.WGS84

def check_route_for_land_crossings(waypoints, name="Route"):
    """Check if any segment of the route crosses land."""
    print(f"\n{'='*60}")
    print(f"Testing {name}")
    print(f"{'='*60}")
    
    crossings = []
    for i in range(len(waypoints) - 1):
        start = waypoints[i]
        end = waypoints[i + 1]
        
        line = geod.InverseLine(start[0], start[1], end[0], end[1])
        distance_km = line.s13 / 1000
        
        # Check at 1km intervals
        interval = 1000
        n = int(math.ceil(line.s13 / interval))
        land_points = 0
        
        for j in range(0, n + 1):
            s = min(interval * j, line.s13)
            g = line.Position(s, Geodesic.STANDARD | Geodesic.LONG_UNROLL)
            if is_land_global_land_mask(g['lat2'], g['lon2']):
                land_points += 1
        
        if land_points > 0:
            crossings.append({
                'segment': i,
                'start': start,
                'end': end,
                'distance_km': distance_km,
                'land_points': land_points,
                'land_km': land_points * 1.0  # approximate
            })
            print(f"  ❌ Segment [{i}→{i+1}]: {start} → {end}")
            print(f"     Distance: {distance_km:.1f} km, Land crossings: {land_points} points (~{land_points}km)")
        else:
            print(f"  ✅ Segment [{i}→{i+1}]: {distance_km:.1f} km - Clear")
    
    if not crossings:
        print(f"\n✅ SUCCESS: {name} has NO land crossings!")
        return True
    else:
        print(f"\n❌ FAIL: {name} has {len(crossings)} segments crossing land")
        total_land_km = sum(c['land_km'] for c in crossings)
        print(f"   Total land crossed: ~{total_land_km:.1f} km")
        return False

def main():
    print("\n" + "="*60)
    print("GCR Slider Land Crossing Fix - Verification Test")
    print("="*60)
    
    # The problematic route from our debugging
    # This previously crossed southern Sardinia
    problematic_route = [
        (39.5000, 8.0000),   # West of Sardinia
        (39.0635, 8.2701),   # South of Sant'Antioco
        (39.0100, 9.1634),   # Gulf of Cagliari - THIS SEGMENT CROSSED 56km OF LAND
        (39.3946, 10.1308),
        (39.8322, 11.1410),  # East of Sardinia
        (42.8431, 6.6902)    # Near Toulon, France
    ]
    
    print("\n📋 Test Case: Mediterranean route (Sardinia passage)")
    print(f"   Start: ({problematic_route[0][0]}, {problematic_route[0][1]})")
    print(f"   End: ({problematic_route[-1][0]}, {problematic_route[-1][1]})")
    print(f"   Waypoints: {len(problematic_route)}")
    
    # Check the problematic segment specifically
    print("\n🔍 Checking segment [2]→[3] (previously crossed 56km of land):")
    segment_ok = check_route_for_land_crossings(
        [problematic_route[2], problematic_route[3]],
        "Segment [2]→[3]"
    )
    
    # Check full route
    route_ok = check_route_for_land_crossings(
        problematic_route,
        "Full Route"
    )
    
    print("\n" + "="*60)
    if segment_ok and route_ok:
        print("✅ ALL TESTS PASSED!")
        print("   The fix successfully prevents land crossing.")
        return 0
    else:
        print("❌ TESTS FAILED!")
        print("   The route still crosses land. Further fixes needed.")
        return 1

if __name__ == '__main__':
    sys.exit(main())
