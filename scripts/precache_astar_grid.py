#!/usr/bin/env python3
"""
Pre-cache A* routing grid for a given bounding box.

This script builds the A* navigation graph for a specified region and caches it
to disk. Subsequent routing requests in that region will load the cached graph
instantly (~2-3 seconds) instead of rebuilding it (~3-5 minutes).

Usage:
    python scripts/precache_astar_grid.py --lat-min 34 --lat-max 45 --lon-min 4 --lon-max 21
    python scripts/precache_astar_grid.py --lat-min 34 --lat-max 45 --lon-min 4 --lon-max 21 --resolution 0.05
    
Examples:
    # Western/Central Mediterranean (default for local_server.py)
    python scripts/precache_astar_grid.py --lat-min 34 --lat-max 45 --lon-min 4 --lon-max 21
    
    # Smaller test region (Sardinia area)
    python scripts/precache_astar_grid.py --lat-min 38 --lat-max 42 --lon-min 7 --lon-max 11
    
    # Higher resolution (slower build, more precise routes)
    python scripts/precache_astar_grid.py --lat-min 38 --lat-max 42 --lon-min 7 --lon-max 11 --resolution 0.025
"""

import argparse
import sys
import time
from datetime import datetime
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from WeatherRoutingTool.algorithms.astar_router import AStarRouter, GRAPH_CACHE_DIR


class MinimalConfig:
    """Minimal config object for A* router initialization."""
    
    def __init__(self, lat_min, lat_max, lon_min, lon_max, resolution=0.05):
        # Dummy route (required by base class but not used for grid building)
        self.DEFAULT_ROUTE = [lat_min, lon_min, lat_max, lon_max]
        self.DEFAULT_MAP = [lat_min, lon_min, lat_max, lon_max]
        self.DEPARTURE_TIME = datetime.now()
        
        # A* grid parameters
        self.ASTAR_GRID_RESOLUTION = resolution
        self.ASTAR_NOF_NEIGHBORS = 1  # 8-connected grid
        self.ASTAR_LAND_CHECK_INTERVAL = 1000  # meters
        self.ASTAR_USE_WEATHER = False
        self.ASTAR_LAT_MIN = lat_min
        self.ASTAR_LAT_MAX = lat_max
        self.ASTAR_LON_MIN = lon_min
        self.ASTAR_LON_MAX = lon_max
        
        # Use global land mask for grid building (fast)
        # Polygon-based checking is done at edge level during routing
        self.CONSTRAINTS_LIST = ['land_crossing_global_land_mask']


def estimate_grid_size(lat_min, lat_max, lon_min, lon_max, resolution):
    """Estimate the number of grid cells and approximate build time."""
    n_lats = int((lat_max - lat_min) / resolution) + 1
    n_lons = int((lon_max - lon_min) / resolution) + 1
    total_cells = n_lats * n_lons
    
    # Rough estimates based on observed performance
    # ~50% of cells are typically water in Mediterranean
    water_ratio = 0.5
    water_cells = int(total_cells * water_ratio)
    
    # Build time: ~0.5ms per water cell for edge checking
    estimated_seconds = water_cells * 0.0005
    
    return n_lats, n_lons, total_cells, water_cells, estimated_seconds


def main():
    parser = argparse.ArgumentParser(
        description='Pre-cache A* routing grid for a given bounding box.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    
    parser.add_argument('--lat-min', type=float, required=True,
                        help='Minimum latitude (southern bound)')
    parser.add_argument('--lat-max', type=float, required=True,
                        help='Maximum latitude (northern bound)')
    parser.add_argument('--lon-min', type=float, required=True,
                        help='Minimum longitude (western bound)')
    parser.add_argument('--lon-max', type=float, required=True,
                        help='Maximum longitude (eastern bound)')
    parser.add_argument('--resolution', type=float, default=0.05,
                        help='Grid resolution in degrees (default: 0.05 = ~5km)')
    parser.add_argument('--force', action='store_true',
                        help='Force rebuild even if cache exists')
    
    args = parser.parse_args()
    
    # Validate bounds
    if args.lat_min >= args.lat_max:
        print("Error: lat-min must be less than lat-max")
        sys.exit(1)
    if args.lon_min >= args.lon_max:
        print("Error: lon-min must be less than lon-max")
        sys.exit(1)
    if not (-90 <= args.lat_min <= 90 and -90 <= args.lat_max <= 90):
        print("Error: Latitude must be between -90 and 90")
        sys.exit(1)
    if not (-180 <= args.lon_min <= 180 and -180 <= args.lon_max <= 180):
        print("Error: Longitude must be between -180 and 180")
        sys.exit(1)
    if args.resolution <= 0 or args.resolution > 1:
        print("Error: Resolution must be between 0 and 1 degrees")
        sys.exit(1)
    
    # Estimate grid size
    n_lats, n_lons, total_cells, water_cells, est_time = estimate_grid_size(
        args.lat_min, args.lat_max, args.lon_min, args.lon_max, args.resolution
    )
    
    print("=" * 60)
    print("A* Grid Pre-caching Tool")
    print("=" * 60)
    print(f"Region: ({args.lat_min}, {args.lon_min}) to ({args.lat_max}, {args.lon_max})")
    print(f"Resolution: {args.resolution}° (~{args.resolution * 111:.1f} km)")
    print(f"Grid size: {n_lats} x {n_lons} = {total_cells:,} cells")
    print(f"Estimated water cells: ~{water_cells:,}")
    print(f"Estimated build time: ~{est_time/60:.1f} minutes")
    print(f"Cache directory: {GRAPH_CACHE_DIR}")
    print("=" * 60)
    
    # Check for existing cache
    config = MinimalConfig(
        args.lat_min, args.lat_max, args.lon_min, args.lon_max, args.resolution
    )
    
    # Create router (this will check for cached graph)
    print("\nInitializing A* router...")
    router = AStarRouter(config)
    
    # Check if cache exists
    cache_exists = False
    if GRAPH_CACHE_DIR.exists():
        # The cache filename is based on a hash of the parameters
        # Let the router check for it
        pass
    
    if args.force:
        print("Force rebuild requested - will rebuild even if cache exists")
    
    # Build/load the graph
    print("\nBuilding/loading graph...")
    start_time = time.time()
    
    try:
        router._load_or_build_graph()
    except Exception as e:
        print(f"\nError building graph: {e}")
        sys.exit(1)
    
    elapsed = time.time() - start_time
    
    print("\n" + "=" * 60)
    print("SUCCESS!")
    print("=" * 60)
    print(f"Graph nodes: {router.graph.number_of_nodes():,}")
    print(f"Graph edges: {router.graph.number_of_edges():,}")
    print(f"Build/load time: {elapsed:.1f} seconds")
    
    # Calculate cache file size
    cache_files = list(GRAPH_CACHE_DIR.glob("*.pkl"))
    if cache_files:
        latest_cache = max(cache_files, key=lambda p: p.stat().st_mtime)
        cache_size_mb = latest_cache.stat().st_size / (1024 * 1024)
        print(f"Cache file: {latest_cache.name}")
        print(f"Cache size: {cache_size_mb:.1f} MB")
    
    print("=" * 60)
    print("\nThe graph is now cached. Subsequent routing requests will load instantly.")


if __name__ == "__main__":
    main()
