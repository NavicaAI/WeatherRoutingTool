#!/usr/bin/env python3
"""
Quick test to verify polygon caching is working and measure performance improvement
"""
import time
import logging
from WeatherRoutingTool.constraints.constraints import LandPolygonsCrossing
from WeatherRoutingTool.utils.maps import Map

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def test_caching():
    """Test that polygon loading is cached and reused"""
    
    # Define a test bbox (southern Corsica)
    map_size = Map(lat1=41.0, lat2=41.8, lon1=8.5, lon2=9.5)
    
    print("\n" + "="*70)
    print("Testing Polygon Caching Performance")
    print("="*70)
    
    # First initialization (should query database)
    print("\n1. First initialization (expect database query)...")
    start = time.time()
    detector1 = LandPolygonsCrossing(map_size=map_size)
    time1 = time.time() - start
    print(f"   Time: {time1:.3f}s")
    print(f"   Initialized successfully: {detector1.initialization_successful}")
    
    # Second initialization (should use cache)
    print("\n2. Second initialization (expect cache hit)...")
    start = time.time()
    detector2 = LandPolygonsCrossing(map_size=map_size)
    time2 = time.time() - start
    print(f"   Time: {time2:.3f}s")
    print(f"   Initialized successfully: {detector2.initialization_successful}")
    
    # Third initialization (should use cache)
    print("\n3. Third initialization (expect cache hit)...")
    start = time.time()
    detector3 = LandPolygonsCrossing(map_size=map_size)
    time3 = time.time() - start
    print(f"   Time: {time3:.3f}s")
    print(f"   Initialized successfully: {detector3.initialization_successful}")
    
    print("\n" + "="*70)
    print("Performance Summary:")
    print("="*70)
    print(f"First initialization:  {time1:.3f}s (database query)")
    print(f"Second initialization: {time2:.3f}s (cache)")
    print(f"Third initialization:  {time3:.3f}s (cache)")
    
    if time2 < time1 and time3 < time1:
        speedup = time1 / ((time2 + time3) / 2)
        print(f"\n✓ Caching working! Speedup: {speedup:.1f}x faster")
        print(f"  Saved ~{(time1 - time2):.3f}s per cached initialization")
    else:
        print("\n⚠ Warning: Cache may not be working as expected")
    
    # Check cache size
    cache_size = len(LandPolygonsCrossing._polygon_cache)
    print(f"\nCache entries: {cache_size}")
    print("="*70)

if __name__ == "__main__":
    test_caching()
