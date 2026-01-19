#!/usr/bin/env python3
"""
Local Server for WeatherRoutingTool

FastAPI wrapper that directly imports and runs WRT for weather-optimized routing.

Usage:
  cd /home/insectile/Development/Navica/WeatherRoutingTool
  source .venv/bin/activate
  python local_server.py

Environment Variables:
  DATA_DIR              - Where to store weather/route data (default: /tmp/wrt_data)
  FROZEN_TIME_MODE      - 'true' to cache weather indefinitely for rapid iteration (default: true)
  FROZEN_TIME_KEY       - Cache key prefix when frozen (default: sardinia_2026_01)
  WEATHER_CACHE_TTL_SECONDS - Cache TTL when not frozen (default: 3600)
  WRT_DB_HOST/PORT/DATABASE/USERNAME/PASSWORD - PostGIS connection for land polygons

Then point your frontend to: FIFTY_TWO_NORTH_SERVICE_URL=http://localhost:8001
"""
import os
import sys
import json
import math
import hashlib
import pickle
import time
import asyncio
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple, Set
from concurrent.futures import ThreadPoolExecutor

# Add WRT to path
WRT_REPO_DIR = Path(__file__).parent.absolute()
sys.path.insert(0, str(WRT_REPO_DIR))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import uvicorn
import requests
import numpy as np
import pandas as pd
import xarray as xr

# WRT imports
from WeatherRoutingTool.execute_routing import execute_routing
from WeatherRoutingTool.config import Config

# ============================================================
# Configuration
# ============================================================
DATA_DIR = os.environ.get('DATA_DIR', '/tmp/wrt_data')
OUTPUT_DIR = os.environ.get('OUTPUT_DIR', f'{DATA_DIR}/routes')
CACHE_DIR = f"{DATA_DIR}/.cache"
WEATHER_CACHE_TTL_SECONDS = int(os.environ.get('WEATHER_CACHE_TTL_SECONDS', '3600'))

# Debug logging - set DEBUG_LOGGING=true to see full request/response JSON
DEBUG_LOGGING = os.environ.get('DEBUG_LOGGING', 'true').lower() == 'true'

# FROZEN TIME MODE: Reuse cached weather data indefinitely for rapid iteration
FROZEN_TIME_MODE = os.environ.get('FROZEN_TIME_MODE', 'true').lower() == 'true'
FROZEN_TIME_KEY = os.environ.get('FROZEN_TIME_KEY', 'sardinia_2026_01')
# Default departure time in frozen mode - must be within the cached weather data range
FROZEN_DEPARTURE_TIME = os.environ.get('FROZEN_DEPARTURE_TIME', '2026-01-15T08:00:00Z')

# PostGIS config for land polygon detection
os.environ.setdefault('WRT_DB_HOST', 'localhost')
os.environ.setdefault('WRT_DB_PORT', '5433')
os.environ.setdefault('WRT_DB_DATABASE', 'gis_db')
os.environ.setdefault('WRT_DB_USERNAME', 'gis_user')
os.environ.setdefault('WRT_DB_PASSWORD', 'postgis_password')

# Thread pool for CPU-bound operations
executor = ThreadPoolExecutor(max_workers=2)

# Lock for netCDF operations (netCDF4 library is not thread-safe)
import threading
netcdf_lock = threading.Lock()

# Ensure directories exist
Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
Path(CACHE_DIR).mkdir(parents=True, exist_ok=True)

# ============================================================
# Land Proximity Grid for Smart Resolution Selection
# ============================================================
_land_proximity_grid = None

def _load_land_proximity_grid():
    """Load the pre-computed land proximity grid for fast lookups."""
    global _land_proximity_grid
    if _land_proximity_grid is not None:
        return _land_proximity_grid
    
    # Try to load the pre-computed grid
    grid_path = Path('/tmp/wrt_graph_cache/LAND_PROXIMITY_2.00deg_150km.pkl')
    if grid_path.exists():
        try:
            with open(grid_path, 'rb') as f:
                _land_proximity_grid = pickle.load(f)
            print(f"[LandProx] Loaded land proximity grid: {grid_path.name}", flush=True)
            return _land_proximity_grid
        except Exception as e:
            print(f"[LandProx] Failed to load grid: {e}", flush=True)
    
    return None


def route_passes_near_land(route_points: list, threshold_km: float = 150.0) -> bool:
    """
    Check if a route passes within threshold_km of land.
    
    Uses pre-computed land proximity grid for fast lookups.
    Falls back to conservative "True" if grid not available.
    
    Args:
        route_points: List of (lat, lon) tuples along the route
        threshold_km: Distance threshold in km
        
    Returns:
        True if route passes near land, False if open ocean only
    """
    grid = _load_land_proximity_grid()
    
    if grid is None:
        # No grid available - be conservative, assume near land
        return True
    
    near_land = grid['near_land']
    lats = grid['lats']
    lons = grid['lons']
    resolution = grid['resolution']
    
    # Check each route point
    for lat, lon in route_points:
        # Find nearest grid cell
        lat_idx = int(round((lat - lats[0]) / resolution))
        lon_idx = int(round((lon - lons[0]) / resolution))
        
        # Clamp to valid indices
        lat_idx = max(0, min(lat_idx, len(lats) - 1))
        lon_idx = max(0, min(lon_idx, len(lons) - 1))
        
        if near_land[lat_idx, lon_idx]:
            return True
    
    return False


def is_route_in_pacific_nw(route_points: list) -> bool:
    """
    Check if all route points are within the Pacific Northwest region.
    
    Pacific NW bounds: lat 47-60, lon -140 to -122
    """
    if not route_points:
        return False
    
    PACIFIC_NW_BOUNDS = {
        "lat_min": 47.0,
        "lat_max": 60.0,
        "lon_min": -140.0,
        "lon_max": -122.0,
    }
    
    for lat, lon in route_points:
        if not (PACIFIC_NW_BOUNDS["lat_min"] <= lat <= PACIFIC_NW_BOUNDS["lat_max"] and
                PACIFIC_NW_BOUNDS["lon_min"] <= lon <= PACIFIC_NW_BOUNDS["lon_max"]):
            return False
    return True


def select_astar_resolution(route_distance_nm: float, route_points: list) -> float:
    """
    Select optimal A* grid resolution based on route distance, region, and land proximity.
    
    Strategy:
    - Pacific NW region: Use 0.02° (high-res regional graph with intricate coastlines)
    - Short routes (<200nm): Fine resolution (0.05°)
    - Medium routes (200-1000nm): Medium resolution (0.1°) 
    - Long routes (1000-5000nm): Coarse resolution (0.25°)
    - Very long routes (>5000nm):
      - If passes near land/straits: 1.0° (need connectivity through straits)
      - If open ocean only: 2.0° (can use coarse grid)
    """
    # Check for Pacific NW region - use high-res regional graph
    if route_points and is_route_in_pacific_nw(route_points):
        print(f"[Resolution] Route is in Pacific NW - using regional 0.02° graph", flush=True)
        return 0.02
    
    if route_distance_nm is None or route_distance_nm < 200:
        return 0.05
    elif route_distance_nm < 1000:
        return 0.1
    elif route_distance_nm < 5000:
        return 0.25
    else:
        # Long route - check if it passes near land
        if route_passes_near_land(route_points):
            # Near land - need finer resolution for strait connectivity
            return 1.0
        else:
            # Open ocean - can use coarse resolution
            return 2.0


def debug_log_response(response: "RouteResponse") -> "RouteResponse":
    """Log full response JSON if DEBUG_LOGGING is enabled."""
    if DEBUG_LOGGING:
        print(f"\n[DEBUG] Full response JSON:", flush=True)
        response_dict = response.model_dump()
        print(json.dumps(response_dict, indent=2, default=str), flush=True)
        print(f"{'='*60}\n", flush=True)
    return response


# ============================================================
# Weather Data Cache
# ============================================================
def get_cache_key(lat: float, lon: float, forecast_days: int) -> str:
    """Generate cache key for weather data at a point.
    
    In FROZEN_TIME_MODE (default: on), uses a fixed key WITHOUT forecast_days
    so weather data is cached indefinitely for rapid dev iteration.
    """
    if FROZEN_TIME_MODE:
        # Use frozen key WITHOUT forecast_days - always hit cache
        key = f"{lat:.2f}_{lon:.2f}_{FROZEN_TIME_KEY}"
    else:
        # Normal mode - cache includes forecast_days and invalidates daily
        today = datetime.now().strftime("%Y-%m-%d")
        key = f"{lat:.2f}_{lon:.2f}_{forecast_days}_{today}"
    return hashlib.md5(key.encode()).hexdigest()[:12]


def get_cached_response(cache_key: str) -> dict | None:
    """Get cached weather response. In FROZEN_TIME_MODE, cache never expires."""
    cache_path = Path(CACHE_DIR)
    cache_file = cache_path / f"{cache_key}.pkl"
    if cache_file.exists():
        # In frozen mode, never expire cache
        if not FROZEN_TIME_MODE:
            age = datetime.now().timestamp() - cache_file.stat().st_mtime
            if age > WEATHER_CACHE_TTL_SECONDS:
                cache_file.unlink()
                return None
        try:
            with open(cache_file, 'rb') as f:
                return pickle.load(f)
        except:
            pass
    return None


def save_to_cache(cache_key: str, data: dict):
    """Save weather data to cache."""
    cache_path = Path(CACHE_DIR)
    cache_path.mkdir(parents=True, exist_ok=True)
    cache_file = cache_path / f"{cache_key}.pkl"
    try:
        with open(cache_file, 'wb') as f:
            pickle.dump(data, f)
    except Exception as e:
        print(f"[Cache] Error saving {cache_file}: {e}", flush=True)


def cleanup_stale_cache():
    """
    Remove stale cache entries. In FROZEN_TIME_MODE, never cleanup (keep all cached data).
    In normal mode, remove entries older than 24 hours.
    """
    if FROZEN_TIME_MODE:
        print("[Cache] FROZEN_TIME_MODE is ON - skipping cleanup to preserve cached weather data", flush=True)
        return 0
        
    cache_path = Path(CACHE_DIR)
    if not cache_path.exists():
        return 0
    
    removed = 0
    
    for cache_file in cache_path.glob("*.pkl"):
        try:
            # Check file age - if modified more than 24 hours ago, it's stale
            age_seconds = datetime.now().timestamp() - cache_file.stat().st_mtime
            if age_seconds > 86400:  # 24 hours
                cache_file.unlink()
                removed += 1
        except Exception as e:
            print(f"[Cache] Error cleaning {cache_file}: {e}", flush=True)
    
    if removed > 0:
        print(f"[Cache] Cleaned up {removed} stale cache entries", flush=True)
    return removed


# ============================================================
# Corridor-Based Weather Fetching
# ============================================================
def generate_corridor_points(
    waypoints: List[Tuple[float, float]],
    corridor_width_km: float = 200.0,
    point_spacing_km: float = 100.0,
    weather_resolution: float = 1.0
) -> Set[Tuple[float, float]]:
    """
    Generate weather grid points along a route corridor.
    
    Instead of fetching weather for the entire bounding box (which wastes API calls
    on land areas and regions far from the route), this generates points:
    1. Along the geodesic path between waypoints
    2. With perpendicular offsets to create a corridor of specified width
    
    Args:
        waypoints: List of (lat, lon) tuples defining the route
        corridor_width_km: Width of corridor on each side of route (default 200km)
        point_spacing_km: Spacing between points along route (default 100km)
        weather_resolution: Resolution to snap points to (default 1.0°)
        
    Returns:
        Set of (lat, lon) tuples for weather grid points (snapped to resolution)
    """
    from geographiclib.geodesic import Geodesic
    geod = Geodesic.WGS84
    
    corridor_points = set()
    
    # Sample along each segment
    for i in range(len(waypoints) - 1):
        lat1, lon1 = waypoints[i]
        lat2, lon2 = waypoints[i + 1]
        
        # Get geodesic line
        line = geod.InverseLine(lat1, lon1, lat2, lon2)
        segment_km = line.s13 / 1000.0
        
        # Sample points along segment
        n_points = max(2, int(segment_km / point_spacing_km) + 1)
        
        for j in range(n_points):
            # Position along geodesic
            s = (line.s13 * j) / (n_points - 1) if n_points > 1 else 0
            g = line.Position(s)
            center_lat, center_lon = g['lat2'], g['lon2']
            azimuth = g['azi2']  # Forward azimuth at this point
            
            # Add center point
            snapped_lat = round(center_lat / weather_resolution) * weather_resolution
            snapped_lon = round(center_lon / weather_resolution) * weather_resolution
            corridor_points.add((snapped_lat, snapped_lon))
            
            # Add perpendicular offset points for corridor width
            # Perpendicular azimuths: azimuth + 90° and azimuth - 90°
            for offset_km in [corridor_width_km / 2, corridor_width_km]:
                for perp_dir in [90, -90]:
                    perp_az = (azimuth + perp_dir) % 360
                    offset_point = geod.Direct(center_lat, center_lon, perp_az, offset_km * 1000)
                    off_lat = round(offset_point['lat2'] / weather_resolution) * weather_resolution
                    off_lon = round(offset_point['lon2'] / weather_resolution) * weather_resolution
                    # Clamp latitude to valid range
                    off_lat = max(-90, min(90, off_lat))
                    corridor_points.add((off_lat, off_lon))
    
    return corridor_points


def fetch_marine_corridor(
    waypoints: List[Tuple[float, float]],
    corridor_width_km: float = 200.0,
    point_spacing_km: float = 100.0,
    weather_resolution: float = 1.0,
    forecast_days: int = 7,
    map_bounds: Tuple[float, float, float, float] = None  # (lat_min, lon_min, lat_max, lon_max)
) -> xr.Dataset:
    """
    Fetch weather data along a route corridor instead of a full bounding box.
    
    This is much more efficient for long routes that don't need weather data
    for the entire rectangular bounding box (which would include land and
    irrelevant ocean areas).
    
    Args:
        waypoints: List of (lat, lon) tuples defining the route
        corridor_width_km: Width of corridor on each side of route
        point_spacing_km: Spacing between sample points along route
        weather_resolution: Resolution for weather grid snapping
        forecast_days: Number of forecast days to fetch
        map_bounds: (lat_min, lon_min, lat_max, lon_max) for the full map region.
                   The returned dataset will cover this region so WRT validation passes.
        
    Returns:
        xarray Dataset with weather data for corridor points
    """
    import time as time_module
    
    # Generate corridor points
    corridor_points = generate_corridor_points(
        waypoints, corridor_width_km, point_spacing_km, weather_resolution
    )
    
    # Filter to ocean points only (skip land)
    from global_land_mask import globe
    ocean_points = [(lat, lon) for lat, lon in corridor_points 
                    if not globe.is_land(lat, lon)]
    
    print(f"[Weather] Corridor: {len(corridor_points)} grid points, {len(ocean_points)} ocean points", flush=True)
    
    if len(ocean_points) == 0:
        raise ValueError("No ocean points found along route corridor")
    
    # Calculate bounding box for the dataset structure
    lats_list = sorted(set(p[0] for p in ocean_points))
    lons_list = sorted(set(p[1] for p in ocean_points))
    
    # Create a sparse set for quick lookup
    ocean_points_set = set(ocean_points)
    
    marine_url = "https://marine-api.open-meteo.com/v1/marine"
    weather_url = "https://api.open-meteo.com/v1/forecast"
    
    hourly_vars = [
        "wave_height", "wave_direction", "wave_period",
        "wind_wave_height", "wind_wave_direction", "wind_wave_period",
        "swell_wave_height", "swell_wave_direction", "swell_wave_period"
    ]
    wind_vars = ["wind_speed_10m", "wind_direction_10m"]
    
    all_data = []
    total_points = len(ocean_points)
    current = 0
    cached_count = 0
    fetched_count = 0
    start_time = time_module.time()
    
    for lat, lon in ocean_points:
        current += 1
        cache_key = get_cache_key(lat, lon, forecast_days)
        cached = get_cached_response(cache_key)
        
        if cached:
            all_data.append(cached)
            cached_count += 1
            continue
        
        fetched_count += 1
        try:
            marine_response = requests.get(marine_url, params={
                "latitude": lat, "longitude": lon,
                "hourly": ",".join(hourly_vars),
                "forecast_days": forecast_days,
                "timezone": "UTC"
            }, timeout=30)
            
            wind_response = requests.get(weather_url, params={
                "latitude": lat, "longitude": lon,
                "hourly": ",".join(wind_vars),
                "forecast_days": forecast_days,
                "timezone": "UTC",
                "wind_speed_unit": "ms"
            }, timeout=30)
            
            marine_data = marine_response.json() if marine_response.ok else {}
            wind_data = wind_response.json() if wind_response.ok else {}
            
            point_data = {
                'lat': lat, 'lon': lon,
                'marine': marine_data,
                'wind': wind_data
            }
            all_data.append(point_data)
            save_to_cache(cache_key, point_data)
            
            # Progress update every 50 points
            if fetched_count % 50 == 0:
                elapsed = time_module.time() - start_time
                rate = fetched_count / elapsed if elapsed > 0 else 0
                remaining = (total_points - current) / rate if rate > 0 else 0
                print(f"[Weather] Fetched {current}/{total_points} ({cached_count} cached, {fetched_count} fetched, ~{remaining:.0f}s remaining)", flush=True)
                
        except Exception as e:
            print(f"[Weather] Error at ({lat}, {lon}): {e}", flush=True)
            all_data.append({'lat': lat, 'lon': lon, 'marine': {}, 'wind': {}})
    
    elapsed = time_module.time() - start_time
    print(f"[Weather] Corridor fetch complete: {cached_count} cached, {fetched_count} fetched in {elapsed:.1f}s", flush=True)
    
    # Build xarray dataset from corridor points
    # Use map_bounds if provided (for WRT validation), otherwise use corridor bounds
    if map_bounds:
        grid_lat_min, grid_lon_min, grid_lat_max, grid_lon_max = map_bounds
        # Snap to weather resolution grid
        grid_lat_min = np.floor(grid_lat_min / weather_resolution) * weather_resolution
        grid_lat_max = np.ceil(grid_lat_max / weather_resolution) * weather_resolution
        grid_lon_min = np.floor(grid_lon_min / weather_resolution) * weather_resolution
        grid_lon_max = np.ceil(grid_lon_max / weather_resolution) * weather_resolution
    else:
        grid_lat_min, grid_lat_max = min(lats_list), max(lats_list)
        grid_lon_min, grid_lon_max = min(lons_list), max(lons_list)
    
    lats = np.arange(grid_lat_min, grid_lat_max + weather_resolution, weather_resolution)
    lons = np.arange(grid_lon_min, grid_lon_max + weather_resolution, weather_resolution)
    
    # Get time axis from first valid response
    times = None
    for d in all_data:
        if 'marine' in d and 'hourly' in d['marine'] and 'time' in d['marine']['hourly']:
            times = pd.to_datetime(d['marine']['hourly']['time'])
            break
        if 'wind' in d and 'hourly' in d['wind'] and 'time' in d['wind']['hourly']:
            times = pd.to_datetime(d['wind']['hourly']['time'])
            break
    
    if times is None:
        # Fallback: create time array
        times = pd.date_range(start=pd.Timestamp.now(tz='UTC').floor('H'), 
                             periods=forecast_days * 24, freq='h')
    
    n_times = len(times)
    n_lats = len(lats)
    n_lons = len(lons)
    
    # Initialize arrays with NaN (will be filled where we have data)
    wave_height = np.full((n_times, n_lats, n_lons), np.nan)
    wave_dir = np.full((n_times, n_lats, n_lons), np.nan)
    wave_period = np.full((n_times, n_lats, n_lons), np.nan)
    wind_speed = np.full((n_times, n_lats, n_lons), np.nan)
    wind_dir = np.full((n_times, n_lats, n_lons), np.nan)
    
    # Fill in data from corridor points
    for point_data in all_data:
        lat, lon = point_data['lat'], point_data['lon']
        lat_idx = int(round((lat - lats[0]) / weather_resolution))
        lon_idx = int(round((lon - lons[0]) / weather_resolution))
        
        if 0 <= lat_idx < n_lats and 0 <= lon_idx < n_lons:
            marine = point_data.get('marine', {})
            wind = point_data.get('wind', {})
            
            if 'hourly' in marine:
                hourly = marine['hourly']
                if 'wave_height' in hourly and hourly['wave_height']:
                    data = np.array(hourly['wave_height'][:n_times], dtype=float)
                    data = np.where(np.isnan(data), 0.5, data)
                    wave_height[:len(data), lat_idx, lon_idx] = data
                if 'wave_direction' in hourly and hourly['wave_direction']:
                    data = np.array(hourly['wave_direction'][:n_times], dtype=float)
                    data = np.where(np.isnan(data), 0.0, data)
                    wave_dir[:len(data), lat_idx, lon_idx] = data
                if 'wave_period' in hourly and hourly['wave_period']:
                    data = np.array(hourly['wave_period'][:n_times], dtype=float)
                    data = np.where(np.isnan(data), 6.0, data)
                    wave_period[:len(data), lat_idx, lon_idx] = data
            
            if 'hourly' in wind:
                hourly = wind['hourly']
                if 'wind_speed_10m' in hourly and hourly['wind_speed_10m']:
                    data = np.array(hourly['wind_speed_10m'][:n_times], dtype=float)
                    data = np.where(np.isnan(data), 5.0, data)
                    wind_speed[:len(data), lat_idx, lon_idx] = data
                if 'wind_direction_10m' in hourly and hourly['wind_direction_10m']:
                    data = np.array(hourly['wind_direction_10m'][:n_times], dtype=float)
                    data = np.where(np.isnan(data), 0.0, data)
                    wind_dir[:len(data), lat_idx, lon_idx] = data
    
    # Fill NaN values with defaults (for grid cells not in corridor)
    wave_height = np.where(np.isnan(wave_height), 1.0, wave_height)
    wave_dir = np.where(np.isnan(wave_dir), 0.0, wave_dir)
    wave_period = np.where(np.isnan(wave_period), 6.0, wave_period)
    wind_speed = np.where(np.isnan(wind_speed), 5.0, wind_speed)
    wind_dir = np.where(np.isnan(wind_dir), 0.0, wind_dir)
    
    # Convert wind speed/direction to u/v components
    wind_dir_rad = np.radians(wind_dir)
    wind_u = -wind_speed * np.sin(wind_dir_rad)
    wind_v = -wind_speed * np.cos(wind_dir_rad)
    
    # Build dataset
    depths = np.array([0.0, 10.0])
    heights = np.array([10.0])
    n_depths = len(depths)
    
    wind_u_4d = np.expand_dims(wind_u, axis=1)
    wind_v_4d = np.expand_dims(wind_v, axis=1)
    
    ds = xr.Dataset({
        'thetao': (['time', 'depth', 'latitude', 'longitude'], np.full((n_times, n_depths, n_lats, n_lons), 15.0)),
        'so': (['time', 'depth', 'latitude', 'longitude'], np.full((n_times, n_depths, n_lats, n_lons), 35.0)),
        'uo': (['time', 'depth', 'latitude', 'longitude'], np.zeros((n_times, n_depths, n_lats, n_lons))),
        'vo': (['time', 'depth', 'latitude', 'longitude'], np.zeros((n_times, n_depths, n_lats, n_lons))),
        'utotal': (['time', 'depth', 'latitude', 'longitude'], np.zeros((n_times, n_depths, n_lats, n_lons))),
        'vtotal': (['time', 'depth', 'latitude', 'longitude'], np.zeros((n_times, n_depths, n_lats, n_lons))),
        'VHM0': (['time', 'latitude', 'longitude'], wave_height),
        'VMDR': (['time', 'latitude', 'longitude'], wave_dir),
        'VTPK': (['time', 'latitude', 'longitude'], wave_period),
        'Pressure_reduced_to_MSL_msl': (['time', 'latitude', 'longitude'], np.full((n_times, n_lats, n_lons), 101325.0)),
        'Temperature_surface': (['time', 'latitude', 'longitude'], np.full((n_times, n_lats, n_lons), 288.0)),
        'u-component_of_wind_height_above_ground': (['time', 'height_above_ground', 'latitude', 'longitude'], wind_u_4d),
        'v-component_of_wind_height_above_ground': (['time', 'height_above_ground', 'latitude', 'longitude'], wind_v_4d),
        'u': (['time', 'latitude', 'longitude'], wind_u),
        'v': (['time', 'latitude', 'longitude'], wind_v),
    }, coords={
        'time': times,
        'depth': depths.astype(np.float64),
        'height_above_ground': heights.astype(np.float64),
        'latitude': lats.astype(np.float64),
        'longitude': lons.astype(np.float64),
    })
    
    # Add units
    for var, unit in [('thetao', 'degrees_C'), ('so', '1e-3'), ('uo', 'm/s'), ('vo', 'm/s'),
                      ('utotal', 'm/s'), ('vtotal', 'm/s'), ('VHM0', 'm'), ('VMDR', 'degree'),
                      ('VTPK', 's'), ('Pressure_reduced_to_MSL_msl', 'Pa'), ('Temperature_surface', 'K'),
                      ('u-component_of_wind_height_above_ground', 'm/s'),
                      ('v-component_of_wind_height_above_ground', 'm/s'), ('u', 'm/s'), ('v', 'm/s')]:
        ds[var].attrs['units'] = unit
    
    return ds


# ============================================================
# OpenMeteo Weather Fetching
# ============================================================
def fetch_marine_grid(
    lat_min: float, lat_max: float,
    lon_min: float, lon_max: float,
    resolution: float = 1.0,  # Increased from 0.5 for faster fetching
    forecast_days: int = 3    # Reduced from 7 for faster fetching
) -> xr.Dataset:
    # Snap bounds to whole degrees for better cache hits across different routes
    # This ensures nearby routes use the same grid points and share cached data
    lat_min_snapped = np.floor(lat_min)
    lat_max_snapped = np.ceil(lat_max)
    lon_min_snapped = np.floor(lon_min)
    lon_max_snapped = np.ceil(lon_max)
    
    lats = np.arange(lat_min_snapped, lat_max_snapped + resolution, resolution)
    lons = np.arange(lon_min_snapped, lon_max_snapped + resolution, resolution)
    
    print(f"[Weather] Grid: {len(lats)} x {len(lons)} = {len(lats) * len(lons)} points", flush=True)
    
    marine_url = "https://marine-api.open-meteo.com/v1/marine"
    weather_url = "https://api.open-meteo.com/v1/forecast"
    
    hourly_vars = [
        "wave_height", "wave_direction", "wave_period",
        "wind_wave_height", "wind_wave_direction", "wind_wave_period",
        "swell_wave_height", "swell_wave_direction", "swell_wave_period"
    ]
    wind_vars = ["wind_speed_10m", "wind_direction_10m"]
    
    all_data = []
    total_points = len(lats) * len(lons)
    current = 0
    cached_count = 0
    fetched_count = 0
    
    for lat in lats:
        for lon in lons:
            current += 1
            cache_key = get_cache_key(lat, lon, forecast_days)
            cached = get_cached_response(cache_key)
            
            if cached:
                all_data.append(cached)
                cached_count += 1
                continue
            
            fetched_count += 1
            try:
                marine_response = requests.get(marine_url, params={
                    "latitude": lat, "longitude": lon,
                    "hourly": ",".join(hourly_vars),
                    "forecast_days": forecast_days
                }, timeout=10)
                
                weather_response = requests.get(weather_url, params={
                    "latitude": lat, "longitude": lon,
                    "hourly": ",".join(wind_vars),
                    "forecast_days": forecast_days,
                    "wind_speed_unit": "ms"  # Request m/s instead of default km/h
                }, timeout=10)
                
                point_data = {
                    "lat": lat, "lon": lon,
                    "marine": marine_response.json() if marine_response.ok else {},
                    "weather": weather_response.json() if weather_response.ok else {}
                }
                
                save_to_cache(cache_key, point_data)
                all_data.append(point_data)
                
                if fetched_count % 5 == 0:
                    print(f"[Weather] Fetching: {fetched_count} new ({cached_count} from cache) - {current}/{total_points}", flush=True)
                    
            except Exception as e:
                print(f"[Weather] Error at ({lat}, {lon}): {e}", flush=True)
                all_data.append({"lat": lat, "lon": lon, "marine": {}, "weather": {}})
    
    print(f"[Weather] Done: {fetched_count} fetched, {cached_count} from cache ({total_points} total)", flush=True)
    return convert_to_xarray(all_data, lats, lons)


def convert_to_xarray(all_data: list, lats: np.ndarray, lons: np.ndarray) -> xr.Dataset:
    """Convert OpenMeteo API responses to WRT-compatible xarray Dataset with all required variables."""
    if not all_data or not all_data[0].get('marine', {}).get('hourly'):
        print("[Weather] Creating minimal weather dataset", flush=True)
        n_times = 168
        times = [datetime.now(timezone.utc) + timedelta(hours=h) for h in range(n_times)]
        
        return xr.Dataset({
            'thetao': (['time', 'latitude', 'longitude'], np.full((n_times, len(lats), len(lons)), 15.0)),
            'uo': (['time', 'latitude', 'longitude'], np.zeros((n_times, len(lats), len(lons)))),
            'vo': (['time', 'latitude', 'longitude'], np.zeros((n_times, len(lats), len(lons)))),
            'VHM0': (['time', 'latitude', 'longitude'], np.full((n_times, len(lats), len(lons)), 1.0)),
            'VMDR': (['time', 'latitude', 'longitude'], np.zeros((n_times, len(lats), len(lons)))),
            'VTPK': (['time', 'latitude', 'longitude'], np.full((n_times, len(lats), len(lons)), 8.0)),
            'u': (['time', 'latitude', 'longitude'], np.zeros((n_times, len(lats), len(lons)))),
            'v': (['time', 'latitude', 'longitude'], np.zeros((n_times, len(lats), len(lons)))),
        }, coords={
            'time': times,
            'latitude': lats,
            'longitude': lons,
        })
    
    sample = all_data[0]
    times_str = sample['marine']['hourly'].get('time', [])
    times = np.array([np.datetime64(datetime.fromisoformat(t.replace('Z', '+00:00')).replace(tzinfo=None)) for t in times_str])
    n_times = len(times)
    n_lats = len(lats)
    n_lons = len(lons)
    
    # Depth and height dimensions (matching synthetic weather format)
    depths = np.array([0.5])  # Surface depth in meters
    heights = np.array([10.0])  # 10m height above ground for wind
    n_depths = len(depths)
    n_heights = len(heights)
    
    # Initialize all required variables with proper dimensions
    wave_height = np.full((n_times, n_lats, n_lons), 1.0)
    wave_dir = np.zeros((n_times, n_lats, n_lons))
    wave_period = np.full((n_times, n_lats, n_lons), 8.0)
    wind_u = np.full((n_times, n_heights, n_lats, n_lons), 0.001)
    wind_v = np.full((n_times, n_heights, n_lats, n_lons), 0.001)
    wind_u_legacy = np.full((n_times, n_lats, n_lons), 0.001)
    wind_v_legacy = np.full((n_times, n_lats, n_lons), 0.001)
    
    for point_data in all_data:
        lat_idx = np.argmin(np.abs(lats - point_data['lat']))
        lon_idx = np.argmin(np.abs(lons - point_data['lon']))
        
        marine = point_data.get('marine', {}).get('hourly', {})
        weather = point_data.get('weather', {}).get('hourly', {})
        
        wh = marine.get('wave_height', [])
        wd = marine.get('wave_direction', [])
        wp = marine.get('wave_period', [])
        ws = weather.get('wind_speed_10m', [])
        wind_direction = weather.get('wind_direction_10m', [])
        
        for t in range(min(n_times, len(wh) if wh else 0)):
            wave_height[t, lat_idx, lon_idx] = wh[t] if wh[t] is not None else 1.0
            wave_dir[t, lat_idx, lon_idx] = wd[t] if wd and wd[t] is not None else 0.0
            wave_period[t, lat_idx, lon_idx] = wp[t] if wp and wp[t] is not None else 8.0
        
        for t in range(min(n_times, len(ws) if ws else 0)):
            speed = ws[t] if ws[t] is not None else 0.0
            direction = wind_direction[t] if wind_direction and wind_direction[t] is not None else 0.0
            dir_rad = np.radians(direction)
            u_val = -speed * np.sin(dir_rad)
            v_val = -speed * np.cos(dir_rad)
            wind_u[t, 0, lat_idx, lon_idx] = u_val if u_val != 0 else 0.001
            wind_v[t, 0, lat_idx, lon_idx] = v_val if v_val != 0 else 0.001
            wind_u_legacy[t, lat_idx, lon_idx] = u_val if u_val != 0 else 0.001
            wind_v_legacy[t, lat_idx, lon_idx] = v_val if v_val != 0 else 0.001
    
    # Ocean currents (zero for now - OpenMeteo doesn't provide these)
    uo_vals = np.zeros((n_times, n_depths, n_lats, n_lons))
    vo_vals = np.zeros((n_times, n_depths, n_lats, n_lons))
    utotal_vals = uo_vals.copy()
    vtotal_vals = np.sqrt(uo_vals**2 + vo_vals**2)
    
    ds = xr.Dataset({
        # Ocean variables (with depth)
        'thetao': (['time', 'depth', 'latitude', 'longitude'], np.full((n_times, n_depths, n_lats, n_lons), 15.0)),
        'so': (['time', 'depth', 'latitude', 'longitude'], np.full((n_times, n_depths, n_lats, n_lons), 35.0)),
        'uo': (['time', 'depth', 'latitude', 'longitude'], uo_vals),
        'vo': (['time', 'depth', 'latitude', 'longitude'], vo_vals),
        'utotal': (['time', 'depth', 'latitude', 'longitude'], utotal_vals),
        'vtotal': (['time', 'depth', 'latitude', 'longitude'], vtotal_vals),
        
        # Wave variables (from real data)
        'VHM0': (['time', 'latitude', 'longitude'], wave_height),
        'VMDR': (['time', 'latitude', 'longitude'], wave_dir),
        'VTPK': (['time', 'latitude', 'longitude'], wave_period),
        
        # Atmospheric variables
        'Pressure_reduced_to_MSL_msl': (['time', 'latitude', 'longitude'], np.full((n_times, n_lats, n_lons), 101325.0)),
        'Temperature_surface': (['time', 'latitude', 'longitude'], np.full((n_times, n_lats, n_lons), 288.0)),
        
        # Wind variables (with height_above_ground)
        'u-component_of_wind_height_above_ground': (['time', 'height_above_ground', 'latitude', 'longitude'], wind_u),
        'v-component_of_wind_height_above_ground': (['time', 'height_above_ground', 'latitude', 'longitude'], wind_v),
        
        # Legacy wind (for compatibility)
        'u': (['time', 'latitude', 'longitude'], wind_u_legacy),
        'v': (['time', 'latitude', 'longitude'], wind_v_legacy),
    }, coords={
        'time': times,
        'depth': depths.astype(np.float64),
        'height_above_ground': heights.astype(np.float64),
        'latitude': lats.astype(np.float64),
        'longitude': lons.astype(np.float64),
    })
    
    # Add units attributes (WRT-compatible)
    ds['thetao'].attrs['units'] = 'degrees_C'
    ds['so'].attrs['units'] = '1e-3'  # WRT expects '1e-3' not 'PSU'
    ds['uo'].attrs['units'] = 'm/s'
    ds['vo'].attrs['units'] = 'm/s'
    ds['utotal'].attrs['units'] = 'm/s'
    ds['vtotal'].attrs['units'] = 'm/s'
    ds['VHM0'].attrs['units'] = 'm'
    ds['VMDR'].attrs['units'] = 'degree'  # WRT expects singular 'degree'
    ds['VTPK'].attrs['units'] = 's'
    ds['Pressure_reduced_to_MSL_msl'].attrs['units'] = 'Pa'
    ds['Temperature_surface'].attrs['units'] = 'K'
    ds['u-component_of_wind_height_above_ground'].attrs['units'] = 'm/s'
    ds['v-component_of_wind_height_above_ground'].attrs['units'] = 'm/s'
    ds['u'].attrs['units'] = 'm/s'
    ds['v'].attrs['units'] = 'm/s'
    
    return ds


def create_synthetic_weather(
    lat_min: float, lat_max: float,
    lon_min: float, lon_max: float,
    resolution: float = 1.0,
    time_hours: int = 168,  # Default 7 days
    output_path: str = None,
    departure_time: str = None  # ISO datetime string for base time
) -> xr.Dataset:
    """Create synthetic weather data for fast local development."""
    lats = np.arange(lat_min, lat_max + resolution, resolution)
    lons = np.arange(lon_min, lon_max + resolution, resolution)
    n_lats, n_lons = len(lats), len(lons)
    n_times = time_hours  # Use provided time range
    
    # Use numpy datetime64 for netCDF compatibility
    # Base time priority: departure_time > FROZEN_DEPARTURE_TIME > now()
    if departure_time:
        dep_dt = datetime.fromisoformat(departure_time.replace('Z', '+00:00')).replace(tzinfo=None)
        base_time = np.datetime64(dep_dt)
    elif FROZEN_TIME_MODE:
        frozen_dt = datetime.fromisoformat(FROZEN_DEPARTURE_TIME.replace('Z', '+00:00')).replace(tzinfo=None)
        base_time = np.datetime64(frozen_dt)
    else:
        base_time = np.datetime64(datetime.now(timezone.utc).replace(tzinfo=None))
    times = np.array([base_time + np.timedelta64(h, 'h') for h in range(n_times)])
    
    # Calm conditions - minimal wind and waves
    # Ocean currents need depth dimension (using surface depth only for simplicity)
    depths = np.array([0.5])  # Surface depth in meters
    heights = np.array([10.0])  # 10m height above ground for wind
    n_depths = len(depths)
    n_heights = len(heights)
    
    # Calculate current components (u=east-west, v=north-south, total=magnitude)
    uo_vals = np.zeros((n_times, n_depths, n_lats, n_lons))
    vo_vals = np.zeros((n_times, n_depths, n_lats, n_lons))
    utotal_vals = uo_vals.copy()  # u component of total current
    vtotal_vals = np.sqrt(uo_vals**2 + vo_vals**2)  # Total current speed
    
    # All variables required by ship.py evaluate_weather():
    ds = xr.Dataset({
        # Ocean variables (with depth)
        'thetao': (['time', 'depth', 'latitude', 'longitude'], np.full((n_times, n_depths, n_lats, n_lons), 15.0)),  # Water temp
        'so': (['time', 'depth', 'latitude', 'longitude'], np.full((n_times, n_depths, n_lats, n_lons), 35.0)),  # Salinity
        'uo': (['time', 'depth', 'latitude', 'longitude'], uo_vals),
        'vo': (['time', 'depth', 'latitude', 'longitude'], vo_vals),
        'utotal': (['time', 'depth', 'latitude', 'longitude'], utotal_vals),
        'vtotal': (['time', 'depth', 'latitude', 'longitude'], vtotal_vals),
        
        # Wave variables (no depth) - CALM CONDITIONS
        'VHM0': (['time', 'latitude', 'longitude'], np.full((n_times, n_lats, n_lons), 0.1)),  # Wave height: 0.1m (very calm)
        'VMDR': (['time', 'latitude', 'longitude'], np.zeros((n_times, n_lats, n_lons))),  # Wave direction: 0
        'VTPK': (['time', 'latitude', 'longitude'], np.full((n_times, n_lats, n_lons), 6.0)),  # Wave period: 6s
        
        # Atmospheric variables (surface)
        'Pressure_reduced_to_MSL_msl': (['time', 'latitude', 'longitude'], np.full((n_times, n_lats, n_lons), 101325.0)),  # Pressure in Pa (standard)
        'Temperature_surface': (['time', 'latitude', 'longitude'], np.full((n_times, n_lats, n_lons), 288.0)),  # Air temp: 15C
        
        # Wind variables (with height_above_ground) - MINIMAL WIND (non-zero to avoid div/0)
        'u-component_of_wind_height_above_ground': (['time', 'height_above_ground', 'latitude', 'longitude'], 
                                                     np.full((n_times, n_heights, n_lats, n_lons), 0.001)),  # U wind: 0.001 m/s
        'v-component_of_wind_height_above_ground': (['time', 'height_above_ground', 'latitude', 'longitude'], 
                                                     np.full((n_times, n_heights, n_lats, n_lons), 0.001)),  # V wind: 0.001 m/s
        
        # Legacy wind (kept for compatibility) - MINIMAL WIND
        'u': (['time', 'latitude', 'longitude'], np.full((n_times, n_lats, n_lons), 0.001)),
        'v': (['time', 'latitude', 'longitude'], np.full((n_times, n_lats, n_lons), 0.001)),
    }, coords={
        'time': times,
        'depth': depths.astype(np.float64),
        'height_above_ground': heights.astype(np.float64),
        'latitude': lats.astype(np.float64),
        'longitude': lons.astype(np.float64),
    })
    
    ds.attrs['title'] = 'Synthetic weather data for fast local dev'
    ds.attrs['source'] = 'NavicaAI local dev server'
    
    # Add units attributes to avoid WRT warnings
    ds['thetao'].attrs['units'] = 'degrees_C'
    ds['thetao'].attrs['long_name'] = 'Sea Water Temperature'
    ds['so'].attrs['units'] = '1e-3'  # WRT expects '1e-3' not 'PSU'
    ds['so'].attrs['long_name'] = 'Sea Water Salinity'
    ds['uo'].attrs['units'] = 'm/s'
    ds['uo'].attrs['long_name'] = 'Eastward Sea Water Velocity'
    ds['vo'].attrs['units'] = 'm/s'
    ds['vo'].attrs['long_name'] = 'Northward Sea Water Velocity'
    ds['utotal'].attrs['units'] = 'm/s'
    ds['utotal'].attrs['long_name'] = 'Total Eastward Current Velocity'
    ds['vtotal'].attrs['units'] = 'm/s'
    ds['vtotal'].attrs['long_name'] = 'Total Northward Current Velocity'
    ds['VHM0'].attrs['units'] = 'm'
    ds['VHM0'].attrs['long_name'] = 'Significant Wave Height'
    ds['VMDR'].attrs['units'] = 'degree'  # WRT expects singular 'degree'
    ds['VMDR'].attrs['long_name'] = 'Mean Wave Direction'
    ds['VTPK'].attrs['units'] = 's'
    ds['VTPK'].attrs['long_name'] = 'Peak Wave Period'
    ds['Pressure_reduced_to_MSL_msl'].attrs['units'] = 'Pa'
    ds['Pressure_reduced_to_MSL_msl'].attrs['long_name'] = 'Mean Sea Level Pressure'
    ds['Temperature_surface'].attrs['units'] = 'K'
    ds['Temperature_surface'].attrs['long_name'] = 'Surface Air Temperature'
    ds['u-component_of_wind_height_above_ground'].attrs['units'] = 'm/s'
    ds['u-component_of_wind_height_above_ground'].attrs['long_name'] = 'U-Component of Wind'
    ds['v-component_of_wind_height_above_ground'].attrs['units'] = 'm/s'
    ds['v-component_of_wind_height_above_ground'].attrs['long_name'] = 'V-Component of Wind'
    ds['u'].attrs['units'] = 'm/s'
    ds['u'].attrs['long_name'] = 'U-Component of Wind (legacy)'
    ds['v'].attrs['units'] = 'm/s'
    ds['v'].attrs['long_name'] = 'V-Component of Wind (legacy)'
    
    if output_path:
        ds.to_netcdf(output_path)
        print(f"[Weather] Created synthetic: {output_path} ({n_lats}x{n_lons} grid)", flush=True)
    
    return ds


def create_synthetic_depth(
    lat_min: float, lat_max: float,
    lon_min: float, lon_max: float,
    resolution: float = 0.25,
    output_path: str = None
) -> str:
    lats = np.arange(lat_min, lat_max + resolution, resolution)
    lons = np.arange(lon_min, lon_max + resolution, resolution)
    n_lats, n_lons = len(lats), len(lons)
    
    if output_path is None:
        output_path = f"{DATA_DIR}/synthetic_depth.nc"
    
    # Use 'z' variable name as expected by WRT constraints (negative = below sea level)
    depth = np.random.uniform(-5000, -3000, (n_lats, n_lons))
    
    ds = xr.Dataset(
        {'z': (['latitude', 'longitude'], depth.astype(np.float64))},  # Must be 'z' for WRT
        coords={'latitude': lats.astype(np.float64), 'longitude': lons.astype(np.float64)}
    )
    
    ds.attrs['title'] = 'Synthetic depth data for WRT routing'
    ds.attrs['source'] = 'NavicaAI local dev server'
    ds.attrs['created'] = datetime.now(timezone.utc).isoformat()
    
    with netcdf_lock:
        ds.to_netcdf(output_path)
    print(f"[Depth] Created: {output_path} ({n_lats}x{n_lons} grid)", flush=True)
    return output_path


# ============================================================
# Request/Response Models
# ============================================================
class RoutePoint(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)
    name: Optional[str] = None

class VesselConfig(BaseModel):
    speed_knots: float = Field(default=12.0)
    length: float = Field(default=180)
    breadth: float = Field(default=32)
    draught: float = Field(default=10)
    # Fuel/power parameters (used for fuel consumption calculations)
    fuel_rate_kg_per_hour: Optional[float] = Field(default=None, description="Fuel consumption rate in kg/hour. Default: 167")
    hotel_load_kw: Optional[float] = Field(default=None, description="Auxiliary/hotel power load in kW. Default: 30")
    smcr_power_kw: Optional[float] = Field(default=None, description="SMCR (max continuous) engine power in kW. Default: 6502")

class RouteRequest(BaseModel):
    start: RoutePoint
    end: RoutePoint
    waypoints: Optional[List[RoutePoint]] = []
    departure_time: Optional[str] = None
    vessel: Optional[VesselConfig] = None
    weather_optimization: bool = True  # If True, runs two passes: calm baseline + real weather optimized
    algorithm: Optional[str] = None  # Options: "astar" (default), "isofuel", "genetic", "gcrslider", "greedy", "dijkstra"
    weather_source: Optional[str] = None  # Deprecated - now controlled by weather_optimization flag
    time_forecast_hours: Optional[int] = Field(default=None, description="Forecast window in hours. If not provided, uses 168 for calm weather or 144 for real weather. Tip: use calm route time estimate to size this.")

class CalculatedRoute(BaseModel):
    waypoints: List[List[float]]
    total_distance_nm: float
    total_time_hours: float

class WeatherHazardSummary(BaseModel):
    """Summary of weather hazards encountered during routing."""
    hazards_detected: int = 0
    hazards_avoided: int = 0
    hazards_traversed: int = 0

class WeatherHazard(BaseModel):
    """Individual weather hazard location."""
    lat: float
    lon: float
    wind_speed_kts: float
    wave_height_m: Optional[float] = None
    status: str  # "avoided" or "traversed"
    reason: Optional[str] = None  # "mandatory_waypoint" or "no_alternative"

class WeatherHazardReport(BaseModel):
    """Full weather hazard report from routing."""
    hazards: List[WeatherHazard] = []
    summary: WeatherHazardSummary = WeatherHazardSummary()
    thresholds: dict = {}

class RouteResponse(BaseModel):
    success: bool
    error: Optional[str] = None
    route: Optional[CalculatedRoute] = None
    optimized_route: Optional[CalculatedRoute] = None
    provider: str = "52north-local"
    execution_time_ms: Optional[float] = None
    weather_cached: bool = False
    algorithm_used: Optional[str] = None
    weather_source_used: Optional[str] = None
    weather_hazards: Optional[WeatherHazardReport] = None

class HealthResponse(BaseModel):
    status: str
    service: str
    version: str
    wrt_available: bool
    cache_entries: int


# ============================================================
# Utility Functions
# ============================================================
def calculate_distance_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat1_rad, lon1_rad = math.radians(lat1), math.radians(lon1)
    lat2_rad, lon2_rad = math.radians(lat2), math.radians(lon2)
    
    dlat = lat2_rad - lat1_rad
    dlon = lon2_rad - lon1_rad
    
    a = math.sin(dlat/2)**2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon/2)**2
    c = 2 * math.asin(math.sqrt(a))
    
    return 6371 * 0.539957 * c


def calculate_route_distance(coords: List[List[float]]) -> float:
    if len(coords) < 2:
        return 0.0
    total = 0.0
    for i in range(len(coords) - 1):
        total += calculate_distance_nm(coords[i][0], coords[i][1], coords[i+1][0], coords[i+1][1])
    return total


def format_departure_time(iso_string: Optional[str]) -> str:
    """Format departure time. In FROZEN_TIME_MODE, defaults to FROZEN_DEPARTURE_TIME."""
    if not iso_string:
        if FROZEN_TIME_MODE:
            # Use fixed departure time in frozen mode to match cached weather data
            dt = datetime.fromisoformat(FROZEN_DEPARTURE_TIME.replace('Z', '+00:00'))
        else:
            dt = datetime.now(timezone.utc) + timedelta(hours=1)
    else:
        try:
            dt = datetime.fromisoformat(iso_string.replace('Z', '+00:00'))
        except:
            if FROZEN_TIME_MODE:
                dt = datetime.fromisoformat(FROZEN_DEPARTURE_TIME.replace('Z', '+00:00'))
            else:
                dt = datetime.now(timezone.utc) + timedelta(hours=1)
    return dt.strftime('%Y-%m-%dT%H:%MZ')


# ============================================================
# FastAPI App
# ============================================================
app = FastAPI(
    title="52North Weather Routing - Local Dev",
    description="Local development server for rapid WRT iteration",
    version="1.0.0-local"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def validate_route_land_crossings(coords: list, map_size_tuple: tuple) -> dict:
    """
    Validate that a route doesn't cross land using PostGIS polygon data.
    
    Args:
        coords: List of [lat, lon] coordinates
        map_size_tuple: (lat_min, lon_min, lat_max, lon_max) for the route area
    
    Returns:
        {"valid": True} or {"valid": False, "crossings": [...list of crossing segment indices...]}
    """
    try:
        from WeatherRoutingTool.utils.maps import Map
        from WeatherRoutingTool.constraints.constraints import LandPolygonsCrossing
        from sqlalchemy import create_engine
        import numpy as np
        
        # Create database connection
        host = os.environ.get('WRT_DB_HOST', 'localhost')
        port = os.environ.get('WRT_DB_PORT', '5433')
        database = os.environ.get('WRT_DB_DATABASE', 'gis_db')
        username = os.environ.get('WRT_DB_USERNAME', 'gis_user')
        password = os.environ.get('WRT_DB_PASSWORD', 'postgis_password')
        
        connection_string = f"postgresql://{username}:{password}@{host}:{port}/{database}"
        engine = create_engine(connection_string)
        
        # Create map and constraint checker
        lat_min, lon_min, lat_max, lon_max = map_size_tuple
        map_size = Map(lat1=lat_min, lon1=lon_min, lat2=lat_max, lon2=lon_max)
        checker = LandPolygonsCrossing(map_size=map_size, db_engine=engine)
        
        if not checker.initialization_successful:
            print("[Route Validation] Warning: Land polygon checker not available, skipping validation", flush=True)
            return {"valid": True, "warning": "validation_unavailable"}
        
        # Check each segment
        crossings = []
        for i in range(len(coords) - 1):
            lat1, lon1 = coords[i]
            lat2, lon2 = coords[i + 1]
            
            result = checker.check_crossing(
                np.array([lat1]), np.array([lon1]),
                np.array([lat2]), np.array([lon2])
            )
            
            if result and result[0]:
                crossings.append({
                    "segment": i,
                    "from": [lat1, lon1],
                    "to": [lat2, lon2]
                })
        
        if crossings:
            print(f"[Route Validation] WARNING: Route crosses land at {len(crossings)} segments: {[c['segment'] for c in crossings]}", flush=True)
            return {"valid": False, "crossings": crossings}
        
        return {"valid": True}
        
    except Exception as e:
        print(f"[Route Validation] Error during validation: {e}", flush=True)
        return {"valid": True, "error": str(e)}


def run_wrt_direct(config_dict: dict, output_dir: str) -> dict:
    """Run WRT directly using Python imports instead of subprocess.
    
    For A* algorithm with weather avoidance enabled, captures and returns weather hazard report.
    """
    import tempfile
    from WeatherRoutingTool.ship.ship_factory import ShipFactory
    from WeatherRoutingTool.weather_factory import WeatherFactory
    from WeatherRoutingTool.constraints.constraints import ConstraintsListFactory, WaterDepth
    from WeatherRoutingTool.algorithms.routingalg_factory import RoutingAlgFactory
    from WeatherRoutingTool.utils.maps import Map
    
    config_path = Path(tempfile.mktemp(suffix='.json'))
    weather_hazards = None
    
    try:
        with open(config_path, 'w') as f:
            json.dump(config_dict, f, indent=2)
        
        print(f"[WRT] Running with config: {config_path}", flush=True)
        print(f"[WRT] Algorithm: {config_dict.get('ALGORITHM_TYPE')}", flush=True)
        print(f"[WRT] Constraints: {config_dict.get('CONSTRAINTS_LIST')}", flush=True)
        
        config = Config.assign_config(config_path)
        
        # Run routing inline to capture the router object
        windfile = config.WEATHER_DATA
        depthfile = config.DEPTH_DATA
        routepath = config.ROUTE_PATH
        time_resolution = config.DELTA_TIME_FORECAST
        time_forecast = config.TIME_FORECAST
        lat1, lon1, lat2, lon2 = config.DEFAULT_MAP
        departure_time = config.DEPARTURE_TIME
        default_map = Map(lat1, lon1, lat2, lon2)
        
        # Initialize weather
        wt = WeatherFactory.get_weather(
            config._DATA_MODE_WEATHER, windfile, departure_time, 
            time_forecast, time_resolution, default_map
        )
        
        # Initialize boat
        boat = ShipFactory.get_ship(config)
        
        # Initialize constraints
        water_depth = WaterDepth(
            config._DATA_MODE_DEPTH, boat.get_required_water_depth(),
            default_map, depthfile
        )
        constraint_list = ConstraintsListFactory.get_constraints_list(
            constraints_string_list=config.CONSTRAINTS_LIST, 
            data_mode=config._DATA_MODE_DEPTH,
            min_depth=boat.get_required_water_depth(),
            map_size=default_map, depthfile=depthfile, 
            waypoints=config.INTERMEDIATE_WAYPOINTS,
            courses_path=config.COURSES_FILE
        )
        
        # Initialize and run routing algorithm
        alg = RoutingAlgFactory.get_routing_alg(config)
        alg.init_fig(water_depth=water_depth, map_size=default_map)
        
        min_fuel_route, error_code = alg.execute_routing(boat, wt, constraint_list)
        
        # Check for routing errors
        if error_code != 0:
            error_msg = f"Routing algorithm returned error code {error_code}"
            if hasattr(min_fuel_route, 'route_type') and 'failed' in str(min_fuel_route.route_type):
                error_msg = f"No valid route found (error {error_code})"
            print(f"[WRT] {error_msg}", flush=True)
            return {"success": False, "error": error_msg}
        
        min_fuel_route.write_to_geojson(routepath + '/' + str(min_fuel_route.route_type) + ".json")
        
        # Capture weather hazards report (always include if A* with avoidance enabled)
        if hasattr(alg, 'get_weather_hazards'):
            hazard_data = alg.get_weather_hazards()
            weather_hazards = hazard_data  # Always include, even if empty
            if hazard_data.get('hazards'):
                print(f"[WRT] Weather hazards: {hazard_data['summary']}", flush=True)
            else:
                print(f"[WRT] No weather hazards detected (thresholds: {hazard_data.get('thresholds', {})})", flush=True)
        
        # Find output route
        output_path = Path(output_dir)
        route_files = list(output_path.glob("*_route.json")) + list(output_path.glob("*.json"))
        
        for route_file in route_files:
            try:
                with open(route_file, 'r') as f:
                    data = json.load(f)
                
                coords = []
                if isinstance(data, dict) and data.get('type') == 'FeatureCollection':
                    for feature in data.get('features', []):
                        geom = feature.get('geometry', {})
                        if geom.get('type') == 'Point':
                            c = geom.get('coordinates', [])
                            if len(c) >= 2:
                                coords.append([c[1], c[0]])
                        elif geom.get('type') == 'LineString':
                            for c in geom.get('coordinates', []):
                                if len(c) >= 2:
                                    coords.append([c[1], c[0]])
                
                if coords:
                    print(f"[WRT] Found route with {len(coords)} points", flush=True)
                    
                    # Validate route doesn't cross land
                    # Extract map bounds from config
                    map_lat1 = config_dict.get('LAT_START', coords[0][0]) - 1
                    map_lat2 = config_dict.get('LAT_FINISH', coords[-1][0]) + 1
                    map_lon1 = config_dict.get('LON_START', coords[0][1]) - 1
                    map_lon2 = config_dict.get('LON_FINISH', coords[-1][1]) + 1
                    map_bounds = (min(map_lat1, map_lat2), min(map_lon1, map_lon2), 
                                 max(map_lat1, map_lat2), max(map_lon1, map_lon2))
                    
                    validation = validate_route_land_crossings(coords, map_bounds)
                    if not validation.get("valid", True):
                        # Route crosses land - return error with details
                        crossings = validation.get("crossings", [])
                        crossing_info = [f"Segment {c['segment']}: {c['from']} -> {c['to']}" for c in crossings[:3]]
                        return {
                            "success": False, 
                            "error": f"Route crosses land at {len(crossings)} segment(s): {'; '.join(crossing_info)}",
                            "land_crossings": crossings,
                            "coords": coords  # Include coords for debugging
                        }
                    
                    return {
                        "success": True, 
                        "coords": coords,
                        "weather_hazards": weather_hazards
                    }
            except Exception as e:
                print(f"[WRT] Error parsing {route_file}: {e}", flush=True)
        
        return {"success": False, "error": "No route output found"}
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {"success": False, "error": str(e)}
    finally:
        if config_path.exists():
            config_path.unlink()


async def run_single_routing_pass(
    request: "RouteRequest",
    weather_source: str,  # "calm" or "real"
    pass_id: str,
    vessel: "VesselConfig",
    departure: str,
    lat_min: float, lat_max: float,
    lon_min: float, lon_max: float,
    algorithm: str,
    time_forecast_hours: Optional[int] = None,
    route_distance_nm: Optional[float] = None,  # Pre-computed route distance
    route_points: Optional[list] = None  # List of (lat, lon) for smart resolution
) -> dict:
    """
    Run a single WRT routing pass with specified weather source.
    Returns {"success": True, "coords": [...], "weather_cached": bool} or {"success": False, "error": "..."}
    
    Args:
        time_forecast_hours: Override for TIME_FORECAST. If None, auto-sizes based on route_distance_nm.
        route_distance_nm: Pre-computed route distance in nautical miles. Used to auto-size time_forecast.
                          For a 5000nm route at 10kts, creates ~750 hour forecast window.
        route_points: List of (lat, lon) waypoints for smart resolution selection based on land proximity.
    """
    request_output_dir = f"{OUTPUT_DIR}/{pass_id}"
    Path(request_output_dir).mkdir(parents=True, exist_ok=True)
    
    # Determine time_forecast:
    # - If caller provided time_forecast_hours, use it
    # - Otherwise auto-size based on route distance at assumed 10 knots average
    # - Add 50% safety margin for routing overhead
    if time_forecast_hours is not None:
        time_forecast = time_forecast_hours
        # Don't log here - the route endpoint already logs auto-sizing if applicable
    elif route_distance_nm is not None and route_distance_nm > 0:
        # Auto-size based on route distance: assume 10 knots average speed
        # Add 50% margin for routing exploration, weather delays, etc.
        estimated_hours = int(route_distance_nm / 10.0 * 1.5)
        # Minimum 168 hours (7 days), round up to nearest 24 hours
        time_forecast = max(168, ((estimated_hours + 23) // 24) * 24)
        print(f"[Weather:{pass_id}] Auto-sized TIME_FORECAST: {time_forecast}h for {route_distance_nm:.0f}nm route", flush=True)
    elif weather_source == "real":
        time_forecast = 144  # 6 days to leave margin for WRT's internal validation
        print(f"[Weather:{pass_id}] Using default TIME_FORECAST: {time_forecast} hours", flush=True)
    else:
        time_forecast = 168 if algorithm in ["genetic", "isofuel"] else 96
        print(f"[Weather:{pass_id}] Using default TIME_FORECAST: {time_forecast} hours", flush=True)
    
    weather_time_hours = time_forecast + 12  # Buffer
    
    # Generate weather data
    # For A* with calm weather, we can skip weather creation entirely - A* uses pure distance cost
    weather_cached = False
    weather_path = None
    
    if weather_source == "calm" and algorithm == "astar":
        # A* with calm weather doesn't need weather data - uses pure geodesic distance
        print(f"[Weather:{pass_id}] A* calm mode - skipping weather (pure distance cost)", flush=True)
        weather_path = f"{DATA_DIR}/weather_dummy.nc"  # Placeholder path (won't be read)
        # Create a minimal 1-hour dummy file if it doesn't exist
        if not Path(weather_path).exists():
            create_synthetic_weather(0, 1, 0, 1, 1.0, 1, weather_path, departure)
        weather_cached = True
    elif weather_source == "calm":
        print(f"[Weather:{pass_id}] Using calm synthetic data (no wind, 0.1m waves)", flush=True)
        weather_path = f"{DATA_DIR}/weather_{pass_id}.nc"
        create_synthetic_weather(lat_min, lat_max, lon_min, lon_max, 1.0, weather_time_hours, weather_path, departure)
        weather_cached = True
    else:  # "real"
        print(f"[Weather:{pass_id}] Fetching real weather from OpenMeteo...", flush=True)
        try:
            # Fetch enough days to cover the forecast window
            # Note: OpenMeteo has 16-day limit, so we cap at 16 days for API
            forecast_days = min(16, max(7, (time_forecast // 24) + 1))
            
            # Use corridor-based fetching for routes with waypoints
            # This is much more efficient for long routes
            if route_points and len(route_points) >= 2:
                # Calculate route distance to determine corridor parameters
                from geographiclib.geodesic import Geodesic
                geod = Geodesic.WGS84
                total_dist_km = 0
                for i in range(len(route_points) - 1):
                    g = geod.Inverse(route_points[i][0], route_points[i][1],
                                    route_points[i+1][0], route_points[i+1][1])
                    total_dist_km += g['s12'] / 1000.0
                
                # Adaptive corridor parameters based on route length
                if total_dist_km > 5000:  # > 2700nm - long oceanic route
                    corridor_width = 300.0  # km - wider for uncertainty
                    point_spacing = 150.0   # km - coarser for efficiency
                    weather_res = 1.5       # Coarser resolution for huge distances
                elif total_dist_km > 2000:  # > 1000nm
                    corridor_width = 250.0
                    point_spacing = 100.0
                    weather_res = 1.0
                else:  # Shorter routes
                    corridor_width = 200.0
                    point_spacing = 75.0
                    weather_res = 1.0
                
                print(f"[Weather:{pass_id}] Using corridor fetch: {total_dist_km:.0f}km route, "
                      f"{corridor_width}km width, {point_spacing}km spacing, {weather_res}° res", flush=True)
                
                weather_ds = fetch_marine_corridor(
                    waypoints=route_points,
                    corridor_width_km=corridor_width,
                    point_spacing_km=point_spacing,
                    weather_resolution=weather_res,
                    forecast_days=forecast_days,
                    map_bounds=(lat_min, lon_min, lat_max, lon_max)  # Pass map bounds for WRT validation
                )
            else:
                # Fallback to full grid fetch (for routes without pre-computed waypoints)
                print(f"[Weather:{pass_id}] Using full grid fetch (no waypoints available)", flush=True)
                weather_ds = fetch_marine_grid(lat_min, lat_max, lon_min, lon_max, resolution=1.0, forecast_days=forecast_days)
            
            weather_path = f"{DATA_DIR}/weather_{pass_id}.nc"
            with netcdf_lock:
                weather_ds.to_netcdf(weather_path)
            print(f"[Weather:{pass_id}] Saved real weather to: {weather_path}", flush=True)
        except Exception as e:
            return {"success": False, "error": f"Failed to fetch real weather: {str(e)}"}
    
    # Depth data
    depth_path = f"{DATA_DIR}/depth_{pass_id}.nc"
    try:
        create_synthetic_depth(lat_min, lat_max, lon_min, lon_max, 0.5, depth_path)
    except Exception as e:
        print(f"[Depth:{pass_id}] Warning: {e}", flush=True)
        depth_path = f"{DATA_DIR}/depth_default.nc"
        create_synthetic_depth(lat_min, lat_max, lon_min, lon_max, 0.5, depth_path)
    
    # Build WRT config
    config = {
        "DEFAULT_ROUTE": [request.start.lat, request.start.lon, request.end.lat, request.end.lon],
        "DEFAULT_MAP": [lat_min, lon_min, lat_max, lon_max],
        "DEPARTURE_TIME": departure,
        "INTERMEDIATE_WAYPOINTS": [[p.lat, p.lon] for p in (request.waypoints or [])],
        
        "ALGORITHM_TYPE": algorithm,
        "BOAT_TYPE": "direct_power_method",
        "BOAT_DRAUGHT_AFT": vessel.draught,
        "BOAT_DRAUGHT_FORE": vessel.draught,
        "BOAT_BREADTH": vessel.breadth,
        "BOAT_LENGTH": vessel.length,
        "BOAT_SPEED": vessel.speed_knots * 0.5144,
        "BOAT_SMCR_SPEED": vessel.speed_knots * 0.5144,
        "BOAT_FUEL_RATE": vessel.fuel_rate_kg_per_hour or 167,  # Default: 167 kg/hour
        "BOAT_HBR": vessel.hotel_load_kw or 30,  # Default: 30 kW hotel load
        "BOAT_SMCR_POWER": vessel.smcr_power_kw or 6502,  # Default: 6502 kW SMCR
        
        "CONSTRAINTS_LIST": ["land_crossing_global_land_mask", "land_crossing_polygons", "on_map"] + 
                           (["via_waypoints"] if request.waypoints else []),
        "TIME_FORECAST": time_forecast,
        "ROUTING_STEPS": 40,
        "DELTA_TIME_FORECAST": 3,
        "DELTA_FUEL": 3000,
        "ROUTER_HDGS_SEGMENTS": 30,
        "ROUTER_HDGS_INCREMENTS_DEG": 6,
        "ISOCHRONE_PRUNE_SECTOR_DEG_HALF": 91,
        "ISOCHRONE_PRUNE_SEGMENTS": 20,
        # Use 'progress' criterion - rewards progress toward destination
        # Combined with 'larger_direction' pruning (default), this prevents overshooting
        "ISOCHRONE_MINIMISATION_CRITERION": "squareddist_over_disttodest",
        "ISOCHRONE_NUMBER_OF_ROUTES": 1,
        
        "GENETIC_NUMBER_GENERATIONS": 15,
        "GENETIC_POPULATION_SIZE": 12,
        "GENETIC_NUMBER_OFFSPRINGS": 8,
        "GENETIC_POPULATION_TYPE": "grid_based",
        "GENETIC_REPAIR_TYPE": ["waypoints_infill", "constraint_violation"],
        "GENETIC_MUTATION_TYPE": "random",
        "GENETIC_CROSSOVER_PATCHER": "isofuel",
        
        "GCR_SLIDER_USE_POLYGON_LAND_DETECTION": True,
        "GCR_SLIDER_ANGLE_STEP": 30,
        "GCR_SLIDER_DISTANCE_MOVE": 10000,
        "GCR_SLIDER_LAND_BUFFER": 1000,
        "GCR_SLIDER_THRESHOLD": 10000,
        "GCR_SLIDER_MAX_POINTS": 300,
        
        # Greedy algorithm settings
        "GREEDY_DELTA_FUEL_KG": 500.0,       # Fuel per step (smaller = finer resolution)
        "GREEDY_HEADING_SAMPLES": 72,        # Sample every 5 degrees (360/72)
        "GREEDY_MAX_ITERATIONS": 1000,       # Safety limit
        "GREEDY_ARRIVAL_THRESHOLD_M": 2000.0, # Arrival distance in meters
        "GREEDY_USE_WEATHER": True,          # Use weather data if available
        
        # Dijkstra algorithm settings
        "DIJKSTRA_MASK_FILE": "/home/insectile/Development/Navica/WeatherRoutingTool/.venv/lib/python3.10/site-packages/global_land_mask/globe_combined_mask_compressed.npz",
        "DIJKSTRA_NOF_NEIGHBORS": 2,         # Connect to 2 neighbors in each direction for more route options
        "DIJKSTRA_STEP": 1,                  # Keep all waypoints
        "DIJKSTRA_USE_WEATHER": weather_source == "real",  # Use weather for fuel costs when available
        
        # A* algorithm settings
        # Graph bounds are computed DYNAMICALLY from the route - no hard-coded limits!
        # The router will expand/build graphs on-demand for any route worldwide.
        # 
        # SMART ADAPTIVE RESOLUTION based on route distance AND land proximity:
        #   < 200nm     → 0.05° (~5km)   - Fine: Coastal, regional routes
        #   200-1000nm  → 0.1° (~11km)   - Medium: Multi-day passages
        #   1000-5000nm → 0.25° (~28km)  - Coarse: Transoceanic routes
        #   > 5000nm    → 1.0° or 2.0°   - Based on land proximity:
        #                  Near land/straits: 1.0° (need connectivity through straits)
        #                  Open ocean only: 2.0° (can use coarse grid for speed)
        "ASTAR_GRID_RESOLUTION": select_astar_resolution(route_distance_nm, route_points or []),
        "ASTAR_NOF_NEIGHBORS": 1,            # 8 neighbors per node
        "ASTAR_LAND_CHECK_INTERVAL": (       # Adaptive land check interval
            1000 if route_distance_nm is None or route_distance_nm < 200 else
            1000 if route_distance_nm < 1000 else
            1000
        ),
        "ASTAR_USE_WEATHER": weather_source == "real",  # Use weather for edge costs
        # No ASTAR_LAT_MIN/MAX or ASTAR_LON_MIN/MAX - bounds are computed from route!
        # Weather avoidance thresholds (only for real weather)
        # Note: Using higher thresholds for POC; should be configurable via request
        "ASTAR_MAX_WAVE_HEIGHT_M": 5.0 if weather_source == "real" else None,  # Avoid >5m waves
        "ASTAR_MAX_WIND_SPEED_KTS": 50.0 if weather_source == "real" else None,  # Avoid >50kt wind (gale force)
        "ASTAR_MAX_CURRENT_SPEED_KTS": 4.0 if weather_source == "real" else None,  # Avoid >4kt currents (strong tidal)
        "ASTAR_WEATHER_PENALTY_FACTOR": 10.0,  # 10x cost penalty for hazardous edges
        # Dynamic corridor - limits search to area around route for performance
        "ASTAR_USE_CORRIDOR": True,          # Enable corridor filtering
        "ASTAR_CORRIDOR_FRACTION": 0.3,      # 30% of route distance as corridor width
        "ASTAR_CORRIDOR_MIN_KM": 100.0,      # Minimum 100km corridor for short routes
        
        "WEATHER_DATA": weather_path,
        "DEPTH_DATA": depth_path,
        "ROUTE_PATH": request_output_dir,
        "COURSES_FILE": request_output_dir,
    }
    
    # Log resolution being used
    if algorithm == "astar" and route_distance_nm:
        print(f"[WRT:{pass_id}] A* resolution: {config['ASTAR_GRID_RESOLUTION']:.2f}° for {route_distance_nm:.0f}nm route", flush=True)
    
    print(f"[WRT:{pass_id}] Running {algorithm} with {weather_source} weather...", flush=True)
    
    try:
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(executor, run_wrt_direct, config, request_output_dir)
    except Exception as e:
        return {"success": False, "error": f"Routing failed: {str(e)}"}
    
    if not result.get("success"):
        return {"success": False, "error": result.get("error", "Unknown routing error")}
    
    return {
        "success": True,
        "coords": result["coords"],
        "weather_cached": weather_cached,
        "weather_source": weather_source,
        "weather_hazards": result.get("weather_hazards")
    }


@app.get("/health", response_model=HealthResponse)
async def health_check():
    cache_count = len(list(Path(CACHE_DIR).glob("*.pkl"))) if Path(CACHE_DIR).exists() else 0
    
    return HealthResponse(
        status="healthy",
        service="52north-weather-routing-local",
        version="1.0.0-local",
        wrt_available=True,
        cache_entries=cache_count
    )


@app.post("/route", response_model=RouteResponse)
async def calculate_route(request: RouteRequest):
    """
    Calculate maritime route with optional weather optimization.
    
    When weather_optimization=False (Plan Route):
        - Runs isofuel with calm synthetic weather
        - Returns: route=calm_result, optimized_route=None
        
    When weather_optimization=True (Compare with Weather):
        - Runs isofuel with calm weather for baseline
        - Runs isofuel with real weather for optimization
        - Returns: route=calm_baseline, optimized_route=real_weather_result
    """
    start_time = time.time()
    
    print(f"\n{'='*60}", flush=True)
    print(f"[Route] New routing request", flush=True)
    
    # Debug: log full request JSON
    if DEBUG_LOGGING:
        request_dict = request.model_dump()
        print(f"[DEBUG] Full request JSON:", flush=True)
        print(json.dumps(request_dict, indent=2, default=str), flush=True)
    
    print(f"[Route] Start: ({request.start.lat}, {request.start.lon})", flush=True)
    print(f"[Route] End: ({request.end.lat}, {request.end.lon})", flush=True)
    print(f"[Route] Waypoints: {len(request.waypoints or [])}", flush=True)
    print(f"[Route] Weather optimization: {request.weather_optimization}", flush=True)
    
    vessel = request.vessel or VesselConfig()
    
    # Log vessel configuration including fuel rate source
    fuel_rate = vessel.fuel_rate_kg_per_hour or 167
    fuel_source = "from TypeScript fuel-curve.ts" if vessel.fuel_rate_kg_per_hour else "default (167 kg/hr)"
    print(f"[Route] Vessel config:", flush=True)
    print(f"[Route]   Speed: {vessel.speed_knots} knots", flush=True)
    print(f"[Route]   Fuel rate: {fuel_rate} kg/hr ({fuel_source})", flush=True)
    if vessel.smcr_power_kw:
        print(f"[Route]   SMCR power: {vessel.smcr_power_kw} kW", flush=True)
    if vessel.hotel_load_kw:
        print(f"[Route]   Hotel load: {vessel.hotel_load_kw} kW", flush=True)
    
    departure = format_departure_time(request.departure_time)
    
    # Calculate bounding box
    all_lats = [request.start.lat, request.end.lat] + [wp.lat for wp in (request.waypoints or [])]
    all_lons = [request.start.lon, request.end.lon] + [wp.lon for wp in (request.waypoints or [])]
    
    lat_min = min(all_lats) - 2
    lat_max = max(all_lats) + 2
    lon_min = min(all_lons) - 2
    lon_max = max(all_lons) + 2
    
    # Estimate great circle distance for adaptive resolution
    # Include waypoints in distance calculation
    route_points = [(request.start.lat, request.start.lon)] + \
                   [(wp.lat, wp.lon) for wp in (request.waypoints or [])] + \
                   [(request.end.lat, request.end.lon)]
    gc_distance_nm = 0.0
    for i in range(len(route_points) - 1):
        gc_distance_nm += calculate_distance_nm(
            route_points[i][0], route_points[i][1],
            route_points[i+1][0], route_points[i+1][1]
        )
    
    # Smart resolution selection based on distance AND land proximity
    selected_resolution = select_astar_resolution(gc_distance_nm, route_points)
    near_land = route_passes_near_land(route_points)
    
    # Log resolution choice with reasoning
    res_labels = {0.05: "fine (0.05°, ~5km)", 0.1: "medium (0.1°, ~11km)", 
                  0.25: "coarse (0.25°, ~28km)", 1.0: "very coarse (1.0°, ~111km)",
                  2.0: "ultra coarse (2.0°, ~222km)"}
    res_label = res_labels.get(selected_resolution, f"{selected_resolution}°")
    land_note = " [near land - using finer grid]" if gc_distance_nm >= 5000 and near_land else ""
    land_note = " [open ocean - using coarse grid]" if gc_distance_nm >= 5000 and not near_land else land_note
    print(f"[Route] Great circle distance: {gc_distance_nm:.1f} nm → {res_label} resolution{land_note}", flush=True)
    
    request_id = hashlib.md5(f"{request.start.lat}{request.start.lon}{request.end.lat}{request.end.lon}{time.time()}".encode()).hexdigest()[:8]
    
    # Algorithm selection (default: astar for reliable land avoidance + weather)
    algorithm = request.algorithm or "astar"
    if algorithm not in ["isofuel", "genetic", "gcrslider", "greedy", "dijkstra", "astar"]:
        return debug_log_response(RouteResponse(
            success=False,
            error=f"Unknown algorithm: {algorithm}. Options: isofuel, genetic, gcrslider, greedy, dijkstra, astar",
            execution_time_ms=(time.time() - start_time) * 1000
        ))
    
    # Validate waypoint support
    algorithms_with_waypoint_support = ["isofuel", "greedy", "astar"]
    if request.waypoints and algorithm not in algorithms_with_waypoint_support:
        from fastapi import HTTPException
        raise HTTPException(
            status_code=400,
            detail=f"Algorithm '{algorithm}' does not support intermediate waypoints. "
                   f"Use one of: {', '.join(algorithms_with_waypoint_support)}"
        )
    
    print(f"[Route] Algorithm: {algorithm}", flush=True)
    if request.time_forecast_hours:
        print(f"[Route] Time forecast override: {request.time_forecast_hours} hours", flush=True)
    
    # ============================================================
    # PASS 1: Calm weather baseline (always run)
    # ============================================================
    print(f"\n--- PASS 1: Calm weather baseline ---", flush=True)
    calm_result = await run_single_routing_pass(
        request=request,
        weather_source="calm",
        pass_id=f"{request_id}_calm",
        vessel=vessel,
        departure=departure,
        lat_min=lat_min, lat_max=lat_max,
        lon_min=lon_min, lon_max=lon_max,
        algorithm=algorithm,
        time_forecast_hours=request.time_forecast_hours,
        route_distance_nm=gc_distance_nm,
        route_points=route_points
    )
    
    if not calm_result.get("success"):
        return debug_log_response(RouteResponse(
            success=False,
            error=f"Calm weather routing failed: {calm_result.get('error')}",
            execution_time_ms=(time.time() - start_time) * 1000
        ))
    
    calm_coords = calm_result["coords"]
    calm_distance = calculate_route_distance(calm_coords)
    calm_time = calm_distance / vessel.speed_knots if vessel.speed_knots > 0 else 0
    
    calm_route = CalculatedRoute(
        waypoints=calm_coords,
        total_distance_nm=round(calm_distance, 2),
        total_time_hours=round(calm_time, 2)
    )
    
    # ============================================================
    # PASS 2: Real weather optimization (only if weather_optimization=True)
    # ============================================================
    if not request.weather_optimization:
        # Plan Route mode: return calm route only
        exec_time = (time.time() - start_time) * 1000
        print(f"\n[Route] Plan route complete in {exec_time:.0f}ms", flush=True)
        print(f"[Route] Calm weather route: {calm_distance:.1f}nm", flush=True)
        print(f"{'='*60}\n", flush=True)
        
        return debug_log_response(RouteResponse(
            success=True,
            route=calm_route,
            optimized_route=None,  # No optimization requested
            provider="52north-local",
            execution_time_ms=exec_time,
            weather_cached=True,
            algorithm_used=algorithm,
            weather_source_used="calm"
        ))
    
    # Weather optimization mode: run second pass with real weather
    print(f"\n--- PASS 2: Real weather optimization ---", flush=True)
    
    # Use caller-provided time_forecast_hours, or estimate from calm route time (with 50% buffer)
    real_weather_forecast_hours = request.time_forecast_hours
    if real_weather_forecast_hours is None and calm_time > 0:
        # Use calm route time as estimate, add 50% buffer, round up to nearest 24 hours
        estimated_hours = int(calm_time * 1.5)
        real_weather_forecast_hours = max(48, ((estimated_hours // 24) + 1) * 24)  # Min 48 hours
        real_weather_forecast_hours = min(real_weather_forecast_hours, 144)  # Max 6 days (OpenMeteo limit)
        print(f"[Route] Auto-sized forecast window from calm route: {calm_time:.1f}h -> {real_weather_forecast_hours}h", flush=True)
    
    # Use the calm route as the corridor for weather fetching (much more efficient)
    # calm_coords is a list of [lat, lon] arrays - convert to (lat, lon) tuples
    corridor_points = [(p[0], p[1]) for p in calm_coords]
    print(f"[Route] Using calm route as weather corridor: {len(corridor_points)} waypoints", flush=True)
    
    real_result = await run_single_routing_pass(
        request=request,
        weather_source="real",
        pass_id=f"{request_id}_real",
        vessel=vessel,
        departure=departure,
        lat_min=lat_min, lat_max=lat_max,
        lon_min=lon_min, lon_max=lon_max,
        algorithm=algorithm,
        time_forecast_hours=real_weather_forecast_hours,
        route_distance_nm=gc_distance_nm,
        route_points=corridor_points  # Use calm route as corridor, not original waypoints
    )
    
    if not real_result.get("success"):
        # If real weather fails, return calm route with error note
        exec_time = (time.time() - start_time) * 1000
        error_msg = real_result.get("error", "Unknown error")
        print(f"[Route] Real weather routing failed: {error_msg}", flush=True)
        print(f"[Route] Returning calm route only", flush=True)
        return debug_log_response(RouteResponse(
            success=True,
            route=calm_route,
            optimized_route=None,
            provider="52north-local",
            execution_time_ms=exec_time,
            weather_cached=True,
            algorithm_used=algorithm,
            weather_source_used=f"calm (real weather failed: {error_msg})"
        ))
    
    real_coords = real_result["coords"]
    real_distance = calculate_route_distance(real_coords)
    real_time = real_distance / vessel.speed_knots if vessel.speed_knots > 0 else 0
    
    optimized_route = CalculatedRoute(
        waypoints=real_coords,
        total_distance_nm=round(real_distance, 2),
        total_time_hours=round(real_time, 2)
    )
    
    exec_time = (time.time() - start_time) * 1000
    print(f"\n[Route] Weather comparison complete in {exec_time:.0f}ms", flush=True)
    print(f"[Route] Calm weather baseline: {calm_distance:.1f}nm", flush=True)
    print(f"[Route] Real weather optimized: {real_distance:.1f}nm", flush=True)
    print(f"[Route] Difference: {real_distance - calm_distance:+.1f}nm", flush=True)
    print(f"{'='*60}\n", flush=True)
    
    # Build weather hazard report if available
    weather_hazard_report = None
    raw_hazards = real_result.get("weather_hazards")
    if raw_hazards:
        weather_hazard_report = WeatherHazardReport(
            hazards=[WeatherHazard(**h) for h in raw_hazards.get("hazards", [])],
            summary=WeatherHazardSummary(**raw_hazards.get("summary", {})),
            thresholds=raw_hazards.get("thresholds", {})
        )
        print(f"[Route] Weather hazards: {raw_hazards['summary']}", flush=True)
    
    return debug_log_response(RouteResponse(
        success=True,
        route=calm_route,              # Baseline with calm weather
        optimized_route=optimized_route,  # Optimized with real weather
        provider="52north-local",
        execution_time_ms=exec_time,
        weather_cached=real_result.get("weather_cached", False),
        algorithm_used=algorithm,
        weather_source_used="calm vs real",
        weather_hazards=weather_hazard_report
    ))


@app.delete("/cache")
async def clear_cache():
    """Clear all cache entries."""
    cache_path = Path(CACHE_DIR)
    count = 0
    if cache_path.exists():
        for f in cache_path.glob("*.pkl"):
            f.unlink()
            count += 1
    return {"success": True, "message": f"Cache cleared ({count} entries removed)"}


@app.post("/cache/cleanup")
async def cleanup_cache():
    """Remove stale cache entries (older than 24 hours)."""
    removed = cleanup_stale_cache()
    return {"success": True, "removed": removed, "message": f"Cleaned up {removed} stale entries"}


@app.on_event("startup")
async def startup_event():
    """Run cache cleanup on startup."""
    print("[Startup] Cleaning up stale cache entries...", flush=True)
    removed = cleanup_stale_cache()
    if removed > 0:
        print(f"[Startup] Removed {removed} stale cache entries", flush=True)
    else:
        print("[Startup] Cache is clean", flush=True)


@app.get("/")
async def root():
    return {
        "service": "52North Weather Routing - Local Dev Server",
        "status": "running",
        "endpoints": ["/health", "/route", "/cache", "/cache/cleanup"],
        "repo": str(WRT_REPO_DIR)
    }


if __name__ == "__main__":
    frozen_status = f"ON (key: {FROZEN_TIME_KEY})" if FROZEN_TIME_MODE else "OFF"
    print(f"""
╔══════════════════════════════════════════════════════════════╗
║  52North WeatherRoutingTool - Local Development Server       ║
╠══════════════════════════════════════════════════════════════╣
║  WRT Repo: {str(WRT_REPO_DIR):<47} ║
║  Data Dir: {DATA_DIR:<47} ║
║  PostGIS:  localhost:5433/gis_db                             ║
║  Frozen Time Mode: {frozen_status:<40} ║
╠══════════════════════════════════════════════════════════════╣
║  Starting on http://localhost:8001                           ║
╚══════════════════════════════════════════════════════════════╝
""")
    uvicorn.run(app, host="0.0.0.0", port=8001, reload=False)
