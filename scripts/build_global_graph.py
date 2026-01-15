#!/usr/bin/env python3
"""
Build a global ocean routing graph for A* pathfinding.

This creates a pre-computed graph covering all navigable oceans.
Building takes 1-4 hours depending on resolution, but the result
is cached and enables instant global routing thereafter.

Usage:
    python scripts/build_global_graph.py [--resolution 0.25]
    
Resolutions:
    0.25° (~28km) - Transoceanic, ~400MB, builds in ~1-2 hours
    0.1°  (~11km) - Medium passages, ~2.5GB, builds in ~4-6 hours  
    0.05° (~5km)  - Fine coastal, ~10GB, builds in ~12+ hours (not recommended globally)
"""

import sys
import os
import time
import pickle
import argparse
from pathlib import Path
from datetime import datetime

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import networkx as nx
from math import ceil

# These will be imported after path setup
from geographiclib.geodesic import Geodesic

geod = Geodesic.WGS84

def build_global_graph(resolution: float = 0.25, output_dir: str = "/tmp/wrt_graph_cache"):
    """
    Build a global ocean routing graph.
    
    Args:
        resolution: Grid resolution in degrees (0.25 = ~28km)
        output_dir: Where to save the graph
    """
    print("=" * 70)
    print("GLOBAL OCEAN GRAPH BUILDER")
    print("=" * 70)
    print(f"Resolution: {resolution}° (~{resolution * 111:.0f} km)")
    print(f"Output dir: {output_dir}")
    print(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)
    
    # Global bounds (full world)
    lat_min, lat_max = -80.0, 80.0  # Skip polar regions (ice)
    lon_min, lon_max = -180.0, 180.0
    
    # Grid dimensions
    lat_grid = np.arange(lat_min, lat_max, resolution)
    lon_grid = np.arange(lon_min, lon_max, resolution)
    
    n_lat = len(lat_grid)
    n_lon = len(lon_grid)
    n_cells = n_lat * n_lon
    
    print(f"\nGrid size: {n_lat} x {n_lon} = {n_cells:,} cells")
    
    # Estimate sizes
    ocean_pct = 0.71  # Earth is ~71% ocean
    est_nodes = int(n_cells * ocean_pct)
    est_edges = est_nodes * 8  # 8 neighbors per node
    est_size_mb = (est_nodes * 100 + est_edges * 80) / (1024 * 1024)
    
    print(f"Estimated ocean cells: ~{est_nodes:,}")
    print(f"Estimated edges: ~{est_edges:,}")
    print(f"Estimated file size: ~{est_size_mb:.0f} MB")
    
    # Initialize land detection
    print(f"\nInitializing land detection...")
    try:
        from global_land_mask import globe
        use_land_mask = True
        print("Using global_land_mask for ocean detection")
    except ImportError:
        print("WARNING: global_land_mask not available, will check all cells")
        use_land_mask = False
    
    # Classify cells as water or land
    print(f"\nClassifying cells (water vs land)...")
    start_classify = time.time()
    
    water_mask = np.zeros((n_lat, n_lon), dtype=bool)
    
    for i, lat in enumerate(lat_grid):
        if i % 50 == 0:
            pct = 100 * i / n_lat
            print(f"  Classifying... {pct:.0f}% ({i}/{n_lat} rows)", flush=True)
        
        for j, lon in enumerate(lon_grid):
            if use_land_mask:
                # global_land_mask returns True for land
                is_land = globe.is_land(lat, lon)
                water_mask[i, j] = not is_land
            else:
                # Assume water if no mask available
                water_mask[i, j] = True
    
    n_water = np.sum(water_mask)
    classify_time = time.time() - start_classify
    print(f"  {n_water:,} water cells out of {n_cells:,} ({100*n_water/n_cells:.1f}%)")
    print(f"  Classification took {classify_time/60:.1f} minutes")
    
    # Build graph
    print(f"\nBuilding graph...")
    G = nx.DiGraph()
    
    # Add water nodes
    print(f"  Adding {n_water:,} water nodes...")
    for i, lat in enumerate(lat_grid):
        for j, lon in enumerate(lon_grid):
            if water_mask[i, j]:
                G.add_node((lat, lon))
    
    # 8-directional neighbors
    directions = [
        (-1, -1), (-1, 0), (-1, 1),
        (0, -1),           (0, 1),
        (1, -1),  (1, 0),  (1, 1)
    ]
    
    # Add edges (this is the slow part)
    print(f"  Building edges (this is the slow part)...")
    start_edges = time.time()
    edge_count = 0
    # Adaptive land check interval based on resolution
    # Coarser resolutions can use coarser land checks
    if resolution >= 2.0:
        land_check_interval = 20000  # 20km for 2.0° resolution
    elif resolution >= 1.0:
        land_check_interval = 10000  # 10km for 1.0° resolution
    elif resolution >= 0.25:
        land_check_interval = 5000   # 5km for 0.25° resolution
    else:
        land_check_interval = 2000   # 2km for finer resolutions
    
    print(f"  Using land check interval: {land_check_interval/1000:.0f}km")
    
    total_water_cells = n_water
    processed = 0
    last_report = 0
    
    for i, lat in enumerate(lat_grid):
        for j, lon in enumerate(lon_grid):
            if not water_mask[i, j]:
                continue
            
            processed += 1
            
            # Progress report every 5%
            pct = 100 * processed / total_water_cells
            if pct >= last_report + 5:
                elapsed = time.time() - start_edges
                rate = processed / elapsed if elapsed > 0 else 0
                remaining = (total_water_cells - processed) / rate if rate > 0 else 0
                print(f"  Building graph... {pct:.0f}% ({edge_count:,} edges) - "
                      f"elapsed: {elapsed/60:.1f}min, ETA: {remaining/60:.1f}min", flush=True)
                last_report = pct
            
            node = (lat, lon)
            
            for di, dj in directions:
                ni, nj = i + di, j + dj
                
                # Handle longitude wrap-around
                if nj < 0:
                    nj = n_lon - 1
                elif nj >= n_lon:
                    nj = 0
                
                # Skip out of latitude bounds
                if ni < 0 or ni >= n_lat:
                    continue
                
                # Skip if neighbor is land
                if not water_mask[ni, nj]:
                    continue
                
                neighbor_lat = lat_grid[ni]
                neighbor_lon = lon_grid[nj]
                neighbor = (neighbor_lat, neighbor_lon)
                
                # Calculate geodesic distance
                inv = geod.Inverse(lat, lon, neighbor_lat, neighbor_lon)
                distance = inv['s12']  # meters
                
                # Basic land crossing check (sample along edge)
                crosses_land = False
                if use_land_mask:
                    line = geod.InverseLine(lat, lon, neighbor_lat, neighbor_lon)
                    n_samples = max(2, int(ceil(line.s13 / land_check_interval)))
                    for k in range(1, n_samples):
                        s = k * line.s13 / n_samples
                        pos = line.Position(s)
                        if globe.is_land(pos['lat2'], pos['lon2']):
                            crosses_land = True
                            break
                
                if not crosses_land:
                    G.add_edge(node, neighbor, distance=distance)
                    edge_count += 1
    
    edge_time = time.time() - start_edges
    print(f"  Built {edge_count:,} edges in {edge_time/60:.1f} minutes")
    
    # Save graph
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    
    res_str = f"{resolution:.2f}".replace(".", "_")
    filename = f"GLOBAL_OCEAN_{res_str}deg.gpickle"
    filepath = output_path / filename
    
    print(f"\nSaving graph to: {filepath}")
    start_save = time.time()
    
    data = {
        'graph': G,
        'lat_grid': lat_grid,
        'lon_grid': lon_grid,
        'resolution': resolution,
        'bounds': {'lat_min': lat_min, 'lat_max': lat_max, 'lon_min': lon_min, 'lon_max': lon_max},
        'built_at': datetime.now().isoformat(),
    }
    
    with open(filepath, 'wb') as f:
        pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)
    
    file_size = filepath.stat().st_size / (1024 * 1024)
    save_time = time.time() - start_save
    print(f"  Saved in {save_time:.1f}s - {file_size:.1f} MB")
    
    # Summary
    total_time = time.time() - start_classify
    print("\n" + "=" * 70)
    print("BUILD COMPLETE")
    print("=" * 70)
    print(f"Graph: {G.number_of_nodes():,} nodes, {G.number_of_edges():,} edges")
    print(f"File: {filepath}")
    print(f"Size: {file_size:.1f} MB")
    print(f"Total time: {total_time/60:.1f} minutes ({total_time/3600:.2f} hours)")
    print(f"Finished at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)
    
    return filepath


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build global ocean routing graph")
    parser.add_argument("--resolution", type=float, default=0.25,
                        help="Grid resolution in degrees (default: 0.25 = ~28km)")
    parser.add_argument("--output-dir", type=str, default="/tmp/wrt_graph_cache",
                        help="Output directory for graph file")
    
    args = parser.parse_args()
    
    print(f"\n{'*' * 70}")
    print(f"*  WARNING: This will take 1-4 hours depending on resolution!")
    print(f"*  Resolution {args.resolution}° selected")
    print(f"{'*' * 70}\n")
    
    build_global_graph(resolution=args.resolution, output_dir=args.output_dir)
