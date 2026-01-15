"""
A* Weather Routing Algorithm

Optimized for weather-aware routing using:
1. Pre-computed graph topology (cached to disk)
2. Dynamic weather-based edge weights
3. A* search with great-circle heuristic
4. DiGraph for asymmetric wind/current costs
"""

import hashlib
import logging
import os
import pickle
import time
from datetime import datetime, timedelta
from math import ceil
from pathlib import Path
from typing import Optional, Tuple, Callable

import networkx as nx
import numpy as np
from astropy import units as u
from geographiclib.geodesic import Geodesic
from global_land_mask import is_land

from WeatherRoutingTool.algorithms.routingalg import RoutingAlg
from WeatherRoutingTool.constraints.constraints import ConstraintsList, LandPolygonsCrossing
from WeatherRoutingTool.routeparams import RouteParams
from WeatherRoutingTool.ship.ship import Boat
from WeatherRoutingTool.ship.shipparams import ShipParams
from WeatherRoutingTool.weather import WeatherCond

logger = logging.getLogger("WRT.astar")
geod = Geodesic.WGS84

# Default cache directory
GRAPH_CACHE_DIR = Path("/tmp/wrt_graph_cache")

# Global graph filename pattern
GLOBAL_GRAPH_PATTERN = "GLOBAL_OCEAN_{res}deg.gpickle"


def _find_global_graph(resolution: float) -> Optional[Path]:
    """Find a pre-built global ocean graph matching the resolution."""
    # Format resolution string (e.g., 0.25 -> "0_25")
    res_str = f"{resolution:.2f}".replace(".", "_")
    global_path = GRAPH_CACHE_DIR / f"GLOBAL_OCEAN_{res_str}deg.gpickle"
    if global_path.exists():
        return global_path
    return None


class AStarRouter(RoutingAlg):
    """
    A* routing algorithm with pre-computed graph topology and dynamic weather weights.
    
    Key features:
    - Pre-computes and caches graph topology (which nodes connect)
    - Applies weather-based edge costs at query time
    - Uses DiGraph for asymmetric wind/current effects
    - A* with great-circle heuristic for efficient search
    """
    
    def __init__(self, config):
        super().__init__(config)
        self.config = config
        
        # Grid parameters
        self.grid_resolution = getattr(config, 'ASTAR_GRID_RESOLUTION', 0.05)  # ~5km default
        self.nof_neighbors = getattr(config, 'ASTAR_NOF_NEIGHBORS', 1)
        self.land_check_interval = getattr(config, 'ASTAR_LAND_CHECK_INTERVAL', 1000)  # meters
        
        # Weather parameters
        self.use_weather = getattr(config, 'ASTAR_USE_WEATHER', True)
        
        # Region bounds - can be set from config, or will be computed dynamically from route
        # If not set, _ensure_graph_covers_route() will compute optimal bounds
        config_lat_min = getattr(config, 'ASTAR_LAT_MIN', None)
        config_lat_max = getattr(config, 'ASTAR_LAT_MAX', None)
        config_lon_min = getattr(config, 'ASTAR_LON_MIN', None)
        config_lon_max = getattr(config, 'ASTAR_LON_MAX', None)
        
        if config_lat_min is not None and config_lat_max is not None:
            # Use configured bounds
            self.lat_min = config_lat_min
            self.lat_max = config_lat_max
            self.lon_min = config_lon_min or self.map_ext.lon1
            self.lon_max = config_lon_max or self.map_ext.lon2
        else:
            # No configured bounds - start with minimal bounds around start/finish
            # These will be expanded by _ensure_graph_covers_route() at routing time
            self.lat_min = min(self.start[0], self.finish[0]) - 1.0
            self.lat_max = max(self.start[0], self.finish[0]) + 1.0
            self.lon_min = min(self.start[1], self.finish[1]) - 1.0
            self.lon_max = max(self.start[1], self.finish[1]) + 1.0
            logger.info(f"A*: Dynamic bounds enabled - will expand based on route")
        
        # Ensure correct ordering
        self.lat_min, self.lat_max = min(self.lat_min, self.lat_max), max(self.lat_min, self.lat_max)
        self.lon_min, self.lon_max = min(self.lon_min, self.lon_max), max(self.lon_min, self.lon_max)
        
        # Polygon-based land detection
        self.land_polygon_detector = None
        if hasattr(config, 'CONSTRAINTS_LIST') and 'land_crossing_polygons' in getattr(config, 'CONSTRAINTS_LIST', []):
            try:
                logger.info("A*: Initializing polygon-based land detection")
                self.land_polygon_detector = LandPolygonsCrossing(map_size=self.map_ext)
                if self.land_polygon_detector.initialization_successful:
                    logger.info("A*: Polygon-based land detection enabled")
                else:
                    self.land_polygon_detector = None
            except Exception as e:
                logger.warning(f"A*: Could not initialize polygon detection: {e}")
        
        # Graph will be loaded/built on first use
        self.graph: Optional[nx.DiGraph] = None
        self.lat_grid: Optional[np.ndarray] = None
        self.lon_grid: Optional[np.ndarray] = None
        
        # Weather/boat references (set during execute_routing)
        self.weather: Optional[WeatherCond] = None
        self.boat: Optional[Boat] = None
        
        # Wind cache for performance (grid-based)
        self._wind_cache: Optional[dict] = None
        self._wind_cache_time: Optional[datetime] = None
        
        # Weather avoidance thresholds
        self.max_wave_height_m = getattr(config, 'ASTAR_MAX_WAVE_HEIGHT_M', None)
        self.max_wind_speed_kts = getattr(config, 'ASTAR_MAX_WIND_SPEED_KTS', None)
        self.max_current_speed_kts = getattr(config, 'ASTAR_MAX_CURRENT_SPEED_KTS', None)
        self.weather_penalty_factor = getattr(config, 'ASTAR_WEATHER_PENALTY_FACTOR', 10.0)
        
        # Dynamic corridor settings
        # corridor_fraction: fraction of route distance to use as corridor width (default 0.3 = 30%)
        # corridor_min_km: minimum corridor width in km (default 100km)
        # If both are None, no corridor filtering is applied
        self.corridor_fraction = getattr(config, 'ASTAR_CORRIDOR_FRACTION', 0.3)
        self.corridor_min_km = getattr(config, 'ASTAR_CORRIDOR_MIN_KM', 100.0)
        self.use_corridor = getattr(config, 'ASTAR_USE_CORRIDOR', True)  # Enable/disable corridor filtering
        
        # Track hazardous nodes and encountered hazards during routing
        self._hazardous_nodes: set = set()
        self._hazards_encountered: list = []  # List of hazard dicts for reporting
        
    def print_init(self):
        logger.info(f"A* Router initialized:")
        logger.info(f"  Region: ({self.lat_min:.2f}, {self.lon_min:.2f}) to ({self.lat_max:.2f}, {self.lon_max:.2f})")
        logger.info(f"  Resolution: {self.grid_resolution}° (~{self.grid_resolution * 111:.1f} km)")
        logger.info(f"  Weather-aware: {self.use_weather}")
        if self.max_wave_height_m or self.max_wind_speed_kts or self.max_current_speed_kts:
            thresholds = []
            if self.max_wave_height_m:
                thresholds.append(f"wave>{self.max_wave_height_m}m")
            if self.max_wind_speed_kts:
                thresholds.append(f"wind>{self.max_wind_speed_kts}kts")
            if self.max_current_speed_kts:
                thresholds.append(f"current>{self.max_current_speed_kts}kts")
            logger.info(f"  Weather avoidance: {', '.join(thresholds)}")
        if self.use_corridor:
            logger.info(f"  Corridor: {self.corridor_fraction*100:.0f}% of route or {self.corridor_min_km}km min")
    
    def init_fig(self, **kwargs):
        pass
    
    def _get_cache_path(self) -> Path:
        """Get the cache file path based on region and resolution."""
        GRAPH_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        
        # Create unique hash based on parameters
        params = f"{self.lat_min}_{self.lat_max}_{self.lon_min}_{self.lon_max}_{self.grid_resolution}_{self.nof_neighbors}"
        param_hash = hashlib.md5(params.encode()).hexdigest()[:12]
        
        return GRAPH_CACHE_DIR / f"astar_graph_{param_hash}.gpickle"
    
    def _extract_subgraph_from_global(self, global_path: Path) -> Optional[nx.DiGraph]:
        """
        Extract a subgraph from the global ocean graph for the current bounds.
        Returns None if extraction fails.
        """
        try:
            start = time.time()
            logger.info(f"A*: Loading global graph from {global_path}")
            print(f"[A*] Loading global ocean graph...", flush=True)
            
            with open(global_path, 'rb') as f:
                global_data = pickle.load(f)
            
            global_graph = global_data['graph']
            global_lat_grid = global_data['lat_grid']
            global_lon_grid = global_data['lon_grid']
            
            load_time = time.time() - start
            logger.info(f"A*: Loaded global graph in {load_time:.1f}s - {global_graph.number_of_nodes()} nodes")
            print(f"[A*] Loaded global graph in {load_time:.1f}s - {global_graph.number_of_nodes()} nodes", flush=True)
            
            # Find nodes within our bounds (with small buffer for edge connectivity)
            buffer = self.grid_resolution * 2
            lat_min_buf = self.lat_min - buffer
            lat_max_buf = self.lat_max + buffer
            lon_min_buf = self.lon_min - buffer
            lon_max_buf = self.lon_max + buffer
            
            # Extract nodes in bounds
            nodes_in_bounds = [
                node for node in global_graph.nodes()
                if lat_min_buf <= node[0] <= lat_max_buf and lon_min_buf <= node[1] <= lon_max_buf
            ]
            
            if len(nodes_in_bounds) == 0:
                logger.warning("A*: No global graph nodes found in bounds")
                return None
            
            # Create subgraph
            subgraph = global_graph.subgraph(nodes_in_bounds).copy()
            
            # Extract lat/lon grids for this region
            self.lat_grid = np.array(sorted(set(n[0] for n in nodes_in_bounds)))
            self.lon_grid = np.array(sorted(set(n[1] for n in nodes_in_bounds)))
            
            extract_time = time.time() - start - load_time
            logger.info(f"A*: Extracted subgraph in {extract_time:.1f}s - {subgraph.number_of_nodes()} nodes, {subgraph.number_of_edges()} edges")
            print(f"[A*] Extracted subgraph: {subgraph.number_of_nodes()} nodes, {subgraph.number_of_edges()} edges (from global)", flush=True)
            
            return subgraph
            
        except Exception as e:
            logger.warning(f"A*: Failed to extract from global graph: {e}")
            return None
    
    def _load_or_build_graph(self) -> nx.DiGraph:
        """Load graph from cache, extract from global graph, or build it."""
        cache_path = self._get_cache_path()
        
        # Option 1: Use regional cache if available
        if cache_path.exists():
            logger.info(f"A*: Loading cached graph from {cache_path}")
            start = time.time()
            with open(cache_path, 'rb') as f:
                data = pickle.load(f)
            self.graph = data['graph']
            self.lat_grid = data['lat_grid']
            self.lon_grid = data['lon_grid']
            logger.info(f"A*: Loaded graph in {time.time() - start:.1f}s - {self.graph.number_of_nodes()} nodes, {self.graph.number_of_edges()} edges")
            print(f"[A*] Loaded cached graph: {self.graph.number_of_nodes()} nodes, {self.graph.number_of_edges()} edges", flush=True)
            return self.graph
        
        # Option 2: Try to extract from global graph (much faster than building)
        global_path = _find_global_graph(self.grid_resolution)
        if global_path:
            subgraph = self._extract_subgraph_from_global(global_path)
            if subgraph is not None:
                self.graph = subgraph
                # Cache the extracted subgraph for future use
                cache_data = {
                    'graph': self.graph,
                    'lat_grid': self.lat_grid,
                    'lon_grid': self.lon_grid,
                }
                with open(cache_path, 'wb') as f:
                    pickle.dump(cache_data, f, protocol=pickle.HIGHEST_PROTOCOL)
                logger.info(f"A*: Saved extracted subgraph to cache: {cache_path}")
                return self.graph
        
        # Option 3: Build from scratch (slowest)
        logger.info(f"A*: Building graph (this may take several minutes)...")
        print(f"[A*] Building graph (this may take 30-40 minutes for large regions)...", flush=True)
        start = time.time()
        
        # Build grid
        self.lat_grid = np.arange(self.lat_min, self.lat_max, self.grid_resolution)
        self.lon_grid = np.arange(self.lon_min, self.lon_max, self.grid_resolution)
        
        logger.info(f"A*: Grid size: {len(self.lat_grid)} x {len(self.lon_grid)} = {len(self.lat_grid) * len(self.lon_grid)} cells")
        print(f"[A*] Grid size: {len(self.lat_grid)} x {len(self.lon_grid)} = {len(self.lat_grid) * len(self.lon_grid)} cells", flush=True)
        
        # Build DiGraph (directed for asymmetric weights)
        self.graph = nx.DiGraph()
        
        # Pre-compute which cells are water
        print(f"[A*] Classifying cells (water vs land)...", flush=True)
        water_mask = np.zeros((len(self.lat_grid), len(self.lon_grid)), dtype=bool)
        for i, lat in enumerate(self.lat_grid):
            for j, lon in enumerate(self.lon_grid):
                water_mask[i, j] = not is_land(lat, lon)
        
        water_cells = np.sum(water_mask)
        logger.info(f"A*: {water_cells} water cells out of {water_mask.size} ({100*water_cells/water_mask.size:.1f}%)")
        print(f"[A*] {water_cells} water cells out of {water_mask.size} ({100*water_cells/water_mask.size:.1f}%)", flush=True)
        
        # Generate neighbor offsets
        neighbors = []
        for di in range(-self.nof_neighbors, self.nof_neighbors + 1):
            for dj in range(-self.nof_neighbors, self.nof_neighbors + 1):
                if di == 0 and dj == 0:
                    continue
                # Only keep "fundamental" directions (GCD = 1 to avoid duplicates)
                from math import gcd
                if gcd(abs(di), abs(dj)) == 1:
                    neighbors.append((di, dj))
        
        logger.info(f"A*: Using {len(neighbors)} neighbor directions")
        print(f"[A*] Using {len(neighbors)} neighbor directions", flush=True)
        print(f"[A*] Building edges (this is the slow part)...", flush=True)
        
        # Build edges
        edges_added = 0
        edges_checked = 0
        last_progress = 0
        
        for i in range(len(self.lat_grid)):
            # Progress logging
            progress = int(100 * i / len(self.lat_grid))
            if progress >= last_progress + 5:  # Report every 5%
                elapsed = time.time() - start
                eta = elapsed / max(progress, 1) * (100 - progress)
                logger.info(f"A*: Building graph... {progress}% ({edges_added} edges)")
                print(f"[A*] Building graph... {progress}% ({edges_added:,} edges) - elapsed: {elapsed/60:.1f}min, ETA: {eta/60:.1f}min", flush=True)
                last_progress = progress
            
            for j in range(len(self.lon_grid)):
                if not water_mask[i, j]:
                    continue
                
                lat = self.lat_grid[i]
                lon = self.lon_grid[j]
                node = (lat, lon)
                
                for di, dj in neighbors:
                    ni, nj = i + di, j + dj
                    
                    # Bounds check
                    if ni < 0 or ni >= len(self.lat_grid) or nj < 0 or nj >= len(self.lon_grid):
                        continue
                    
                    if not water_mask[ni, nj]:
                        continue
                    
                    nlat = self.lat_grid[ni]
                    nlon = self.lon_grid[nj]
                    neighbor = (nlat, nlon)
                    
                    edges_checked += 1
                    
                    # Check if edge crosses land
                    if self._edge_crosses_land(lat, lon, nlat, nlon):
                        continue
                    
                    # Add edge with distance (weights updated at query time)
                    distance = geod.Inverse(lat, lon, nlat, nlon)['s12']
                    
                    # Add both directions (DiGraph)
                    self.graph.add_edge(node, neighbor, distance=distance)
                    self.graph.add_edge(neighbor, node, distance=distance)
                    edges_added += 2
        
        build_time = time.time() - start
        logger.info(f"A*: Graph built in {build_time:.1f}s - {self.graph.number_of_nodes()} nodes, {self.graph.number_of_edges()} edges")
        logger.info(f"A*: Checked {edges_checked} potential edges")
        print(f"[A*] Graph built in {build_time/60:.1f} minutes - {self.graph.number_of_nodes():,} nodes, {self.graph.number_of_edges():,} edges", flush=True)
        
        # Save to cache
        logger.info(f"A*: Saving graph to {cache_path}")
        print(f"[A*] Saving graph to cache...", flush=True)
        with open(cache_path, 'wb') as f:
            pickle.dump({
                'graph': self.graph,
                'lat_grid': self.lat_grid,
                'lon_grid': self.lon_grid,
            }, f, protocol=pickle.HIGHEST_PROTOCOL)
        
        file_size = cache_path.stat().st_size / 1e6
        logger.info(f"A*: Saved ({file_size:.1f} MB)")
        
        return self.graph
    
    def _ensure_graph_covers_route(self, start: Tuple[float, float], end: Tuple[float, float], 
                                    waypoints: list = None) -> bool:
        """
        Ensure the graph covers the route, expanding bounds if needed.
        
        This enables routing to ANY location - if the current graph doesn't cover
        the requested route, we compute new bounds and load/build a graph for them.
        
        Args:
            start: (lat, lon) start point
            end: (lat, lon) end point
            waypoints: Optional intermediate waypoints
            
        Returns:
            True if graph was expanded (rebuilt), False if existing graph was sufficient
        """
        # Compute what bounds we need for this route (with corridor padding)
        needed_bounds = self._compute_corridor_bounds(start, end, waypoints)
        needed_lat_min, needed_lat_max, needed_lon_min, needed_lon_max = needed_bounds
        
        # Add extra padding beyond corridor for safety (allow some detour room)
        padding_deg = 0.5  # ~55km extra on each side
        needed_lat_min -= padding_deg
        needed_lat_max += padding_deg
        needed_lon_min -= padding_deg
        needed_lon_max += padding_deg
        
        # Check if current graph covers the needed area
        if self.graph is not None:
            covers_lat = (self.lat_min <= needed_lat_min and self.lat_max >= needed_lat_max)
            covers_lon = (self.lon_min <= needed_lon_min and self.lon_max >= needed_lon_max)
            
            if covers_lat and covers_lon:
                logger.info(f"A*: Current graph covers route ({self.lat_min:.1f}-{self.lat_max:.1f}, {self.lon_min:.1f}-{self.lon_max:.1f})")
                return False  # No expansion needed
            
            # Log what's missing
            logger.info(f"A*: Graph expansion needed:")
            logger.info(f"  Current: lat={self.lat_min:.1f}-{self.lat_max:.1f}, lon={self.lon_min:.1f}-{self.lon_max:.1f}")
            logger.info(f"  Needed: lat={needed_lat_min:.1f}-{needed_lat_max:.1f}, lon={needed_lon_min:.1f}-{needed_lon_max:.1f}")
        
        # Compute new bounds - union of current (if exists) and needed
        if self.graph is not None:
            new_lat_min = min(self.lat_min, needed_lat_min)
            new_lat_max = max(self.lat_max, needed_lat_max)
            new_lon_min = min(self.lon_min, needed_lon_min)
            new_lon_max = max(self.lon_max, needed_lon_max)
        else:
            new_lat_min = needed_lat_min
            new_lat_max = needed_lat_max
            new_lon_min = needed_lon_min
            new_lon_max = needed_lon_max
        
        # Round to 5° grid boundaries for better cache reuse
        # This means most regional routes will share the same cached graph
        # e.g., all Western Mediterranean routes use (30-45°N, 0-15°E)
        GRID_SIZE = 5.0  # degrees
        new_lat_min = np.floor(new_lat_min / GRID_SIZE) * GRID_SIZE
        new_lat_max = np.ceil(new_lat_max / GRID_SIZE) * GRID_SIZE
        new_lon_min = np.floor(new_lon_min / GRID_SIZE) * GRID_SIZE
        new_lon_max = np.ceil(new_lon_max / GRID_SIZE) * GRID_SIZE
        
        # Clamp to valid geographic bounds
        new_lat_min = max(new_lat_min, -90.0)
        new_lat_max = min(new_lat_max, 90.0)
        new_lon_min = max(new_lon_min, -180.0)
        new_lon_max = min(new_lon_max, 180.0)
        
        print(f"[A*] Expanding graph bounds: ({self.lat_min:.1f},{self.lon_min:.1f})-({self.lat_max:.1f},{self.lon_max:.1f}) -> ({new_lat_min:.1f},{new_lon_min:.1f})-({new_lat_max:.1f},{new_lon_max:.1f})", flush=True)
        
        # Update bounds
        self.lat_min = new_lat_min
        self.lat_max = new_lat_max
        self.lon_min = new_lon_min
        self.lon_max = new_lon_max
        
        # Clear current graph and load/build with new bounds
        self.graph = None
        self._load_or_build_graph()
        
        return True  # Graph was expanded

    def _edge_crosses_land(self, lat1: float, lon1: float, lat2: float, lon2: float) -> bool:
        """Check if an edge crosses land.
        
        For short edges (<100km), uses polygon detection if available.
        For long edges (>=100km), always uses geodesic point sampling because:
        - Polygon detection uses Cartesian LineStrings which diverge from geodesic paths
        - The actual ship route follows a great circle, not a straight lat/lon line
        """
        # Calculate edge length
        line = geod.InverseLine(lat1, lon1, lat2, lon2)
        edge_length_km = line.s13 / 1000.0
        
        # For short edges, polygon detection is accurate enough
        LONG_EDGE_THRESHOLD_KM = 100.0  # Above this, geodesic diverges significantly from Cartesian
        
        if edge_length_km < LONG_EDGE_THRESHOLD_KM and self.land_polygon_detector is not None:
            try:
                result = self.land_polygon_detector.check_crossing(
                    np.array([lat1]), np.array([lon1]),
                    np.array([lat2]), np.array([lon2])
                )
                if result is not None and len(result) > 0:
                    if result[0]:
                        return True  # Polygon detected crossing
                    # Polygon says no crossing - trust it for short edges
                    return False
            except Exception:
                pass  # Fall through to point sampling
        
        # For long edges, OR if polygon check failed, use geodesic point sampling
        # This correctly follows the great circle path the ship would actually take
        n = max(2, int(ceil(line.s13 / self.land_check_interval)))
        
        for i in range(n + 1):
            s = min(self.land_check_interval * i, line.s13)
            g = line.Position(s, Geodesic.STANDARD | Geodesic.LONG_UNROLL)
            if is_land(g['lat2'], g['lon2']):
                if edge_length_km > 200:  # Log for long edges
                    logger.warning(f"A*: Long edge ({edge_length_km:.0f}km) crosses land at ({g['lat2']:.2f}, {g['lon2']:.2f})")
                return True
        
        return False
    
    def _smooth_path(self, path: list, mandatory_points: list = None) -> list:
        """
        Smooth the path by:
        1. Removing collinear intermediate points (same direction)
        2. Shortcutting where possible without crossing land
        
        Mandatory points (like user-specified waypoints) are always preserved.
        
        Args:
            path: List of (lat, lon) tuples
            mandatory_points: List of (lat, lon) tuples that must be preserved
            
        Returns:
            Smoothed path with fewer waypoints
        """
        if len(path) <= 2:
            return path
        
        # Build set of mandatory points for fast lookup
        mandatory_set = set()
        if mandatory_points:
            for pt in mandatory_points:
                # Use rounded coordinates for comparison (to handle floating point)
                mandatory_set.add((round(pt[0], 6), round(pt[1], 6)))
        
        def is_mandatory(pt):
            return (round(pt[0], 6), round(pt[1], 6)) in mandatory_set
        
        logger.info(f"A*: Smoothing path with {len(path)} points ({len(mandatory_set)} mandatory)...")
        
        # Step 1: Remove collinear points (points that don't change direction significantly)
        # Use bearing-based detection - if bearing change < threshold, point is redundant
        # BUT: always keep mandatory points
        # IMPORTANT: Verify the shortcut doesn't cross land before removing a point
        BEARING_THRESHOLD = 2.0  # degrees - points with less than this bearing change are collinear
        
        simplified = [path[0]]
        
        for i in range(1, len(path) - 1):
            prev = simplified[-1]
            curr = path[i]
            next_pt = path[i + 1]
            
            # Always keep mandatory points
            if is_mandatory(curr):
                simplified.append(curr)
                continue
            
            # Calculate bearings
            bearing1 = geod.Inverse(prev[0], prev[1], curr[0], curr[1])['azi1']
            bearing2 = geod.Inverse(curr[0], curr[1], next_pt[0], next_pt[1])['azi1']
            
            # Normalize bearing difference to [-180, 180]
            bearing_diff = bearing2 - bearing1
            while bearing_diff > 180:
                bearing_diff -= 360
            while bearing_diff < -180:
                bearing_diff += 360
            
            # Keep point if there's a significant direction change
            if abs(bearing_diff) > BEARING_THRESHOLD:
                simplified.append(curr)
                continue
            
            # Even if collinear, check that skipping this point doesn't cross land
            # This is critical for paths that follow coastlines
            if self._edge_crosses_land(prev[0], prev[1], next_pt[0], next_pt[1]):
                # Can't skip - the direct path crosses land
                simplified.append(curr)
        
        simplified.append(path[-1])
        
        logger.info(f"A*: After collinear removal: {len(simplified)} points (removed {len(path) - len(simplified)})")
        
        # Step 2: Try to shortcut - skip intermediate waypoints where direct path is clear
        # Use a greedy approach: try to skip as many points as possible from each position
        # BUT: never skip mandatory points
        shortcut = [simplified[0]]
        i = 0
        
        while i < len(simplified) - 1:
            # Find the next mandatory point (if any) after current position
            next_mandatory_idx = None
            for k in range(i + 1, len(simplified)):
                if is_mandatory(simplified[k]):
                    next_mandatory_idx = k
                    break
            
            # Try to skip ahead as far as possible, but not past the next mandatory point
            best_skip = i + 1  # At minimum, go to next point
            max_skip = next_mandatory_idx if next_mandatory_idx else len(simplified) - 1
            
            for j in range(max_skip, i, -1):
                # Check if we can go directly from i to j without crossing land
                if not self._edge_crosses_land(
                    simplified[i][0], simplified[i][1],
                    simplified[j][0], simplified[j][1]
                ):
                    best_skip = j
                    break
            
            # Add the target point
            shortcut.append(simplified[best_skip])
            i = best_skip
        
        logger.info(f"A*: After shortcutting: {len(shortcut)} points (removed {len(simplified) - len(shortcut)})")
        
        # Step 3: Final validation - verify no segments cross land
        # If any do, fall back to the original path for safety
        land_crossings = []
        logger.info(f"A*: Final validation - checking {len(shortcut)-1} segments for land crossings")
        for i in range(len(shortcut) - 1):
            p1, p2 = shortcut[i], shortcut[i+1]
            crosses = self._edge_crosses_land(p1[0], p1[1], p2[0], p2[1])
            # Log every segment's status
            logger.info(f"A*:   Segment {i}: ({p1[0]:.2f},{p1[1]:.2f}) -> ({p2[0]:.2f},{p2[1]:.2f}) - {'CROSSES LAND!' if crosses else 'OK'}")
            if crosses:
                land_crossings.append(i)
        
        if land_crossings:
            logger.warning(f"A*: Smoothed path has {len(land_crossings)} land crossings at segments {land_crossings}!")
            logger.warning(f"A*: Falling back to original unsmoothed path for safety")
            # Log the raw path we're falling back to
            logger.info(f"A*: Raw path has {len(path)} points")
            return path
        
        logger.info(f"A*: Total smoothing: {len(path)} -> {len(shortcut)} points ({100*(len(path)-len(shortcut))/len(path):.1f}% reduction)")
        
        return shortcut
    
    def _interpolate_long_segments(self, path: list, max_segment_km: float = 500.0) -> list:
        """
        Add intermediate waypoints along long segments to ensure proper geodesic rendering.
        
        On flat map projections (Mercator, etc.), straight lines between lat/lon coordinates
        don't follow the actual geodesic (great circle) path. For long segments, this can
        make a route appear to cross land even when the actual geodesic path is over water.
        
        This function interpolates intermediate points along the geodesic for any segment
        longer than max_segment_km, ensuring the route renders correctly on maps.
        
        Args:
            path: List of (lat, lon) tuples
            max_segment_km: Maximum segment length in km before interpolation (default 500km)
            
        Returns:
            Path with intermediate waypoints added for long segments
        """
        if len(path) <= 1:
            return path
        
        interpolated = []
        
        for i in range(len(path) - 1):
            lat1, lon1 = path[i]
            lat2, lon2 = path[i + 1]
            
            # Always add the start point of this segment
            interpolated.append((lat1, lon1))
            
            # Calculate segment distance
            line = geod.InverseLine(lat1, lon1, lat2, lon2)
            dist_km = line.s13 / 1000.0
            
            # If segment is long, add intermediate points along geodesic
            if dist_km > max_segment_km:
                n_points = int(dist_km / max_segment_km)
                for j in range(1, n_points + 1):
                    # Position along the geodesic
                    s = (line.s13 * j) / (n_points + 1)
                    g = line.Position(s)
                    # Round to 2 decimal places for cleaner output
                    interpolated.append((round(g['lat2'], 2), round(g['lon2'], 2)))
        
        # Add the final point
        interpolated.append(path[-1])
        
        if len(interpolated) > len(path):
            added = len(interpolated) - len(path)
            logger.info(f"A*: Interpolated {added} intermediate waypoints for map rendering")
            print(f"[A*] Added {added} intermediate waypoints for geodesic rendering", flush=True)
        
        return interpolated
    
    def _find_nearest_node(self, lat: float, lon: float) -> Tuple[float, float]:
        """Find the nearest graph node to a position."""
        if self.graph is None:
            raise ValueError("Graph not loaded")
        
        # Find nearest grid cell
        lat_idx = np.argmin(np.abs(self.lat_grid - lat))
        lon_idx = np.argmin(np.abs(self.lon_grid - lon))
        
        node = (self.lat_grid[lat_idx], self.lon_grid[lon_idx])
        
        # Check if this node is in the graph
        if node in self.graph:
            return node
        
        # If not, search nearby nodes
        for radius in range(1, 20):
            for di in range(-radius, radius + 1):
                for dj in range(-radius, radius + 1):
                    if abs(di) != radius and abs(dj) != radius:
                        continue
                    ni = lat_idx + di
                    nj = lon_idx + dj
                    if 0 <= ni < len(self.lat_grid) and 0 <= nj < len(self.lon_grid):
                        candidate = (self.lat_grid[ni], self.lon_grid[nj])
                        if candidate in self.graph:
                            return candidate
        
        raise ValueError(f"No graph node found near ({lat}, {lon})")
    
    def _compute_corridor_bounds(self, start: Tuple[float, float], end: Tuple[float, float], 
                                  waypoints: list = None) -> Tuple[float, float, float, float]:
        """
        Compute a bounding box corridor around the route for search pruning.
        
        The corridor is sized based on route distance with a minimum width,
        allowing detours for weather avoidance while preventing global exploration.
        
        Args:
            start: (lat, lon) start point
            end: (lat, lon) end point
            waypoints: Optional intermediate waypoints
            
        Returns:
            (lat_min, lat_max, lon_min, lon_max) corridor bounds
        """
        # Collect all points
        all_points = [start]
        if waypoints:
            all_points.extend(waypoints)
        all_points.append(end)
        
        # Calculate total route distance (great circle through waypoints)
        total_distance = 0.0
        for i in range(len(all_points) - 1):
            dist = geod.Inverse(all_points[i][0], all_points[i][1], 
                               all_points[i+1][0], all_points[i+1][1])['s12']
            total_distance += dist
        
        # Corridor width: max of fraction of distance or minimum
        corridor_m = max(
            total_distance * self.corridor_fraction,
            self.corridor_min_km * 1000  # Convert km to m
        )
        
        # Convert to degrees (approximate, 1 degree ≈ 111km at equator)
        # Use latitude-aware conversion for longitude
        mean_lat = sum(p[0] for p in all_points) / len(all_points)
        corridor_lat_deg = corridor_m / 111000
        corridor_lon_deg = corridor_m / (111000 * np.cos(np.radians(mean_lat)))
        
        # Find bounding box of all route points
        lats = [p[0] for p in all_points]
        lons = [p[1] for p in all_points]
        
        lat_min = min(lats) - corridor_lat_deg
        lat_max = max(lats) + corridor_lat_deg
        lon_min = min(lons) - corridor_lon_deg
        lon_max = max(lons) + corridor_lon_deg
        
        logger.info(f"A* Corridor: route={total_distance/1000:.0f}km, width={corridor_m/1000:.0f}km")
        logger.info(f"A* Corridor: bounds=({lat_min:.2f},{lon_min:.2f}) to ({lat_max:.2f},{lon_max:.2f})")
        print(f"[A* Corridor] route={total_distance/1000:.0f}km, width={corridor_m/1000:.0f}km", flush=True)
        print(f"[A* Corridor] bounds=({lat_min:.2f},{lon_min:.2f}) to ({lat_max:.2f},{lon_max:.2f})", flush=True)
        
        # Clamp to valid geographic bounds
        lat_min = max(lat_min, -90.0)
        lat_max = min(lat_max, 90.0)
        lon_min = max(lon_min, -180.0)
        lon_max = min(lon_max, 180.0)
        
        return lat_min, lat_max, lon_min, lon_max
    
    def _create_corridor_subgraph(self, lat_min: float, lat_max: float, 
                                   lon_min: float, lon_max: float) -> nx.DiGraph:
        """
        Create a subgraph containing only nodes within the corridor bounds.
        
        This is much faster than filtering during A* search because:
        1. Node filtering is O(N) upfront vs O(N) repeated checks during search
        2. Edge count is drastically reduced
        3. NetworkX A* only considers edges in the subgraph
        
        Args:
            lat_min, lat_max, lon_min, lon_max: Corridor bounds
            
        Returns:
            DiGraph subgraph containing only corridor nodes
        """
        # Find nodes within corridor
        corridor_nodes = [
            node for node in self.graph.nodes()
            if lat_min <= node[0] <= lat_max and lon_min <= node[1] <= lon_max
        ]
        
        if len(corridor_nodes) == 0:
            logger.warning("A* Corridor: No nodes in corridor bounds!")
            return self.graph  # Fall back to full graph
        
        # Create subgraph (preserves edges between selected nodes)
        subgraph = self.graph.subgraph(corridor_nodes).copy()
        
        logger.info(f"A* Corridor: {len(corridor_nodes)} nodes ({100*len(corridor_nodes)/self.graph.number_of_nodes():.1f}% of full graph)")
        print(f"[A* Corridor] {len(corridor_nodes)} nodes ({100*len(corridor_nodes)/self.graph.number_of_nodes():.1f}% of full graph), {subgraph.number_of_edges()} edges", flush=True)
        logger.info(f"A* Corridor: {subgraph.number_of_edges()} edges ({100*subgraph.number_of_edges()/self.graph.number_of_edges():.1f}% of full graph)")
        
        return subgraph

    def _heuristic(self, node: Tuple[float, float], goal: Tuple[float, float]) -> float:
        """
        A* heuristic: great-circle distance assuming best-case speed.
        Must never overestimate actual cost for A* to be optimal.
        """
        distance = geod.Inverse(node[0], node[1], goal[0], goal[1])['s12']
        
        # Get best-case speed (boat max speed + max tailwind benefit)
        if self.boat is not None:
            boat_speed = self.boat.get_boat_speed()
            if hasattr(boat_speed, 'value'):
                max_speed = float(boat_speed.value) * 1.2  # 20% tailwind bonus max
            else:
                max_speed = float(boat_speed) * 1.2
        else:
            max_speed = 10.0  # Default ~20 knots
        
        # Return minimum possible time (never overestimates)
        return distance / max_speed
    
    def _compute_edge_weight(self, u: Tuple[float, float], v: Tuple[float, float], 
                             edge_data: dict, current_time: datetime) -> float:
        """
        Compute the cost of traversing an edge, considering weather.
        
        Returns travel time in seconds (or fuel in kg, depending on optimization target).
        """
        distance = edge_data['distance']
        
        # Get base boat speed
        if self.boat is not None:
            boat_speed = self.boat.get_boat_speed()
            if hasattr(boat_speed, 'value'):
                base_speed = float(boat_speed.value)
            else:
                base_speed = float(boat_speed)
        else:
            base_speed = 7.2  # ~14 knots default
        
        if not self.use_weather or self.weather is None:
            # No weather - simple distance/speed
            return distance / base_speed
        
        # Weather-aware cost calculation
        try:
            # Get edge midpoint for weather lookup
            mid_lat = (u[0] + v[0]) / 2
            mid_lon = (u[1] + v[1]) / 2
            
            # Get heading from u to v
            inv = geod.Inverse(u[0], u[1], v[0], v[1])
            heading = inv['azi1']  # Forward azimuth
            if heading < 0:
                heading += 360
            
            # Convert heading to radians for vector math
            heading_rad = np.radians(heading)
            
            # Get wind at midpoint
            # Note: This is simplified - full implementation would interpolate
            wind_u, wind_v = self._get_wind(mid_lat, mid_lon, current_time)
            wind_speed = np.sqrt(wind_u**2 + wind_v**2)
            wind_dir = np.degrees(np.arctan2(wind_u, wind_v)) % 360
            
            # Calculate wind effect on speed
            # True wind angle relative to heading
            twa = (wind_dir - heading + 180) % 360 - 180  # -180 to +180
            
            # Simple wind effect model:
            # - Headwind (twa near 0): reduces speed
            # - Tailwind (twa near 180/-180): increases speed
            wind_factor = 1.0 + 0.15 * np.cos(np.radians(twa + 180)) * min(wind_speed / 15.0, 1.0)
            effective_speed = base_speed * wind_factor
            
            # Get current at midpoint and factor into effective speed
            current_uo, current_vo = self._get_current(mid_lat, mid_lon, current_time)
            current_speed_ms = np.sqrt(current_uo**2 + current_vo**2)
            
            if current_speed_ms > 0.01:  # Only if significant current
                # Calculate component of current along heading
                # Heading unit vector: (sin(heading), cos(heading)) in (E, N) coords
                heading_east = np.sin(heading_rad)
                heading_north = np.cos(heading_rad)
                
                # Current vector: (uo, vo) = (eastward, northward) in m/s
                # Dot product gives current component along heading
                current_along_heading = current_uo * heading_east + current_vo * heading_north
                
                # Convert to m/s and adjust effective speed
                # Positive = current helping, Negative = current opposing
                effective_speed += current_along_heading
            
            # Ensure minimum speed
            effective_speed = max(effective_speed, base_speed * 0.5)
            
            base_cost = distance / effective_speed
            
            # Weather avoidance: penalize edges leading to hazardous nodes
            is_hazardous, weather_data = self._is_node_hazardous(v[0], v[1], current_time)
            if is_hazardous:
                # Track hazardous node for reporting
                node_key = (round(v[0], 4), round(v[1], 4))
                if node_key not in self._hazardous_nodes:
                    self._hazardous_nodes.add(node_key)
                
                # Apply penalty to discourage this route
                return base_cost * self.weather_penalty_factor
            
            return base_cost
            
        except Exception as e:
            logger.debug(f"Weather lookup failed for edge: {e}")
            return distance / base_speed
    
    def _preload_wind_cache(self, time: datetime):
        """Pre-load wind and wave data for the entire grid at a given time."""
        if self.weather is None or not hasattr(self.weather, 'ds') or self.weather.ds is None:
            self._wind_cache = None
            return
        
        try:
            ds = self.weather.ds
            time_str = time.strftime('%Y-%m-%d %H:00:00')
            
            # Get u/v arrays for entire grid
            if 'u' in ds.data_vars and 'v' in ds.data_vars:
                u_data = ds['u'].sel(time=time_str, method='nearest')
                v_data = ds['v'].sel(time=time_str, method='nearest')
            elif 'u-component_of_wind_height_above_ground' in ds.data_vars:
                u_data = ds['u-component_of_wind_height_above_ground'].sel(
                    time=time_str, height_above_ground=10, method='nearest')
                v_data = ds['v-component_of_wind_height_above_ground'].sel(
                    time=time_str, height_above_ground=10, method='nearest')
            else:
                self._wind_cache = None
                return
            
            # Create interpolation functions for fast lookup
            lats = u_data.latitude.values
            lons = u_data.longitude.values
            
            from scipy.interpolate import RegularGridInterpolator
            self._wind_cache = {
                'u_interp': RegularGridInterpolator((lats, lons), u_data.values, method='linear', bounds_error=False, fill_value=0.0),
                'v_interp': RegularGridInterpolator((lats, lons), v_data.values, method='linear', bounds_error=False, fill_value=0.0),
                'wave_interp': None,  # Will be populated if wave data available
            }
            self._wind_cache_time = time
            logger.info(f"A*: Pre-loaded wind cache for {time_str}")
            
            # Try to cache wave height data too
            wave_vars = ['swh', 'significant_wave_height', 'VHM0', 'Hs', 'wave_height']
            for var_name in wave_vars:
                if var_name in ds.data_vars:
                    try:
                        wave_data = ds[var_name].sel(time=time_str, method='nearest')
                        wave_lats = wave_data.latitude.values
                        wave_lons = wave_data.longitude.values
                        self._wind_cache['wave_interp'] = RegularGridInterpolator(
                            (wave_lats, wave_lons), wave_data.values, 
                            method='linear', bounds_error=False, fill_value=np.nan
                        )
                        logger.info(f"A*: Pre-loaded wave cache ({var_name})")
                        break
                    except Exception as e:
                        logger.debug(f"A*: Could not cache {var_name}: {e}")
            
            # Try to cache ocean current data (uo, vo)
            if 'uo' in ds.data_vars and 'vo' in ds.data_vars:
                try:
                    uo_data = ds['uo'].sel(time=time_str, method='nearest')
                    vo_data = ds['vo'].sel(time=time_str, method='nearest')
                    
                    # Handle depth dimension if present (use surface layer)
                    if 'depth' in uo_data.dims:
                        uo_data = uo_data.isel(depth=0)
                        vo_data = vo_data.isel(depth=0)
                    
                    current_lats = uo_data.latitude.values
                    current_lons = uo_data.longitude.values
                    
                    self._wind_cache['uo_interp'] = RegularGridInterpolator(
                        (current_lats, current_lons), uo_data.values,
                        method='linear', bounds_error=False, fill_value=0.0
                    )
                    self._wind_cache['vo_interp'] = RegularGridInterpolator(
                        (current_lats, current_lons), vo_data.values,
                        method='linear', bounds_error=False, fill_value=0.0
                    )
                    logger.info(f"A*: Pre-loaded ocean current cache (uo, vo)")
                except Exception as e:
                    logger.debug(f"A*: Could not cache ocean currents: {e}")
            
        except Exception as e:
            logger.warning(f"A*: Failed to preload wind cache: {e}")
            self._wind_cache = None
    
    def _get_wind(self, lat: float, lon: float, time: datetime) -> Tuple[float, float]:
        """Get wind u,v components at a position using cached interpolators."""
        # Use cached interpolator if available
        if self._wind_cache is not None:
            try:
                u = float(self._wind_cache['u_interp']((lat, lon)))
                v = float(self._wind_cache['v_interp']((lat, lon)))
                return u, v
            except Exception:
                pass
        
        # Fallback to direct lookup (slower)
        if self.weather is None or not hasattr(self.weather, 'ds') or self.weather.ds is None:
            return 0.0, 0.0
        
        try:
            ds = self.weather.ds
            time_str = time.strftime('%Y-%m-%d %H:00:00')
            
            if 'u' in ds.data_vars and 'v' in ds.data_vars:
                u_data = ds['u'].sel(time=time_str, method='nearest')
                v_data = ds['v'].sel(time=time_str, method='nearest')
                wind_u = float(u_data.interp(latitude=lat, longitude=lon).values)
                wind_v = float(v_data.interp(latitude=lat, longitude=lon).values)
                return wind_u, wind_v
        except Exception:
            pass
        
        return 0.0, 0.0
    
    def _get_current(self, lat: float, lon: float, time: datetime) -> Tuple[float, float]:
        """Get ocean current uo,vo components at a position using cached interpolators.
        
        Returns (uo, vo) in m/s - eastward and northward current velocities.
        """
        # Use cached interpolator if available
        if self._wind_cache is not None:
            uo_interp = self._wind_cache.get('uo_interp')
            vo_interp = self._wind_cache.get('vo_interp')
            if uo_interp is not None and vo_interp is not None:
                try:
                    uo = float(uo_interp((lat, lon)))
                    vo = float(vo_interp((lat, lon)))
                    return uo, vo
                except Exception:
                    pass
        
        # Fallback to direct lookup (slower)
        if self.weather is None or not hasattr(self.weather, 'ds') or self.weather.ds is None:
            return 0.0, 0.0
        
        try:
            ds = self.weather.ds
            time_str = time.strftime('%Y-%m-%d %H:00:00')
            
            if 'uo' in ds.data_vars and 'vo' in ds.data_vars:
                uo_data = ds['uo'].sel(time=time_str, method='nearest')
                vo_data = ds['vo'].sel(time=time_str, method='nearest')
                
                # Handle depth dimension if present
                if 'depth' in uo_data.dims:
                    uo_data = uo_data.isel(depth=0)
                    vo_data = vo_data.isel(depth=0)
                
                current_uo = float(uo_data.interp(latitude=lat, longitude=lon).values)
                current_vo = float(vo_data.interp(latitude=lat, longitude=lon).values)
                return current_uo, current_vo
        except Exception:
            pass
        
        return 0.0, 0.0
    
    def _get_weather_at_node(self, lat: float, lon: float, time: datetime) -> dict:
        """
        Get weather conditions at a node for hazard assessment.
        Uses cached interpolators for fast lookups.
        
        Returns dict with:
            wind_speed_kts: Wind speed in knots
            wave_height_m: Significant wave height in meters (if available)
            current_speed_kts: Ocean current speed in knots (if available)
        """
        result = {'wind_speed_kts': 0.0, 'wave_height_m': None, 'current_speed_kts': None}
        
        # Get wind speed (uses cached interpolator)
        wind_u, wind_v = self._get_wind(lat, lon, time)
        wind_speed_ms = np.sqrt(wind_u**2 + wind_v**2)
        result['wind_speed_kts'] = wind_speed_ms * 1.94384  # m/s to knots
        
        # Get current speed (uses cached interpolator)
        current_uo, current_vo = self._get_current(lat, lon, time)
        current_speed_ms = np.sqrt(current_uo**2 + current_vo**2)
        if current_speed_ms > 0.01:  # Only report if significant
            result['current_speed_kts'] = current_speed_ms * 1.94384  # m/s to knots
        
        # Try cached wave interpolator first (fast path)
        if self._wind_cache is not None and self._wind_cache.get('wave_interp') is not None:
            try:
                wave_height = float(self._wind_cache['wave_interp']((lat, lon)))
                if not np.isnan(wave_height):
                    result['wave_height_m'] = wave_height
                    return result
            except Exception:
                pass
        
        # Fallback: direct lookup (slow path, should be rare)
        if self.weather is not None and hasattr(self.weather, 'ds') and self.weather.ds is not None:
            ds = self.weather.ds
            time_str = time.strftime('%Y-%m-%d %H:00:00')
            
            # Try common wave height variable names
            wave_vars = ['swh', 'significant_wave_height', 'VHM0', 'Hs', 'wave_height']
            for var_name in wave_vars:
                if var_name in ds.data_vars:
                    try:
                        wave_data = ds[var_name].sel(time=time_str, method='nearest')
                        wave_height = float(wave_data.interp(latitude=lat, longitude=lon).values)
                        if not np.isnan(wave_height):
                            result['wave_height_m'] = wave_height
                            break
                    except Exception:
                        continue
        
        return result
    
    def _is_node_hazardous(self, lat: float, lon: float, time: datetime) -> Tuple[bool, dict]:
        """
        Check if a node has hazardous weather conditions.
        
        Returns:
            (is_hazardous, weather_data) - tuple of bool and the weather conditions
        """
        # If no thresholds set, nothing is hazardous
        if self.max_wave_height_m is None and self.max_wind_speed_kts is None and self.max_current_speed_kts is None:
            return False, {}
        
        weather = self._get_weather_at_node(lat, lon, time)
        is_hazardous = False
        
        # Check wind threshold
        if self.max_wind_speed_kts is not None:
            if weather['wind_speed_kts'] > self.max_wind_speed_kts:
                is_hazardous = True
        
        # Check wave threshold
        if self.max_wave_height_m is not None and weather['wave_height_m'] is not None:
            if weather['wave_height_m'] > self.max_wave_height_m:
                is_hazardous = True
        
        # Check current threshold
        if self.max_current_speed_kts is not None and weather['current_speed_kts'] is not None:
            if weather['current_speed_kts'] > self.max_current_speed_kts:
                is_hazardous = True
        
        return is_hazardous, weather
    
    def execute_routing(self, boat: Boat, wt: WeatherCond, 
                        constraints_list: ConstraintsList, verbose=False) -> Tuple[RouteParams, int]:
        """Execute A* routing with support for intermediate waypoints."""
        self.boat = boat
        self.weather = wt if self.use_weather else None
        
        # Reset hazard tracking for this routing request
        self._hazardous_nodes = set()
        self._hazards_encountered = []
        
        logger.info("=" * 60)
        logger.info("Starting A* Weather Routing")
        logger.info(f"From: {self.start} To: {self.finish}")
        logger.info(f"Weather: {'enabled' if self.weather else 'disabled (calm)'}")
        if self.max_wave_height_m or self.max_wind_speed_kts or self.max_current_speed_kts:
            thresholds = []
            if self.max_wave_height_m:
                thresholds.append(f"wave>{self.max_wave_height_m}m")
            if self.max_wind_speed_kts:
                thresholds.append(f"wind>{self.max_wind_speed_kts}kts")
            if self.max_current_speed_kts:
                thresholds.append(f"current>{self.max_current_speed_kts}kts")
            logger.info(f"Weather avoidance: {', '.join(thresholds)}")
        
        # Pre-load wind cache for fast lookups
        if self.weather is not None:
            self._preload_wind_cache(self.departure_time)
            # Debug: test wind lookup
            test_wind = self._get_wind(self.start[0], self.start[1], self.departure_time)
            logger.info(f"A*: Wind at start: u={test_wind[0]:.2f}, v={test_wind[1]:.2f} m/s")
            # Debug: test current lookup
            test_current = self._get_current(self.start[0], self.start[1], self.departure_time)
            if abs(test_current[0]) > 0.01 or abs(test_current[1]) > 0.01:
                current_speed_kts = np.sqrt(test_current[0]**2 + test_current[1]**2) * 1.94384
                logger.info(f"A*: Current at start: uo={test_current[0]:.3f}, vo={test_current[1]:.3f} m/s ({current_speed_kts:.2f} kts)")
        
        # Get intermediate waypoints from constraints (if any)
        waypoints = []
        if constraints_list.have_positive():
            constraints_list.init_positive_lists(self.start, self.finish)
            # Extract waypoint coordinates
            lat_list = constraints_list.positive_point_dict.get('lat', [])
            lon_list = constraints_list.positive_point_dict.get('lon', [])
            # Skip first (start) and last (finish)
            for i in range(1, len(lat_list) - 1):
                waypoints.append((lat_list[i], lon_list[i]))
            if waypoints:
                logger.info(f"A*: Intermediate waypoints: {waypoints}")
        
        # Build list of all points to visit
        all_points = [self.start] + waypoints + [self.finish]
        logger.info(f"A*: Routing through {len(all_points)} points ({len(waypoints)} intermediate waypoints)")
        logger.info("=" * 60)
        
        # Ensure graph covers the route - dynamically expand if needed
        # This allows routing to ANY location without hard-coded bounds
        graph_expanded = self._ensure_graph_covers_route(self.start, self.finish, waypoints)
        
        # Load graph if not yet loaded (needed for snapping)
        if self.graph is None:
            self._load_or_build_graph()
        
        # ============================================================
        # SNAP ALL WAYPOINTS TO WATER NODES
        # User-specified coordinates may be on land (ports, coastlines).
        # The graph only contains water nodes, so we snap each waypoint
        # to the nearest water node BEFORE routing.
        # ============================================================
        snapped_points = []
        for i, pt in enumerate(all_points):
            try:
                snapped = self._find_nearest_node(pt[0], pt[1])
                if snapped != pt:
                    logger.info(f"A*: Snapped waypoint {i} ({pt[0]:.4f}, {pt[1]:.4f}) -> water node ({snapped[0]:.4f}, {snapped[1]:.4f})")
                    print(f"[A*] Snapped waypoint {i} ({pt[0]:.3f}, {pt[1]:.3f}) -> ({snapped[0]:.3f}, {snapped[1]:.3f})", flush=True)
                snapped_points.append(snapped)
            except ValueError as e:
                logger.error(f"A*: Could not snap waypoint {i} ({pt[0]:.4f}, {pt[1]:.4f}) to water: {e}")
                return self._build_empty_route(), 1
        
        # Update all_points to use snapped (water-only) coordinates
        all_points = snapped_points
        snapped_start = snapped_points[0]
        snapped_finish = snapped_points[-1]
        snapped_waypoints = snapped_points[1:-1] if len(snapped_points) > 2 else []
        if graph_expanded:
            logger.info("A*: Graph was expanded to cover this route")
        
        # Compute corridor bounds for the entire route (if enabled)
        # Use original user coordinates for corridor (to be inclusive)
        search_graph = self.graph
        if self.use_corridor:
            corridor_bounds = self._compute_corridor_bounds(self.start, self.finish, waypoints)
            search_graph = self._create_corridor_subgraph(*corridor_bounds)
            
            # Verify snapped start/end nodes are in corridor subgraph
            if snapped_start not in search_graph or snapped_finish not in search_graph:
                logger.warning("A* Corridor: Start/end nodes not in corridor, using full graph")
                search_graph = self.graph
        
        # Create weight function for A*
        current_time = self.departure_time
        edge_count = [0]  # Use list to allow modification in closure
        
        # Check if we're using a pre-built global graph (edges already validated for land crossings)
        # In that case, skip redundant land checks during A* for massive performance gain
        skip_land_check = _find_global_graph(self.grid_resolution) is not None
        if skip_land_check:
            logger.info("A*: Using pre-built global graph - skipping redundant land checks during search")
        
        def weight_func(u, v, edge_data):
            edge_count[0] += 1
            if edge_count[0] % 10000 == 0:
                print(f"[A*] Evaluated {edge_count[0]:,} edges...", flush=True)
            # Only check land crossing if not using pre-built graph
            # Pre-built graphs already filter out land-crossing edges during construction
            if not skip_land_check and self._edge_crosses_land(u[0], u[1], v[0], v[1]):
                return float("inf")
            return self._compute_edge_weight(u, v, edge_data, current_time)
        
        # Route through each segment using SNAPPED water coordinates
        full_path = [snapped_start]  # Start with snapped water node (not raw user coord)
        total_search_time = 0.0
        
        for seg_idx in range(len(all_points) - 1):
            seg_start = all_points[seg_idx]
            seg_end = all_points[seg_idx + 1]
            
            logger.info(f"A*: Segment {seg_idx + 1}/{len(all_points) - 1}: {seg_start} -> {seg_end}")
            
            # Find nearest nodes for this segment
            try:
                start_node = self._find_nearest_node(seg_start[0], seg_start[1])
                end_node = self._find_nearest_node(seg_end[0], seg_end[1])
                logger.info(f"A*:   Nodes: {start_node} -> {end_node}")
            except ValueError as e:
                logger.error(f"A*: Could not find nodes: {e}")
                return self._build_empty_route(), 1
            
            # Run A* for this segment using the corridor-filtered graph
            start_time = time.time()
            
            try:
                segment_path = nx.astar_path(
                    search_graph,  # Use corridor-filtered graph
                    start_node, 
                    end_node,
                    heuristic=lambda n, g: self._heuristic(n, g),
                    weight=weight_func
                )
                
                search_time = time.time() - start_time
                total_search_time += search_time
                logger.info(f"A*:   Found {len(segment_path)} nodes in {search_time:.2f}s")
                
            except nx.NetworkXNoPath:
                # If corridor too restrictive, try full graph
                if self.use_corridor and search_graph is not self.graph:
                    logger.warning(f"A*: No path in corridor, trying full graph...")
                    try:
                        segment_path = nx.astar_path(
                            self.graph,
                            start_node,
                            end_node,
                            heuristic=lambda n, g: self._heuristic(n, g),
                            weight=weight_func
                        )
                        search_time = time.time() - start_time
                        total_search_time += search_time
                        logger.info(f"A*:   Found {len(segment_path)} nodes in {search_time:.2f}s (full graph)")
                    except nx.NetworkXNoPath:
                        logger.error(f"A*: No path found for segment {seg_idx + 1}")
                        return self._build_empty_route(), 1
                else:
                    logger.error(f"A*: No path found for segment {seg_idx + 1}")
                    return self._build_empty_route(), 1
            except Exception as e:
                logger.error(f"A*: Path finding failed for segment {seg_idx + 1}: {e}")
                return self._build_empty_route(), 1
            
            # Add segment path (grid nodes)
            # Always skip first node to avoid duplicates (we already have the start of each segment)
            full_path.extend(segment_path[1:])
            
            # Note: seg_end is already a snapped water coordinate from all_points
            # No need to add it separately as it's the start of the next segment
            # (or already the last point which we handle below)
        
        # Add snapped finish point (ensure it's the last point)
        if full_path[-1] != snapped_finish:
            full_path.append(snapped_finish)
        
        logger.info(f"A*: Raw path: {len(full_path)} points in {total_search_time:.2f}s")
        
        # Smooth the path - remove redundant points and shortcut where safe
        # Pass SNAPPED waypoints as mandatory points so they're preserved
        mandatory_points = [snapped_start] + snapped_waypoints + [snapped_finish]
        full_path = self._smooth_path(full_path, mandatory_points)
        
        # Interpolate long segments for proper geodesic rendering on maps
        # This adds intermediate waypoints so the route displays correctly
        full_path = self._interpolate_long_segments(full_path, max_segment_km=500.0)
        
        # Build RouteParams
        lats = [p[0] for p in full_path]
        lons = [p[1] for p in full_path]
        
        # Calculate distances and times
        dists = [0.0]
        times = [self.departure_time]
        current_time = self.departure_time
        
        for i in range(1, len(full_path)):
            dist = geod.Inverse(lats[i-1], lons[i-1], lats[i], lons[i])['s12']
            dists.append(dist)
            
            # Estimate time for this segment
            if self.boat:
                boat_speed = self.boat.get_boat_speed()
                if hasattr(boat_speed, 'value'):
                    speed = float(boat_speed.value)
                else:
                    speed = float(boat_speed)
            else:
                speed = 7.2
            
            segment_time = dist / speed
            current_time = current_time + timedelta(seconds=segment_time)
            times.append(current_time)
        
        # Create ship params (placeholder)
        n_segments = len(full_path) - 1
        ship_params = ShipParams(
            speed=np.zeros(n_segments) * u.meter/u.second,
            fuel_rate=np.zeros(n_segments) * u.kg/u.second,
            power=np.zeros(n_segments) * u.Watt,
            rpm=np.zeros(n_segments) * 1/u.minute,
            r_calm=np.zeros(n_segments) * u.newton,
            r_wind=np.zeros(n_segments) * u.newton,
            r_waves=np.zeros(n_segments) * u.newton,
            r_shallow=np.zeros(n_segments) * u.newton,
            r_roughness=np.zeros(n_segments) * u.newton,
            wave_height=np.zeros(n_segments) * u.meter,
            wave_direction=np.zeros(n_segments) * u.radian,
            wave_period=np.zeros(n_segments) * u.second,
            u_currents=np.zeros(n_segments) * u.meter/u.second,
            v_currents=np.zeros(n_segments) * u.meter/u.second,
            u_wind_speed=np.zeros(n_segments) * u.meter/u.second,
            v_wind_speed=np.zeros(n_segments) * u.meter/u.second,
            pressure=np.zeros(n_segments) * u.kg/u.meter/u.second**2,
            air_temperature=np.zeros(n_segments) * u.deg_C,
            salinity=np.zeros(n_segments) * u.dimensionless_unscaled,
            water_temperature=np.zeros(n_segments) * u.deg_C,
            status=np.zeros(n_segments),
            message=np.array([""] * n_segments)
        )
        
        total_dist = sum(dists)
        logger.info(f"A*: Total route distance: {total_dist/1000:.1f} km")
        
        # Analyze weather hazards: which were avoided vs traversed
        self._analyze_hazards(full_path, mandatory_points)
        
        route = RouteParams(
            count=len(full_path) - 2,
            start=self.start,
            finish=self.finish,
            gcr=total_dist,
            route_type='astar_route',
            time=times[-1],
            lats_per_step=lats,
            lons_per_step=lons,
            course_per_step=[0] * len(full_path),
            dists_per_step=dists,
            starttime_per_step=[t.timestamp() if isinstance(t, datetime) else t for t in times],
            ship_params_per_step=ship_params,
        )
        
        return route, 0
    
    def _analyze_hazards(self, path: list, mandatory_points: list) -> None:
        """
        Analyze which hazardous nodes were avoided vs traversed.
        Populates self._hazards_encountered with detailed hazard info.
        """
        # If no hazards detected during routing, nothing to analyze
        if not self._hazardous_nodes:
            logger.info("A*: No weather hazards detected during routing")
            return
        
        # Convert path to set for fast lookup
        path_nodes = set((round(p[0], 4), round(p[1], 4)) for p in path)
        mandatory_set = set((round(p[0], 4), round(p[1], 4)) for p in mandatory_points)
        
        hazards_avoided = 0
        hazards_traversed = 0
        
        for node in self._hazardous_nodes:
            # Get weather data at this node
            _, weather = self._is_node_hazardous(node[0], node[1], self.departure_time)
            
            hazard_info = {
                'lat': node[0],
                'lon': node[1],
                'wind_speed_kts': weather.get('wind_speed_kts', 0),
                'wave_height_m': weather.get('wave_height_m'),
            }
            
            if node in path_nodes:
                # This hazardous node is in the path - we traversed it
                hazard_info['status'] = 'traversed'
                if node in mandatory_set:
                    hazard_info['reason'] = 'mandatory_waypoint'
                else:
                    hazard_info['reason'] = 'no_alternative'
                hazards_traversed += 1
            else:
                # This hazardous node was avoided
                hazard_info['status'] = 'avoided'
                hazards_avoided += 1
            
            self._hazards_encountered.append(hazard_info)
        
        logger.info(f"A*: Weather hazards - {hazards_avoided} avoided, {hazards_traversed} traversed")
        if hazards_traversed > 0:
            logger.warning(f"A*: Route traverses {hazards_traversed} hazardous area(s)")
    
    def get_weather_hazards(self) -> dict:
        """
        Get weather hazard report from the last routing operation.
        
        Returns dict with:
            hazards: List of hazard details
            summary: Count of avoided/traversed hazards
            thresholds: The configured hazard thresholds
        """
        traversed = [h for h in self._hazards_encountered if h.get('status') == 'traversed']
        avoided = [h for h in self._hazards_encountered if h.get('status') == 'avoided']
        
        return {
            'hazards': self._hazards_encountered,
            'summary': {
                'hazards_detected': len(self._hazards_encountered),
                'hazards_avoided': len(avoided),
                'hazards_traversed': len(traversed),
            },
            'thresholds': {
                'max_wave_height_m': self.max_wave_height_m,
                'max_wind_speed_kts': self.max_wind_speed_kts,
                'max_current_speed_kts': self.max_current_speed_kts,
            }
        }
    
    def _build_empty_route(self) -> RouteParams:
        """Build an empty route for error cases."""
        return RouteParams(
            count=0,
            start=self.start,
            finish=self.finish,
            gcr=0,
            route_type='astar_route_failed',
            time=self.departure_time,
            lats_per_step=[],
            lons_per_step=[],
            course_per_step=[],
            dists_per_step=[],
            starttime_per_step=[],
            ship_params_per_step=None,
        )
