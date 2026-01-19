#!/usr/bin/env python3
"""
Build a high-resolution ocean graph for the Pacific Northwest region.

This covers:
- Puget Sound
- Strait of Georgia  
- Juan de Fuca Strait
- Inside Passage (BC coast up to Alaska)
- Sechelt Inlet, Jervis Inlet, Howe Sound
- Desolation Sound, Discovery Passage
- All the beautiful BC inlets and channels

Resolution: 0.02° (~2.2km) to navigate narrow passages
"""

import argparse
import pickle
import sys
import time
from datetime import datetime
from math import ceil
from pathlib import Path

import networkx as nx
import numpy as np
from geographiclib.geodesic import Geodesic
from global_land_mask import is_land

geod = Geodesic.WGS84

# Pacific Northwest region bounds
# From northern Washington to southern Alaska Inside Passage
REGION_BOUNDS = {
    'lat_min': 47.0,   # South of Seattle
    'lat_max': 60.0,   # Into Alaska
    'lon_min': -140.0, # West of Juneau
    'lon_max': -122.0, # East to include Puget Sound
}

# Default resolution for the regional graph
DEFAULT_RESOLUTION = 0.02  # ~2.2km - fine enough for inlets

# Cache directory
GRAPH_CACHE_DIR = Path("/tmp/wrt_graph_cache")


def edge_crosses_land(lat1: float, lon1: float, lat2: float, lon2: float, 
                      interval: float = 500) -> bool:
    """
    Check if a geodesic edge crosses land by sampling points along it.
    
    Args:
        lat1, lon1: Start point
        lat2, lon2: End point  
        interval: Sampling interval in meters (500m for fine resolution)
    """
    line = geod.InverseLine(lat1, lon1, lat2, lon2)
    n = max(2, int(ceil(line.s13 / interval)))
    
    for i in range(n + 1):
        s = min(interval * i, line.s13)
        g = line.Position(s, Geodesic.STANDARD | Geodesic.LONG_UNROLL)
        if is_land(g['lat2'], g['lon2']):
            return True
    return False


def build_pacific_nw_graph(resolution: float = DEFAULT_RESOLUTION,
                           bounds: dict = None) -> nx.DiGraph:
    """
    Build a high-resolution ocean graph for the Pacific Northwest.
    
    Args:
        resolution: Grid resolution in degrees (default 0.02°)
        bounds: Region bounds dict with lat_min, lat_max, lon_min, lon_max
        
    Returns:
        NetworkX DiGraph with ocean nodes and edges
    """
    if bounds is None:
        bounds = REGION_BOUNDS
        
    lat_min = bounds['lat_min']
    lat_max = bounds['lat_max']
    lon_min = bounds['lon_min']
    lon_max = bounds['lon_max']
    
    print(f"Building Pacific NW graph at {resolution}° resolution")
    print(f"Region: ({lat_min}, {lon_min}) to ({lat_max}, {lon_max})")
    
    # Generate grid
    lats = np.arange(lat_min, lat_max + resolution, resolution)
    lons = np.arange(lon_min, lon_max + resolution, resolution)
    
    n_lats = len(lats)
    n_lons = len(lons)
    total_cells = n_lats * n_lons
    
    print(f"Grid size: {n_lats} x {n_lons} = {total_cells:,} cells")
    
    # Create graph
    G = nx.DiGraph()
    
    # Phase 1: Identify water cells
    print("Phase 1: Classifying water vs land cells...")
    water_cells = set()
    
    start_time = time.time()
    for i, lat in enumerate(lats):
        for j, lon in enumerate(lons):
            if not is_land(lat, lon):
                water_cells.add((lat, lon))
                G.add_node((lat, lon))
        
        # Progress update
        if (i + 1) % 50 == 0 or i == len(lats) - 1:
            pct = 100 * (i + 1) / n_lats
            elapsed = time.time() - start_time
            eta = elapsed / (i + 1) * (n_lats - i - 1)
            print(f"  {pct:.1f}% - {len(water_cells):,} water cells found "
                  f"(elapsed: {elapsed:.1f}s, ETA: {eta:.1f}s)")
    
    print(f"Found {len(water_cells):,} water cells ({100*len(water_cells)/total_cells:.1f}%)")
    
    # Phase 2: Build edges with 8-neighbor connectivity
    print("\nPhase 2: Building edges...")
    
    # 8-direction neighbors (including diagonals)
    directions = [
        (-1, -1), (-1, 0), (-1, 1),
        (0, -1),           (0, 1),
        (1, -1),  (1, 0),  (1, 1)
    ]
    
    edge_count = 0
    checked_count = 0
    start_time = time.time()
    
    water_list = list(water_cells)
    total_water = len(water_list)
    
    for idx, (lat, lon) in enumerate(water_list):
        # Find grid indices
        lat_idx = int(round((lat - lat_min) / resolution))
        lon_idx = int(round((lon - lon_min) / resolution))
        
        for dlat, dlon in directions:
            new_lat_idx = lat_idx + dlat
            new_lon_idx = lon_idx + dlon
            
            if 0 <= new_lat_idx < n_lats and 0 <= new_lon_idx < n_lons:
                neighbor_lat = lats[new_lat_idx]
                neighbor_lon = lons[new_lon_idx]
                neighbor = (neighbor_lat, neighbor_lon)
                
                if neighbor in water_cells:
                    checked_count += 1
                    
                    # Check if edge crosses land (fine sampling for narrow passages)
                    if not edge_crosses_land(lat, lon, neighbor_lat, neighbor_lon, 
                                            interval=500):  # 500m sampling
                        # Calculate edge distance
                        g = geod.Inverse(lat, lon, neighbor_lat, neighbor_lon)
                        dist_km = g['s12'] / 1000.0
                        
                        G.add_edge((lat, lon), neighbor, weight=dist_km)
                        edge_count += 1
        
        # Progress update
        if (idx + 1) % 5000 == 0 or idx == total_water - 1:
            pct = 100 * (idx + 1) / total_water
            elapsed = time.time() - start_time
            eta = elapsed / (idx + 1) * (total_water - idx - 1) if idx > 0 else 0
            print(f"  {pct:.1f}% - {edge_count:,} edges "
                  f"(elapsed: {elapsed/60:.1f}min, ETA: {eta/60:.1f}min)")
    
    print(f"\nGraph complete: {G.number_of_nodes():,} nodes, {G.number_of_edges():,} edges")
    
    return G, lats, lons


def analyze_connectivity(G: nx.DiGraph):
    """Analyze graph connectivity and report isolated components."""
    print("\nAnalyzing connectivity...")
    
    components = list(nx.weakly_connected_components(G))
    print(f"Number of connected components: {len(components)}")
    
    # Sort by size
    components.sort(key=len, reverse=True)
    
    print("\nLargest components:")
    for i, comp in enumerate(components[:10]):
        # Sample some nodes
        sample = list(comp)[:3]
        print(f"  {i+1}. {len(comp):,} nodes - sample: {sample}")
    
    # Check key locations
    key_locations = [
        ("Vancouver", 49.0, -123.0),
        ("Victoria", 48.4, -123.4),
        ("Seattle", 47.6, -122.3),
        ("Sechelt", 49.5, -123.75),
        ("Powell River", 49.9, -124.5),
        ("Juneau", 58.3, -134.4),
        ("Prince Rupert", 54.3, -130.3),
        ("Juan de Fuca Strait", 48.4, -124.5),
        ("Open Pacific", 48.0, -126.0),
    ]
    
    main_ocean = components[0] if components else set()
    
    print("\nKey location connectivity:")
    for name, lat, lon in key_locations:
        # Find nearest node
        best_node = None
        best_dist = float('inf')
        for node in G.nodes():
            d = abs(node[0] - lat) + abs(node[1] - lon)
            if d < best_dist:
                best_dist = d
                best_node = node
        
        if best_node and best_dist < 0.5:
            in_main = best_node in main_ocean
            status = "CONNECTED" if in_main else "ISOLATED"
            print(f"  {name}: {status} (node: {best_node})")
        else:
            print(f"  {name}: NO NODE FOUND within 0.5°")
    
    return components


def save_graph(G: nx.DiGraph, lats: np.ndarray, lons: np.ndarray, 
               resolution: float, bounds: dict, output_path: Path):
    """Save graph to pickle file."""
    data = {
        'graph': G,
        'lat_grid': lats,
        'lon_grid': lons,
        'resolution': resolution,
        'bounds': bounds,
        'region': 'pacific_northwest',
        'built_at': datetime.now().isoformat(),
    }
    
    with open(output_path, 'wb') as f:
        pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)
    
    size_mb = output_path.stat().st_size / (1024 * 1024)
    print(f"\nSaved to {output_path} ({size_mb:.1f} MB)")


def main():
    parser = argparse.ArgumentParser(
        description="Build high-resolution Pacific NW ocean graph"
    )
    parser.add_argument(
        "--resolution", type=float, default=DEFAULT_RESOLUTION,
        help=f"Grid resolution in degrees (default: {DEFAULT_RESOLUTION})"
    )
    parser.add_argument(
        "--output", type=str, default=None,
        help="Output file path (default: auto-generated)"
    )
    parser.add_argument(
        "--analyze-only", action="store_true",
        help="Only analyze existing graph, don't build"
    )
    
    args = parser.parse_args()
    
    # Ensure cache directory exists
    GRAPH_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    
    # Generate output filename
    res_str = f"{args.resolution:.2f}".replace(".", "_")
    if args.output:
        output_path = Path(args.output)
    else:
        output_path = GRAPH_CACHE_DIR / f"PACIFIC_NW_{res_str}deg.gpickle"
    
    if args.analyze_only:
        if output_path.exists():
            print(f"Loading existing graph from {output_path}...")
            with open(output_path, 'rb') as f:
                data = pickle.load(f)
            G = data['graph']
            analyze_connectivity(G)
        else:
            print(f"No graph found at {output_path}")
        return
    
    print("=" * 60)
    print("Pacific Northwest High-Resolution Ocean Graph Builder")
    print("=" * 60)
    print()
    
    # Build the graph
    G, lats, lons = build_pacific_nw_graph(
        resolution=args.resolution,
        bounds=REGION_BOUNDS
    )
    
    # Analyze connectivity
    analyze_connectivity(G)
    
    # Save
    save_graph(G, lats, lons, args.resolution, REGION_BOUNDS, output_path)
    
    print("\nDone!")


if __name__ == "__main__":
    main()
