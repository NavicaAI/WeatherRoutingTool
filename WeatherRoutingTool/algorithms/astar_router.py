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
        
        # Region bounds (will be set from config or map_ext)
        self.lat_min = getattr(config, 'ASTAR_LAT_MIN', None) or self.map_ext.lat1
        self.lat_max = getattr(config, 'ASTAR_LAT_MAX', None) or self.map_ext.lat2
        self.lon_min = getattr(config, 'ASTAR_LON_MIN', None) or self.map_ext.lon1
        self.lon_max = getattr(config, 'ASTAR_LON_MAX', None) or self.map_ext.lon2
        
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
        
    def print_init(self):
        logger.info(f"A* Router initialized:")
        logger.info(f"  Region: ({self.lat_min:.2f}, {self.lon_min:.2f}) to ({self.lat_max:.2f}, {self.lon_max:.2f})")
        logger.info(f"  Resolution: {self.grid_resolution}° (~{self.grid_resolution * 111:.1f} km)")
        logger.info(f"  Weather-aware: {self.use_weather}")
    
    def init_fig(self, **kwargs):
        pass
    
    def _get_cache_path(self) -> Path:
        """Get the cache file path based on region and resolution."""
        GRAPH_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        
        # Create unique hash based on parameters
        params = f"{self.lat_min}_{self.lat_max}_{self.lon_min}_{self.lon_max}_{self.grid_resolution}_{self.nof_neighbors}"
        param_hash = hashlib.md5(params.encode()).hexdigest()[:12]
        
        return GRAPH_CACHE_DIR / f"astar_graph_{param_hash}.gpickle"
    
    def _load_or_build_graph(self) -> nx.DiGraph:
        """Load graph from cache or build it."""
        cache_path = self._get_cache_path()
        
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
    
    def _edge_crosses_land(self, lat1: float, lon1: float, lat2: float, lon2: float) -> bool:
        """Check if an edge crosses land."""
        # Try polygon detection first (more accurate)
        if self.land_polygon_detector is not None:
            try:
                result = self.land_polygon_detector.check_crossing(
                    np.array([lat1]), np.array([lon1]),
                    np.array([lat2]), np.array([lon2])
                )
                if result is not None and len(result) > 0:
                    return result[0]
            except Exception:
                pass
        
        # Fall back to point sampling
        line = geod.InverseLine(lat1, lon1, lat2, lon2)
        n = max(2, int(ceil(line.s13 / self.land_check_interval)))
        
        for i in range(n + 1):
            s = min(self.land_check_interval * i, line.s13)
            g = line.Position(s, Geodesic.STANDARD | Geodesic.LONG_UNROLL)
            if is_land(g['lat2'], g['lon2']):
                return True
        
        return False
    
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
            
            # Ensure minimum speed
            effective_speed = max(effective_speed, base_speed * 0.5)
            
            return distance / effective_speed
            
        except Exception as e:
            logger.debug(f"Weather lookup failed for edge: {e}")
            return distance / base_speed
    
    def _get_wind(self, lat: float, lon: float, time: datetime) -> Tuple[float, float]:
        """Get wind u,v components at a position."""
        if self.weather is None:
            return 0.0, 0.0
        
        try:
            # Try to get from weather data
            wind_u = float(self.weather.get_wind_at_point(lat, lon, time, 'u'))
            wind_v = float(self.weather.get_wind_at_point(lat, lon, time, 'v'))
            return wind_u, wind_v
        except:
            return 0.0, 0.0
    
    def execute_routing(self, boat: Boat, wt: WeatherCond, 
                        constraints_list: ConstraintsList, verbose=False) -> Tuple[RouteParams, int]:
        """Execute A* routing with support for intermediate waypoints."""
        self.boat = boat
        self.weather = wt if self.use_weather else None
        
        logger.info("=" * 60)
        logger.info("Starting A* Weather Routing")
        logger.info(f"From: {self.start} To: {self.finish}")
        logger.info(f"Weather: {'enabled' if self.weather else 'disabled (calm)'}")
        
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
        
        # Load or build graph
        if self.graph is None:
            self._load_or_build_graph()
        
        # Create weight function for A*
        current_time = self.departure_time
        
        def weight_func(u, v, edge_data):
            return self._compute_edge_weight(u, v, edge_data, current_time)
        
        # Route through each segment (start -> wp1 -> wp2 -> ... -> finish)
        full_path = [self.start]  # Start with actual start point
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
            
            # Run A* for this segment
            start_time = time.time()
            
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
                logger.info(f"A*:   Found {len(segment_path)} nodes in {search_time:.2f}s")
                
            except nx.NetworkXNoPath:
                logger.error(f"A*: No path found for segment {seg_idx + 1}")
                return self._build_empty_route(), 1
            except Exception as e:
                logger.error(f"A*: Path finding failed for segment {seg_idx + 1}: {e}")
                return self._build_empty_route(), 1
            
            # Add segment path (grid nodes)
            # Skip first node if not first segment to avoid duplicates
            if seg_idx == 0:
                full_path.extend(segment_path)
            else:
                full_path.extend(segment_path[1:])
            
            # Add the actual waypoint at the end of this segment (if not the final segment)
            # This ensures we hit the exact intermediate waypoint coordinates
            if seg_idx < len(all_points) - 2:
                full_path.append(seg_end)
        
        # Add actual end point
        full_path.append(self.finish)
        
        logger.info(f"A*: Total path: {len(full_path)} points in {total_search_time:.2f}s")
        
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
