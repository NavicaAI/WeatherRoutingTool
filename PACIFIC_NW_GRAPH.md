# Pacific Northwest High-Resolution Ocean Graph

## Overview

A pre-built high-resolution (0.02° / ~2.2km) ocean routing graph for the Pacific Northwest region, enabling routing through intricate coastal passages like BC's inlets, Alaska's Inside Passage, and Puget Sound.

## Graph Details

| Property | Value |
|----------|-------|
| Resolution | 0.02° (~2.2 km) |
| Region | 47°N - 60°N, 140°W - 122°W |
| Total Nodes | 307,882 |
| Total Edges | 2,407,137 |
| Main Ocean Component | 305,693 nodes |
| File Size | ~184 MB |
| File Path | `/tmp/wrt_graph_cache/PACIFIC_NW_0_02deg.patched.gpickle` |

## Connectivity

### Major Ports - All Connected ✓
- **British Columbia**: Vancouver, Victoria, Nanaimo, Prince Rupert, Powell River, Comox, Campbell River, Port Hardy, Sechelt, Squamish, Tofino
- **Alaska**: Juneau, Ketchikan, Sitka, Skagway
- **Washington**: Seattle, Bellingham, Port Angeles

### Manually Patched Narrow Passages
Some passages are narrower than the 2.2km grid resolution, so manual edges were added:
- Gastineau Channel → Juneau (17.96 km edge)
- Prince Rupert harbour entrance (13.76 km edge)
- Ketchikan channel (15.39 km edge)
- Vancouver First Narrows / Burrard Inlet (4.89 km edge)
- Ketchikan harbor to Tongass Narrows (6.73 km edge)

### Not Accessible by Ocean
- **Salmon Arm**: On Shuswap Lake (freshwater, landlocked)
- **Olympia**: Too far inland in Puget Sound (no water nodes at this resolution)

## Usage

The graph is automatically used when routing within the Pacific NW region:

```python
# In local_server.py - resolution selection
if is_route_in_pacific_nw(route_points):
    return 0.02  # Use Pacific NW regional graph

# In astar_router.py - graph loading
regional_result = _find_regional_graph(lat_min, lat_max, lon_min, lon_max)
if regional_result:
    regional_path, region_name = regional_result
    # Uses PACIFIC_NW_0_02deg.patched.gpickle
```

## Building/Rebuilding

### Build the base graph:
```bash
cd /home/insectile/Development/Navica/WeatherRoutingTool
source .venv/bin/activate
python scripts/build_pacific_nw_graph.py --resolution 0.02
```

### Patch narrow passages:
```bash
python scripts/patch_coastal_graph.py /tmp/wrt_graph_cache/PACIFIC_NW_0_02deg.gpickle
```

## Files

- `scripts/build_pacific_nw_graph.py`: Builds the high-resolution graph
- `scripts/patch_coastal_graph.py`: Connects isolated coastal components through narrow passages
- `WeatherRoutingTool/algorithms/astar_router.py`: Contains `REGIONAL_GRAPHS` configuration

## Region Configuration

In `astar_router.py`:
```python
REGIONAL_GRAPHS = {
    "PACIFIC_NW": {
        "bounds": {
            "lat_min": 47.0,
            "lat_max": 60.0,
            "lon_min": -140.0,
            "lon_max": -122.0,
        },
        "resolution": 0.02,
        "pattern": "PACIFIC_NW_0_02deg.patched.gpickle",
    }
}
```

## Adding More Regions

To add another high-resolution regional graph:

1. Create a build script similar to `build_pacific_nw_graph.py`
2. Build and optionally patch the graph
3. Add the region to `REGIONAL_GRAPHS` in `astar_router.py`
4. Add the region check in `local_server.py`'s `select_astar_resolution()`

## Limitations

- Very narrow passages (< 200m wide) like Skookumchuck Narrows cannot be resolved even at 0.02°
- Small inland waterways and marinas are generally not included
- Graph must be rebuilt if coastline data changes
