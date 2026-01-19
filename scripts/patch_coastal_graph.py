#!/usr/bin/env python3
"""
Patch isolated coastal components in the Pacific NW graph by connecting
them to the main ocean through narrow passages.

This handles places like:
- Sechelt Inlet (via Skookumchuck Narrows - ~200m wide)
- Jervis Inlet
- Inside Passage narrow channels
- Alaska's tight passages
"""

import argparse
import pickle
from pathlib import Path

import networkx as nx
from geographiclib.geodesic import Geodesic

geod = Geodesic.WGS84

GRAPH_CACHE_DIR = Path("/tmp/wrt_graph_cache")

# Manual connections for narrow passages
# Format: (isolated_lat, isolated_lon, main_ocean_lat, main_ocean_lon, passage_name)
MANUAL_CONNECTIONS = [
    # Sechelt Inlet - connect through Skookumchuck Narrows
    # The inlet is around (49.5, -123.76), main ocean is at (49.75, -124.0)
    (49.50, -123.76, 49.76, -124.04, "Skookumchuck Narrows -> Sechelt Inlet"),
    
    # Jervis Inlet entrance
    (49.80, -123.92, 49.82, -124.08, "Jervis Inlet entrance"),
    
    # Salmon Arm (Shuswap Lake is fresh water, but Salmon Inlet on the coast)
    (49.72, -123.76, 49.74, -123.92, "Salmon Inlet entrance"),
    
    # Princess Louisa Inlet (very narrow entrance)
    (50.16, -123.78, 50.20, -123.94, "Princess Louisa Inlet"),
    
    # Toba Inlet
    (50.30, -124.12, 50.28, -124.32, "Toba Inlet entrance"),
    
    # Bute Inlet  
    (50.62, -124.78, 50.56, -124.96, "Bute Inlet entrance"),
    
    # Knight Inlet
    (50.72, -125.56, 50.66, -125.78, "Knight Inlet entrance"),
    
    # Alaska Inside Passage connections
    # Juneau area - connect through Gastineau Channel
    (58.30, -134.44, 58.20, -134.20, "Gastineau Channel -> Juneau"),
    
    # Prince Rupert - connect through narrow passage
    (54.32, -130.32, 54.28, -130.52, "Prince Rupert harbour entrance"),
    
    # Ketchikan
    (55.34, -131.64, 55.32, -131.82, "Ketchikan channel"),
]


def find_nearest_node(G, lat, lon, tolerance=0.1):
    """Find the nearest node in graph to given coordinates."""
    best_node = None
    best_dist = float('inf')
    
    for node in G.nodes():
        d = abs(node[0] - lat) + abs(node[1] - lon)
        if d < best_dist:
            best_dist = d
            best_node = node
    
    if best_dist > tolerance:
        return None
    return best_node


def patch_graph(graph_path: Path, output_path: Path = None):
    """
    Load graph, connect isolated components through narrow passages.
    """
    print(f"Loading graph from {graph_path}...")
    with open(graph_path, 'rb') as f:
        data = pickle.load(f)
    
    G = data['graph']
    resolution = data.get('resolution', 0.02)
    
    print(f"Graph: {G.number_of_nodes():,} nodes, {G.number_of_edges():,} edges")
    
    # Find main ocean component
    components = list(nx.weakly_connected_components(G))
    main_ocean = max(components, key=len)
    print(f"Main ocean component: {len(main_ocean):,} nodes")
    print(f"Isolated components: {len(components) - 1}")
    
    # Track connections made
    connections_made = []
    
    print("\nPatching narrow passages...")
    for isolated_lat, isolated_lon, main_lat, main_lon, name in MANUAL_CONNECTIONS:
        # Find nodes near these coordinates
        isolated_node = find_nearest_node(G, isolated_lat, isolated_lon, tolerance=0.2)
        main_node = find_nearest_node(G, main_lat, main_lon, tolerance=0.2)
        
        if not isolated_node:
            print(f"  {name}: No node found near isolated point ({isolated_lat}, {isolated_lon})")
            continue
        if not main_node:
            print(f"  {name}: No node found near main ocean point ({main_lat}, {main_lon})")
            continue
        
        # Check if isolated node is actually isolated
        isolated_in_main = isolated_node in main_ocean
        main_in_main = main_node in main_ocean
        
        if isolated_in_main:
            print(f"  {name}: Already connected! (isolated point is in main ocean)")
            continue
        if not main_in_main:
            print(f"  {name}: Main ocean point is also isolated - skipping")
            continue
        
        # Calculate distance
        g = geod.Inverse(isolated_node[0], isolated_node[1], main_node[0], main_node[1])
        dist_km = g['s12'] / 1000.0
        
        # Add bidirectional edge
        G.add_edge(isolated_node, main_node, weight=dist_km, patched=True)
        G.add_edge(main_node, isolated_node, weight=dist_km, patched=True)
        
        # Update main_ocean set (in case we need to patch more connections)
        # Find the component containing isolated_node and merge it
        for comp in components:
            if isolated_node in comp:
                main_ocean.update(comp)
                break
        
        connections_made.append(name)
        print(f"  ✓ {name}: Connected {isolated_node} <-> {main_node} ({dist_km:.2f} km)")
    
    print(f"\nConnections made: {len(connections_made)}")
    
    # Verify connectivity improved
    new_components = list(nx.weakly_connected_components(G))
    new_main = max(new_components, key=len)
    print(f"After patching: {len(new_main):,} nodes in main ocean ({len(new_components)} components)")
    
    # Check key locations again
    print("\nVerifying key locations:")
    key_locations = [
        ("Vancouver", 49.0, -123.0),
        ("Victoria", 48.4, -123.4),
        ("Seattle", 47.6, -122.3),
        ("Sechelt", 49.5, -123.76),
        ("Jervis Inlet", 49.9, -123.9),
        ("Powell River", 49.9, -124.5),
        ("Juneau", 58.3, -134.44),
        ("Prince Rupert", 54.3, -130.3),
    ]
    
    for name, lat, lon in key_locations:
        node = find_nearest_node(G, lat, lon, tolerance=0.3)
        if node:
            in_main = node in new_main
            status = "CONNECTED" if in_main else "isolated"
            print(f"  {name}: {status}")
        else:
            print(f"  {name}: NO NODE")
    
    # Save patched graph
    if output_path is None:
        output_path = graph_path.with_suffix('.patched.gpickle')
    
    data['graph'] = G
    data['patched'] = True
    data['connections_patched'] = connections_made
    
    with open(output_path, 'wb') as f:
        pickle.dump(data, f, protocol=pickle.HIGHEST_PROTOCOL)
    
    size_mb = output_path.stat().st_size / (1024 * 1024)
    print(f"\nSaved patched graph to {output_path} ({size_mb:.1f} MB)")
    
    return G


def main():
    parser = argparse.ArgumentParser(description="Patch isolated coastal components")
    parser.add_argument(
        "--input", type=str, 
        default=str(GRAPH_CACHE_DIR / "PACIFIC_NW_0_02deg.gpickle"),
        help="Input graph file"
    )
    parser.add_argument(
        "--output", type=str, default=None,
        help="Output file (default: input with .patched suffix)"
    )
    
    args = parser.parse_args()
    
    input_path = Path(args.input)
    output_path = Path(args.output) if args.output else None
    
    if not input_path.exists():
        print(f"Error: Input file not found: {input_path}")
        return 1
    
    patch_graph(input_path, output_path)
    print("\nDone!")
    return 0


if __name__ == "__main__":
    exit(main())
