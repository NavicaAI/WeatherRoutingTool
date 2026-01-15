#!/usr/bin/env python3
"""
Build a land proximity grid for fast "is there land nearby?" lookups.

This creates a low-resolution grid where each cell indicates whether
there's land within a configurable distance (default 150km). This is used
to intelligently select A* resolution - coarse in open ocean, fine near land.
"""

import argparse
import pickle
import numpy as np
from pathlib import Path
from global_land_mask import is_land
from geographiclib.geodesic import Geodesic
import time

geod = Geodesic.WGS84

def check_land_nearby(lat: float, lon: float, radius_km: float = 150.0) -> bool:
    """
    Check if there's land within radius_km of (lat, lon).
    
    Samples points in a grid pattern around the center point.
    """
    # If the center point itself is land, definitely near land
    if is_land(lat, lon):
        return True
    
    # Sample at multiple distances and angles
    # More samples at larger radius for better coverage
    for dist_km in [50, 100, 150]:
        if dist_km > radius_km:
            break
        # Sample every 30 degrees at this radius
        for bearing in range(0, 360, 30):
            result = geod.Direct(lat, lon, bearing, dist_km * 1000)
            sample_lat = result['lat2']
            sample_lon = result['lon2']
            if is_land(sample_lat, sample_lon):
                return True
    
    return False


def build_land_proximity_grid(
    resolution: float = 2.0,
    radius_km: float = 150.0,
    lat_min: float = -80.0,
    lat_max: float = 80.0,
    output_path: str = None
):
    """
    Build a grid indicating land proximity at each cell.
    
    Args:
        resolution: Grid resolution in degrees
        radius_km: Distance threshold - cells within this distance of land are marked True
        lat_min, lat_max: Latitude bounds
        output_path: Where to save the grid
    """
    start_time = time.time()
    
    lats = np.arange(lat_min, lat_max + resolution, resolution)
    lons = np.arange(-180.0, 180.0 + resolution, resolution)
    
    n_lats = len(lats)
    n_lons = len(lons)
    total_cells = n_lats * n_lons
    
    print(f"Building land proximity grid:")
    print(f"  Resolution: {resolution}°")
    print(f"  Radius: {radius_km}km")
    print(f"  Grid size: {n_lats} x {n_lons} = {total_cells:,} cells")
    print()
    
    # Create grid - True means "near land", False means "open ocean"
    near_land = np.zeros((n_lats, n_lons), dtype=bool)
    
    cells_checked = 0
    near_land_count = 0
    last_pct = 0
    
    for i, lat in enumerate(lats):
        for j, lon in enumerate(lons):
            if check_land_nearby(lat, lon, radius_km):
                near_land[i, j] = True
                near_land_count += 1
            
            cells_checked += 1
            pct = int(100 * cells_checked / total_cells)
            if pct >= last_pct + 5:
                elapsed = time.time() - start_time
                rate = cells_checked / elapsed
                remaining = (total_cells - cells_checked) / rate
                print(f"  {pct}% complete - {near_land_count:,} near-land cells - ETA: {remaining/60:.1f}min")
                last_pct = pct
    
    elapsed = time.time() - start_time
    
    print()
    print(f"Grid complete in {elapsed/60:.1f} minutes")
    print(f"  Near land cells: {near_land_count:,} ({100*near_land_count/total_cells:.1f}%)")
    print(f"  Open ocean cells: {total_cells - near_land_count:,} ({100*(total_cells-near_land_count)/total_cells:.1f}%)")
    
    # Save the grid
    data = {
        'near_land': near_land,
        'lats': lats,
        'lons': lons,
        'resolution': resolution,
        'radius_km': radius_km,
        'bounds': {
            'lat_min': lat_min,
            'lat_max': lat_max,
            'lon_min': -180.0,
            'lon_max': 180.0
        },
        'built_at': time.strftime('%Y-%m-%d %H:%M:%S')
    }
    
    if output_path is None:
        output_path = f'/tmp/wrt_graph_cache/LAND_PROXIMITY_{resolution:.2f}deg_{int(radius_km)}km.pkl'
    
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    
    with open(output_path, 'wb') as f:
        pickle.dump(data, f)
    
    size_mb = Path(output_path).stat().st_size / (1024 * 1024)
    print(f"  Saved to: {output_path} ({size_mb:.2f} MB)")
    
    return data


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Build land proximity grid for smart resolution selection')
    parser.add_argument('--resolution', type=float, default=2.0, help='Grid resolution in degrees (default: 2.0)')
    parser.add_argument('--radius', type=float, default=150.0, help='Land proximity radius in km (default: 150)')
    parser.add_argument('--output', type=str, default=None, help='Output file path')
    
    args = parser.parse_args()
    
    build_land_proximity_grid(
        resolution=args.resolution,
        radius_km=args.radius,
        output_path=args.output
    )
