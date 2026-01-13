"""
Greedy routing algorithm - simple, intuitive approach to weather routing.

At each step:
1. Sample all directions (every N degrees)
2. For each direction, calculate where we'd end up after burning delta_fuel
3. Check which directions cross land (eliminate them)
4. Pick the direction that gets us closest to destination
5. Repeat until we reach destination

This is simpler than isochrone-based methods and produces more intuitive routes.
"""

import logging
import numpy as np
from datetime import datetime, timedelta
from typing import Optional, List, Tuple
from dataclasses import dataclass
from geovectorslib import geod
from astropy import units as u

from WeatherRoutingTool.algorithms.routingalg import RoutingAlg
from WeatherRoutingTool.constraints.constraints import ConstraintsList, ConstraintsListFactory
from WeatherRoutingTool.routeparams import RouteParams
from WeatherRoutingTool.ship.ship import Boat
from WeatherRoutingTool.weather import WeatherCond
from WeatherRoutingTool.ship.shipparams import ShipParams

logger = logging.getLogger('WRT.Greedy')


@dataclass
class GreedyConfig:
    """Configuration for the greedy routing algorithm."""
    delta_fuel_kg: float = 1000.0  # Fuel budget per step (kg)
    heading_samples: int = 72  # Number of directions to sample (72 = every 5 degrees)
    max_iterations: int = 500  # Safety limit
    arrival_threshold_m: float = 5000.0  # Consider arrived when within this distance (meters)
    use_weather: bool = True  # Whether to use weather data for speed/fuel calculations


class GreedyRouter(RoutingAlg):
    """
    Greedy routing algorithm that always picks the direction making most progress toward destination.
    
    Simple, intuitive, and produces sensible routes. Supports:
    - Weather-optimized routing (different fuel consumption by heading)
    - Calm/no-weather routing (uniform speed in all directions)  
    - Intermediate waypoints
    - Land avoidance via constraint checking
    """
    
    def __init__(self, config):
        """Initialize the greedy router from a Config object."""
        super().__init__(config)
        
        # Extract greedy-specific config, with defaults
        self.delta_fuel_kg = getattr(config, 'GREEDY_DELTA_FUEL_KG', 1000.0)
        self.heading_samples = getattr(config, 'GREEDY_HEADING_SAMPLES', 72)
        self.max_iterations = getattr(config, 'GREEDY_MAX_ITERATIONS', 500)
        self.arrival_threshold_m = getattr(config, 'GREEDY_ARRIVAL_THRESHOLD_M', 5000.0)
        self.use_weather = getattr(config, 'GREEDY_USE_WEATHER', True)
        
        # Note: self.start, self.finish, self.departure_time are set by parent class
        
        # Get fuel rate from config - BOAT_FUEL_RATE is in kg/hr from local_server
        self.fuel_rate_kg_hr = getattr(config, 'BOAT_FUEL_RATE', 167.0)  # Default 167 kg/hr
        
        # Will be set during routing
        self.boat: Optional[Boat] = None
        self.weather: Optional[WeatherCond] = None
        self.constraints: Optional[ConstraintsList] = None
        
        # Results storage
        self.route_lats: List[float] = []
        self.route_lons: List[float] = []
        self.route_times: List[datetime] = []
        self.route_fuels: List[float] = []
        self.route_courses: List[float] = []
    
    def print_init(self):
        """Print initialization info (called by execute_routing framework)."""
        logger.info(f"GreedyRouter initialized:")
        logger.info(f"  delta_fuel_kg: {self.delta_fuel_kg}")
        logger.info(f"  heading_samples: {self.heading_samples} (every {360/self.heading_samples:.1f}°)")
        logger.info(f"  use_weather: {self.use_weather}")
        logger.info(f"  Start: {self.start}, Finish: {self.finish}")
    
    def init_fig(self, water_depth=None, map_size=None, showDepth=False):
        """Initialize figures (called by execute_routing framework). Greedy doesn't need figures."""
        pass
    
    def get_boat_speed_for_heading(self, lat: float, lon: float, heading: float, 
                                    current_time: datetime) -> Tuple[float, float]:
        """
        Get boat speed and fuel rate for a given heading at a position.
        
        Returns:
            (speed_m_s, fuel_rate_kg_s): Boat speed in m/s and fuel consumption in kg/s
        """
        # Use configured fuel rate (kg/hr) -> convert to kg/s
        fuel_rate_kg_s = self.fuel_rate_kg_hr / 3600.0
        
        if not self.use_weather or self.weather is None:
            # No weather - use constant speed from boat config
            # Note: boat.speed is an astropy Quantity with units, need to get .value
            boat_speed = self.boat.get_boat_speed()
            if hasattr(boat_speed, 'value'):
                speed_m_s = float(boat_speed.value)  # Extract numeric value from astropy
            else:
                speed_m_s = float(boat_speed)
            return speed_m_s, fuel_rate_kg_s
        
        try:
            # Get weather at this position and time
            # This is a simplified version - full implementation would interpolate weather
            wind_speed, wind_dir = self._get_wind_at_position(lat, lon, current_time)
            
            # Calculate true wind angle relative to boat heading
            twa = (wind_dir - heading + 180) % 360 - 180  # -180 to +180
            
            # Get boat performance for this wind condition
            # For now, use a simple model: headwind slows us, tailwind helps
            # Real implementation would use polar diagrams
            boat_speed = self.boat.get_boat_speed()
            if hasattr(boat_speed, 'value'):
                base_speed = float(boat_speed.value)
            else:
                base_speed = float(boat_speed)
            wind_factor = 1.0 + 0.1 * np.cos(np.radians(twa)) * min(wind_speed / 20.0, 1.0)
            speed_m_s = base_speed * wind_factor
            
            return float(speed_m_s), fuel_rate_kg_s
            
        except Exception as e:
            logger.warning(f"Error getting weather-adjusted speed: {e}, using default")
            boat_speed = self.boat.get_boat_speed()
            if hasattr(boat_speed, 'value'):
                speed_m_s = float(boat_speed.value)
            else:
                speed_m_s = float(boat_speed)
            return speed_m_s, fuel_rate_kg_s
    
    def _get_wind_at_position(self, lat: float, lon: float, time: datetime) -> Tuple[float, float]:
        """Get wind speed (m/s) and direction (degrees) at a position."""
        if self.weather is None:
            return 0.0, 0.0
        
        try:
            # Try to get wind from weather data
            # This is simplified - real implementation would interpolate
            wind_u = self.weather.get_wind_at_point(lat, lon, time, 'u')
            wind_v = self.weather.get_wind_at_point(lat, lon, time, 'v')
            
            wind_speed = np.sqrt(wind_u**2 + wind_v**2)
            wind_dir = np.degrees(np.arctan2(wind_u, wind_v)) % 360
            
            return float(wind_speed), float(wind_dir)
        except:
            return 0.0, 0.0
    
    def calculate_step(self, lat: float, lon: float, heading: float, 
                       current_time: datetime) -> Tuple[float, float, float, float]:
        """
        Calculate where we end up after burning delta_fuel in a given heading.
        
        Returns:
            (new_lat, new_lon, time_seconds, distance_m)
        """
        speed_m_s, fuel_rate_kg_s = self.get_boat_speed_for_heading(lat, lon, heading, current_time)
        
        # How long can we travel on delta_fuel?
        if fuel_rate_kg_s > 0:
            time_seconds = self.delta_fuel_kg / fuel_rate_kg_s
        else:
            time_seconds = 3600  # Default 1 hour if no fuel rate
        
        # How far do we go?
        distance_m = speed_m_s * time_seconds
        
        # Calculate new position
        result = geod.direct([lat], [lon], [heading], [distance_m])
        new_lat = float(result['lat2'][0])
        new_lon = float(result['lon2'][0])
        
        return new_lat, new_lon, time_seconds, distance_m
    
    def check_land_crossing(self, lat1: float, lon1: float, lat2: float, lon2: float) -> bool:
        """Check if a segment crosses land. Returns True if crosses land."""
        if self.constraints is None:
            return False
        
        # Use the constraints list to check
        is_constrained = self.constraints.safe_crossing(
            np.array([lat1]), np.array([lon1]),
            np.array([lat2]), np.array([lon2]),
            self.departure_time,  # Time doesn't matter for land crossing
            [False]
        )
        
        return is_constrained[0] if is_constrained else False
    
    def distance_to_point(self, lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        """Calculate distance in meters between two points."""
        result = geod.inverse([lat1], [lon1], [lat2], [lon2])
        return float(result['s12'][0])
    
    def route_to_waypoint(self, start_lat: float, start_lon: float, 
                          end_lat: float, end_lon: float,
                          start_time: datetime) -> Tuple[bool, str]:
        """
        Route from start to a single waypoint using greedy algorithm.
        
        Returns:
            (success, error_message)
        """
        current_lat = start_lat
        current_lon = start_lon
        current_time = start_time
        
        # Generate heading samples (0 to 360 degrees)
        headings = np.linspace(0, 360, self.heading_samples, endpoint=False)
        
        # Track if we're departing from land (port scenario)
        is_first_step = True
        
        # Track visited positions to detect loops (grid cells of ~1km)
        visited_cells = set()
        def pos_to_cell(lat, lon):
            # Round to ~1km cells
            return (round(lat * 100), round(lon * 100))
        
        # Track backward progress for stuck detection
        consecutive_backwards = 0
        max_consecutive_backwards = 50  # Allow some backtracking but not infinite
        
        iteration = 0
        while iteration < self.max_iterations:
            iteration += 1
            
            # Check if we've arrived
            dist_to_dest = self.distance_to_point(current_lat, current_lon, end_lat, end_lon)
            if dist_to_dest < self.arrival_threshold_m:
                logger.info(f"Arrived at waypoint after {iteration} iterations, {dist_to_dest:.0f}m from target")
                # Add final point at exact destination
                self.route_lats.append(end_lat)
                self.route_lons.append(end_lon)
                self.route_times.append(current_time)
                self.route_fuels.append(0)  # No additional fuel for final snap
                self.route_courses.append(0)
                return True, ""
            
            # Evaluate all headings
            candidates = []
            
            for heading in headings:
                # Calculate where we'd end up
                new_lat, new_lon, time_s, dist_m = self.calculate_step(
                    current_lat, current_lon, heading, current_time
                )
                
                # Check if this crosses land
                # Skip land check on first step to allow departure from ports (which are on land)
                if not is_first_step and self.check_land_crossing(current_lat, current_lon, new_lat, new_lon):
                    continue  # Skip this heading
                
                # Calculate remaining distance to destination
                remaining_dist = self.distance_to_point(new_lat, new_lon, end_lat, end_lon)
                
                # Calculate progress made (negative remaining is better)
                # We want to MINIMIZE remaining distance
                candidates.append({
                    'heading': heading,
                    'new_lat': new_lat,
                    'new_lon': new_lon,
                    'time_s': time_s,
                    'dist_m': dist_m,
                    'remaining_dist': remaining_dist,
                    'progress': dist_to_dest - remaining_dist  # Positive = good
                })
            
            is_first_step = False  # No longer the first step
            
            if not candidates:
                # All directions cross land - we're stuck
                logger.error(f"Stuck at ({current_lat:.4f}, {current_lon:.4f}) - all directions cross land!")
                return False, f"Stuck at ({current_lat:.4f}, {current_lon:.4f}) - all directions blocked by land"
            
            # Pick the best candidate (minimum remaining distance = maximum progress)
            best = min(candidates, key=lambda c: c['remaining_dist'])
            
            # Check if we're making progress
            if best['progress'] < -1000:  # Going significantly backwards
                logger.warning(f"Step {iteration}: Best option goes backwards by {-best['progress']:.0f}m")
                consecutive_backwards += 1
                if consecutive_backwards > max_consecutive_backwards:
                    logger.error(f"Stuck in backwards loop after {consecutive_backwards} steps")
                    return False, f"Stuck in backwards loop at ({current_lat:.4f}, {current_lon:.4f})"
            else:
                consecutive_backwards = 0  # Reset counter when making progress
            
            # Check for position loops
            cell = pos_to_cell(best['new_lat'], best['new_lon'])
            if cell in visited_cells:
                logger.warning(f"Step {iteration}: Revisiting position cell, may be in a loop")
            visited_cells.add(cell)
            
            # Record this step
            self.route_lats.append(current_lat)
            self.route_lons.append(current_lon)
            self.route_times.append(current_time)
            self.route_fuels.append(self.delta_fuel_kg)
            self.route_courses.append(best['heading'])
            
            # Move to new position
            current_lat = best['new_lat']
            current_lon = best['new_lon']
            current_time = current_time + timedelta(seconds=best['time_s'])
            
            if iteration % 10 == 0:
                logger.debug(f"Step {iteration}: heading={best['heading']:.0f}°, "
                           f"progress={best['progress']:.0f}m, remaining={best['remaining_dist']:.0f}m")
        
        logger.error(f"Max iterations ({self.max_iterations}) reached without arriving")
        return False, f"Max iterations reached, still {dist_to_dest:.0f}m from destination"
    
    def execute_routing(self, boat: Boat, wt: WeatherCond, 
                        constraints_list: ConstraintsList, verbose: bool = False) -> Tuple[RouteParams, int]:
        """
        Execute the greedy routing algorithm.
        
        Args:
            boat: Boat object with speed/fuel characteristics
            wt: Weather conditions (can be None for calm routing)
            constraints_list: Constraints including land crossing checks
            verbose: Enable verbose logging
            
        Returns:
            (RouteParams, error_code): The calculated route and error code (0 = success)
        """
        self.boat = boat
        self.weather = wt if self.use_weather else None
        self.constraints = constraints_list
        
        # Clear previous results
        self.route_lats = []
        self.route_lons = []
        self.route_times = []
        self.route_fuels = []
        self.route_courses = []
        
        logger.info("=" * 60)
        logger.info("Starting Greedy Routing")
        logger.info(f"From: {self.start} To: {self.finish}")
        logger.info(f"Weather: {'enabled' if self.weather else 'disabled (calm)'}")
        logger.info("=" * 60)
        
        # Get waypoints from constraints (if any)
        waypoints = []
        if constraints_list.have_positive():
            constraints_list.init_positive_lists(self.start, self.finish)
            # Extract waypoint coordinates
            lat_list = constraints_list.positive_point_dict.get('lat', [])
            lon_list = constraints_list.positive_point_dict.get('lon', [])
            # Skip first (start) and last (finish)
            for i in range(1, len(lat_list) - 1):
                waypoints.append((lat_list[i], lon_list[i]))
            logger.info(f"Waypoints: {waypoints}")
        
        # Build list of all points to visit
        all_points = [self.start] + waypoints + [self.finish]
        
        current_time = self.departure_time
        
        # Route through each segment
        for i in range(len(all_points) - 1):
            start_point = all_points[i]
            end_point = all_points[i + 1]
            
            logger.info(f"Routing segment {i+1}/{len(all_points)-1}: {start_point} -> {end_point}")
            
            success, error = self.route_to_waypoint(
                start_point[0], start_point[1],
                end_point[0], end_point[1],
                current_time
            )
            
            if not success:
                logger.error(f"Routing failed: {error}")
                # Return partial route if we have one
                if self.route_lats:
                    route = self._build_route_params()
                    return route, 1  # Error code 1
                else:
                    # Return empty route
                    return self._build_empty_route(), 1
            
            # Update time for next segment
            if self.route_times:
                current_time = self.route_times[-1]
        
        logger.info("=" * 60)
        logger.info(f"Routing complete! {len(self.route_lats)} waypoints")
        logger.info("=" * 60)
        
        route = self._build_route_params()
        return route, 0  # Success
    
    def _build_route_params(self) -> RouteParams:
        """Build a RouteParams object from the route data."""
        # Convert lists to numpy arrays
        lats = np.array(self.route_lats)
        lons = np.array(self.route_lons)
        courses = np.array(self.route_courses)
        fuels = np.array(self.route_fuels)
        
        # Calculate distances between points
        dists = np.zeros(len(lats))
        for i in range(1, len(lats)):
            dists[i] = self.distance_to_point(lats[i-1], lons[i-1], lats[i], lons[i])
        
        # Build times array
        times = np.array([t.timestamp() if isinstance(t, datetime) else t for t in self.route_times])
        
        # Calculate total distance
        total_dist = np.sum(dists)
        
        # Create RouteParams - reshape arrays to match expected format
        # RouteParams expects (steps, routes) shape, we have single route
        route = RouteParams(
            count=len(lats),
            start=self.start,
            finish=self.finish,
            gcr=total_dist,
            route_type='greedy_route',
            time=self.route_times[-1] if self.route_times else self.departure_time,
            lats_per_step=lats.reshape(-1, 1),
            lons_per_step=lons.reshape(-1, 1),
            course_per_step=courses.reshape(-1, 1),
            dists_per_step=dists.reshape(-1, 1),
            starttime_per_step=times.reshape(-1, 1),
            ship_params_per_step=None
        )
        
        return route
    
    def _build_empty_route(self) -> RouteParams:
        """Build an empty RouteParams for error cases."""
        return RouteParams(
            count=0,
            start=self.start,
            finish=self.finish,
            gcr=0,
            route_type='greedy_route_failed',
            time=self.departure_time,
            lats_per_step=np.array([]).reshape(-1, 1),
            lons_per_step=np.array([]).reshape(-1, 1),
            course_per_step=np.array([]).reshape(-1, 1),
            dists_per_step=np.array([]).reshape(-1, 1),
            starttime_per_step=np.array([]).reshape(-1, 1),
            ship_params_per_step=None
        )
