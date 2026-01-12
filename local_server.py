#!/usr/bin/env python3
"""
52North Weather Routing Service

FastAPI wrapper for 52North WeatherRoutingTool with OpenMeteo weather data.
This service fetches weather data on-demand and runs weather-optimized routing.

Based on reference implementation from /Users/insectile/Development/54north/playground
"""
import os
import json
import math
import hashlib
import pickle
import subprocess
import asyncio
import sys
from typing import List, Dict, Any, Optional
from pathlib import Path
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import uvicorn

try:
    import requests
    import numpy as np
    import xarray as xr
except ImportError as e:
    print(f"Missing dependency: {e}")
    print("Install with: pip install requests numpy xarray netcdf4")
    sys.exit(1)

# ============================================================
# Configuration
# ============================================================
WRT_HOME = os.environ.get('WRT_HOME', '/opt/wrt')
WRT_REPO_DIR = f"{WRT_HOME}/WeatherRoutingTool"
DATA_DIR = os.environ.get('DATA_DIR', '/data')
OUTPUT_DIR = os.environ.get('OUTPUT_DIR', f'{DATA_DIR}/routes')
CACHE_DIR = f"{DATA_DIR}/.cache"
WEATHER_CACHE_TTL_SECONDS = int(os.environ.get('WEATHER_CACHE_TTL_SECONDS', '3600'))  # 1 hour default

# For running WRT
PYTHON_CMD = f"{WRT_REPO_DIR}/venv/bin/python"

# Thread pool for CPU-bound operations
executor = ThreadPoolExecutor(max_workers=2)

# ============================================================
# Weather Data Cache (like the playground implementation)
# ============================================================
def get_cache_key(lat: float, lon: float, forecast_days: int) -> str:
    """Generate a cache key for a point request."""
    today = datetime.now().strftime("%Y-%m-%d")
    key = f"{lat:.2f}_{lon:.2f}_{forecast_days}_{today}"
    return hashlib.md5(key.encode()).hexdigest()[:12]


def get_cached_response(cache_key: str) -> dict | None:
    """Load cached response if available."""
    cache_path = Path(CACHE_DIR)
    cache_file = cache_path / f"{cache_key}.pkl"
    if cache_file.exists():
        # Check age
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
    """Save response to cache."""
    cache_path = Path(CACHE_DIR)
    cache_path.mkdir(parents=True, exist_ok=True)
    cache_file = cache_path / f"{cache_key}.pkl"
    try:
        with open(cache_file, 'wb') as f:
            pickle.dump(data, f)
    except:
        pass


# ============================================================
# OpenMeteo Weather Fetching (from playground/scripts/fetch_openmeteo.py)
# ============================================================
def fetch_marine_grid(
    lat_min: float, lat_max: float,
    lon_min: float, lon_max: float,
    resolution: float = 0.5,
    forecast_days: int = 7
) -> xr.Dataset:
    """
    Fetch marine weather data from OpenMeteo for a grid of points.
    
    OpenMeteo doesn't provide gridded data directly, so we sample points
    and interpolate to create a grid suitable for WRT.
    """
    
    # Create grid points
    lats = np.arange(lat_min, lat_max + resolution, resolution)
    lons = np.arange(lon_min, lon_max + resolution, resolution)
    
    print(f"[Weather] Grid: {len(lats)} x {len(lons)} = {len(lats) * len(lons)} points")
    
    # OpenMeteo API endpoints
    marine_url = "https://marine-api.open-meteo.com/v1/marine"
    weather_url = "https://api.open-meteo.com/v1/forecast"
    
    # Variables we need for WRT
    hourly_vars = [
        "wave_height",
        "wave_direction", 
        "wave_period",
        "wind_wave_height",
        "wind_wave_direction",
        "swell_wave_height",
    ]
    wind_vars = ["wind_speed_10m", "wind_direction_10m"]
    
    all_data = []
    total_points = len(lats) * len(lons)
    cached_count = 0
    fetched_count = 0
    
    print(f"[Weather] Fetching data for {total_points} grid points...")
    
    for i, lat in enumerate(lats):
        row_data = []
        for j, lon in enumerate(lons):
            point_num = i * len(lons) + j + 1
            
            # Check cache first
            cache_key = get_cache_key(lat, lon, forecast_days)
            cached = get_cached_response(cache_key)
            if cached:
                row_data.append(cached)
                cached_count += 1
                print(f"[Weather] Point {point_num}/{total_points}: ({lat:.2f}, {lon:.2f}) - cached")
                continue
            
            fetched_count += 1
            print(f"[Weather] Point {point_num}/{total_points}: ({lat:.2f}, {lon:.2f}) - fetching...", end="", flush=True)
            
            # Fetch marine data
            marine_params = {
                "latitude": lat,
                "longitude": lon,
                "hourly": ",".join(hourly_vars),
                "forecast_days": forecast_days,
                "timezone": "UTC"
            }
            
            # Fetch wind data
            wind_params = {
                "latitude": lat,
                "longitude": lon,
                "hourly": ",".join(wind_vars),
                "forecast_days": forecast_days,
                "timezone": "UTC"
            }
            
            try:
                marine_resp = requests.get(marine_url, params=marine_params, timeout=30)
                wind_resp = requests.get(weather_url, params=wind_params, timeout=30)
                
                if marine_resp.status_code == 200 and wind_resp.status_code == 200:
                    marine_data = marine_resp.json()
                    wind_data = wind_resp.json()
                    
                    point_data = {
                        'lat': lat,
                        'lon': lon,
                        'marine': marine_data,
                        'wind': wind_data
                    }
                    row_data.append(point_data)
                    save_to_cache(cache_key, point_data)
                    print(" ✓")
                else:
                    row_data.append(None)
                    print(f" ✗ (status: marine={marine_resp.status_code}, wind={wind_resp.status_code})")
                    
            except Exception as e:
                print(f" ✗ ({e})")
                row_data.append(None)
        
        all_data.append(row_data)
    
    print(f"[Weather] ✓ Complete: {fetched_count} fetched, {cached_count} from cache ({total_points} total)")
    
    # Convert to xarray Dataset
    return convert_to_xarray(all_data, lats, lons)


def convert_to_xarray(all_data: list, lats: np.ndarray, lons: np.ndarray) -> xr.Dataset:
    """Convert fetched OpenMeteo data to xarray Dataset in WRT format."""
    
    # Get time coordinates from first valid point
    times = None
    for row in all_data:
        for point in row:
            if point is not None:
                time_strs = point['marine']['hourly']['time']
                times = [datetime.fromisoformat(t) for t in time_strs]
                break
        if times:
            break
    
    if times is None:
        raise ValueError("No valid weather data points found")
    
    n_times = len(times)
    n_lats = len(lats)
    n_lons = len(lons)
    
    # Initialize arrays
    wave_height = np.full((n_times, n_lats, n_lons), np.nan)
    wave_direction = np.full((n_times, n_lats, n_lons), np.nan)
    wave_period = np.full((n_times, n_lats, n_lons), np.nan)
    wind_speed = np.full((n_times, n_lats, n_lons), np.nan)
    wind_direction = np.full((n_times, n_lats, n_lons), np.nan)
    u10 = np.full((n_times, n_lats, n_lons), np.nan)
    v10 = np.full((n_times, n_lats, n_lons), np.nan)
    
    # Fill arrays from fetched data
    for i, row in enumerate(all_data):
        for j, point in enumerate(row):
            if point is None:
                continue
            
            marine = point['marine']['hourly']
            wind = point['wind']['hourly']
            
            # Wave data
            if 'wave_height' in marine and marine['wave_height']:
                wave_height[:, i, j] = marine['wave_height'][:n_times]
            if 'wave_direction' in marine and marine['wave_direction']:
                wave_direction[:, i, j] = marine['wave_direction'][:n_times]
            if 'wave_period' in marine and marine['wave_period']:
                wave_period[:, i, j] = marine['wave_period'][:n_times]
            
            # Wind data - convert speed/direction to u/v components
            if 'wind_speed_10m' in wind and wind['wind_speed_10m']:
                ws = np.array(wind['wind_speed_10m'][:n_times])
                wd = np.array(wind['wind_direction_10m'][:n_times])
                
                wind_speed[:, i, j] = ws
                wind_direction[:, i, j] = wd
                
                # Convert to u,v components (meteorological convention)
                wd_rad = np.radians(wd)
                u10[:, i, j] = -ws * np.sin(wd_rad)
                v10[:, i, j] = -ws * np.cos(wd_rad)
    
    # Create placeholder arrays for variables WRT expects but OpenMeteo doesn't provide
    zeros_4d_depth = np.zeros((n_times, 1, n_lats, n_lons))
    standard_pressure = np.full((n_times, n_lats, n_lons), 101325.0)
    standard_temp = np.full((n_times, n_lats, n_lons), 288.0)
    water_temp_4d = np.full((n_times, 1, n_lats, n_lons), 15.0)
    salinity_4d = np.full((n_times, 1, n_lats, n_lons), 35.0)
    
    # Wind needs height_above_ground dimension
    u10_4d = u10[:, np.newaxis, :, :]
    v10_4d = v10[:, np.newaxis, :, :]
    
    # Create xarray Dataset with all variables WRT expects
    ds = xr.Dataset(
        {
            # Wave data
            'VHM0': (['time', 'latitude', 'longitude'], wave_height,
                    {'long_name': 'Significant wave height', 'units': 'm'}),
            'VMDR': (['time', 'latitude', 'longitude'], wave_direction,
                    {'long_name': 'Mean wave direction', 'units': 'degree'}),
            'VTPK': (['time', 'latitude', 'longitude'], wave_period,
                    {'long_name': 'Peak wave period', 'units': 's'}),
            
            # Wind data with height_above_ground dimension
            'u-component_of_wind_height_above_ground': (['time', 'height_above_ground', 'latitude', 'longitude'], u10_4d,
                    {'long_name': 'U-component of wind at 10m', 'units': 'm s-1'}),
            'v-component_of_wind_height_above_ground': (['time', 'height_above_ground', 'latitude', 'longitude'], v10_4d,
                    {'long_name': 'V-component of wind at 10m', 'units': 'm s-1'}),
            
            # Ocean currents with depth dimension (placeholder zeros)
            'utotal': (['time', 'depth', 'latitude', 'longitude'], zeros_4d_depth.copy(),
                    {'long_name': 'U-component of ocean current', 'units': 'm s-1'}),
            'vtotal': (['time', 'depth', 'latitude', 'longitude'], zeros_4d_depth.copy(),
                    {'long_name': 'V-component of ocean current', 'units': 'm s-1'}),
            
            # Atmospheric properties
            'Pressure_reduced_to_MSL_msl': (['time', 'latitude', 'longitude'], standard_pressure,
                    {'long_name': 'Pressure reduced to MSL', 'units': 'Pa'}),
            'Temperature_surface': (['time', 'latitude', 'longitude'], standard_temp,
                    {'long_name': 'Surface air temperature', 'units': 'K'}),
            
            # Ocean properties with depth dimension
            'thetao': (['time', 'depth', 'latitude', 'longitude'], water_temp_4d,
                    {'long_name': 'Sea water temperature', 'units': 'degrees_C'}),
            'so': (['time', 'depth', 'latitude', 'longitude'], salinity_4d,
                    {'long_name': 'Sea water salinity', 'units': '1e-3'}),
            
            # Keep original names for reference
            'u10': (['time', 'latitude', 'longitude'], u10,
                    {'long_name': 'U-component of 10m wind', 'units': 'm/s'}),
            'v10': (['time', 'latitude', 'longitude'], v10,
                    {'long_name': 'V-component of 10m wind', 'units': 'm/s'}),
        },
        coords={
            'time': times,
            'latitude': lats,
            'longitude': lons,
            'height_above_ground': [10],
            'depth': [0.5],
        },
        attrs={
            'title': 'Weather and marine data from OpenMeteo',
            'source': 'https://open-meteo.com/',
            'history': f'Created {datetime.now().isoformat()}',
            'Conventions': 'CF-1.6',
        }
    )
    
    return ds


# ============================================================
# FastAPI App
# ============================================================
app = FastAPI(
    title="52North Weather Routing Service",
    description="Weather-optimized maritime routing using 52North WeatherRoutingTool with OpenMeteo data",
    version="2.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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

class RouteRequest(BaseModel):
    start: RoutePoint
    end: RoutePoint
    waypoints: Optional[List[RoutePoint]] = []
    departure_time: Optional[str] = None
    vessel: Optional[VesselConfig] = None
    weather_optimization: bool = True

class CalculatedRoute(BaseModel):
    waypoints: List[List[float]]
    total_distance_nm: float
    total_time_hours: float

class RouteResponse(BaseModel):
    success: bool
    error: Optional[str] = None
    route: Optional[CalculatedRoute] = None
    optimized_route: Optional[CalculatedRoute] = None
    provider: str = "52north"
    execution_time_ms: Optional[float] = None
    weather_cached: bool = False

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
    """Calculate distance between two points in nautical miles (Haversine)"""
    lat1_rad, lon1_rad = math.radians(lat1), math.radians(lon1)
    lat2_rad, lon2_rad = math.radians(lat2), math.radians(lon2)
    
    dlat = lat2_rad - lat1_rad
    dlon = lon2_rad - lon1_rad
    
    a = math.sin(dlat/2)**2 + math.cos(lat1_rad) * math.cos(lat2_rad) * math.sin(dlon/2)**2
    c = 2 * math.asin(math.sqrt(a))
    
    return 6371 * 0.539957 * c

def calculate_route_distance(coords: List[List[float]]) -> float:
    """Calculate total route distance in nautical miles"""
    if len(coords) < 2:
        return 0.0
    
    total = 0.0
    for i in range(len(coords) - 1):
        total += calculate_distance_nm(coords[i][0], coords[i][1], coords[i+1][0], coords[i+1][1])
    return total

def ensure_directories():
    """Ensure required directories exist"""
    for directory in [DATA_DIR, OUTPUT_DIR, CACHE_DIR]:
        Path(directory).mkdir(parents=True, exist_ok=True)

def check_wrt_installation() -> bool:
    """Check if WeatherRoutingTool is properly installed"""
    try:
        wrt_path = Path(WRT_REPO_DIR)
        if not wrt_path.exists():
            return False
        
        # Check if we can import WRT
        result = subprocess.run(
            [PYTHON_CMD, "-c", "from WeatherRoutingTool.execute_routing import execute_routing; print('OK')"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=WRT_HOME
        )
        return result.returncode == 0 and 'OK' in result.stdout
    except:
        return False

def format_departure_time(iso_string: Optional[str]) -> str:
    """Format departure time for 52North"""
    if not iso_string:
        dt = datetime.now(timezone.utc) + timedelta(hours=1)
    else:
        try:
            dt = datetime.fromisoformat(iso_string.replace('Z', '+00:00'))
        except:
            dt = datetime.now(timezone.utc) + timedelta(hours=1)
    
    return dt.strftime('%Y-%m-%dT%H:%MZ')

def get_cache_entry_count() -> int:
    """Count cache entries"""
    cache_path = Path(CACHE_DIR)
    if not cache_path.exists():
        return 0
    return len(list(cache_path.glob("*.pkl")))


def create_synthetic_depth(
    lat_min: float, lat_max: float,
    lon_min: float, lon_max: float,
    resolution: float = 0.25,
    output_path: str = None
) -> str:
    """
    Create synthetic depth data for the route region.
    WRT requires DEPTH_DATA to be a valid NetCDF file path.
    We create a simple deep ocean dataset since we're using
    land_crossing_global_land_mask for coast constraints.
    """
    if output_path is None:
        output_path = f"{DATA_DIR}/synthetic_depth.nc"
    
    # Check if we already have depth data for this region (reuse)
    if Path(output_path).exists():
        try:
            existing = xr.open_dataset(output_path)
            existing_lats = existing.coords['latitude'].values
            existing_lons = existing.coords['longitude'].values
            
            # Check if existing data covers our region
            if (existing_lats.min() <= lat_min and existing_lats.max() >= lat_max and
                existing_lons.min() <= lon_min and existing_lons.max() >= lon_max):
                existing.close()
                print(f"[Depth] Using existing depth data: {output_path}")
                return output_path
            existing.close()
        except:
            pass
    
    print(f"[Depth] Creating synthetic depth data for region...")
    
    # Create grid with padding
    pad = 2.0  # Extra degrees around region
    lats = np.arange(lat_min - pad, lat_max + pad + resolution, resolution)
    lons = np.arange(lon_min - pad, lon_max + pad + resolution, resolution)
    
    n_lats = len(lats)
    n_lons = len(lons)
    
    # Create deep ocean depth field (negative values = below sea level)
    # WRT uses depth constraints to avoid shallow areas
    # Since we use land_crossing_global_land_mask, we just need valid depth values
    # Use reasonable ocean depths: -3000 to -5000 meters
    depth = np.random.uniform(-5000, -3000, (n_lats, n_lons))
    
    # Create xarray dataset with format WRT expects (matching reference implementation)
    ds = xr.Dataset(
        {
            'depth': (['latitude', 'longitude'], depth.astype(np.float64)),
        },
        coords={
            'latitude': lats.astype(np.float64),
            'longitude': lons.astype(np.float64),
        }
    )
    
    ds.attrs['title'] = 'Synthetic depth data for WRT routing'
    ds.attrs['source'] = 'NavicaAI weather-routing service'
    ds.attrs['created'] = datetime.now(timezone.utc).isoformat()
    
    # Save
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    ds.to_netcdf(output_path)
    
    print(f"[Depth] ✓ Created depth data: {output_path} ({n_lats}x{n_lons} grid)")
    return output_path

# ============================================================
# Route Execution
# ============================================================
def run_wrt_sync(config: dict, weather_path: str, output_dir: str) -> dict:
    """
    Run WRT using subprocess with WRT's virtualenv Python.
    WRT has its own dependencies (cartopy, etc.) in its venv.
    """
    import tempfile
    
    # Create a temp config file
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        json.dump(config, f, indent=2)
        config_path = f.name
    
    try:
        # Create a runner script that uses WRT's API
        runner_script = f'''
import sys
sys.path.insert(0, "{WRT_REPO_DIR}")
from pathlib import Path
from WeatherRoutingTool.execute_routing import execute_routing
from WeatherRoutingTool.config import Config

config = Config.assign_config(Path("{config_path}"))
execute_routing(config)
print("ROUTING_COMPLETE")
'''
        
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
            f.write(runner_script)
            runner_path = f.name
        
        # Run using WRT's venv Python which has all dependencies
        print(f"[WRT] Starting routing with WRT venv Python...")
        result = subprocess.run(
            [PYTHON_CMD, runner_path],
            capture_output=True,
            text=True,
            timeout=600,  # 10 minute timeout
            cwd=WRT_REPO_DIR
        )
        
        # Clean up runner script
        try:
            Path(runner_path).unlink()
        except:
            pass
        
        if result.returncode != 0:
            error_msg = result.stderr or result.stdout or "Unknown WRT error"
            print(f"[WRT] Error: {error_msg}")
            return {"success": False, "error": error_msg}
        
        if "ROUTING_COMPLETE" not in result.stdout:
            print(f"[WRT] Output: {result.stdout}")
            print(f"[WRT] Stderr: {result.stderr}")
            return {"success": False, "error": "WRT did not complete successfully"}
        
        print(f"[WRT] Routing completed, looking for output files...")
        
        # Find any route JSON output file
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
                                coords.append([c[1], c[0]])  # GeoJSON is [lon, lat]
                        elif geom.get('type') == 'LineString':
                            for c in geom.get('coordinates', []):
                                if len(c) >= 2:
                                    coords.append([c[1], c[0]])
                
                if coords:
                    print(f"[WRT] Found route in {route_file.name}")
                    return {"success": True, "coords": coords}
            except:
                continue
        
        return {"success": False, "error": "No route output found"}
        
    except Exception as e:
        return {"success": False, "error": str(e)}
    finally:
        # Cleanup
        try:
            Path(config_path).unlink()
        except:
            pass


async def calculate_route_async(request: RouteRequest) -> RouteResponse:
    """Calculate route with weather optimization"""
    import time
    start_time = time.time()
    
    ensure_directories()
    
    vessel = request.vessel or VesselConfig()
    departure = format_departure_time(request.departure_time)
    
    # Calculate bounding box with buffer (matching reference implementation)
    all_points = [request.start, request.end] + (request.waypoints or [])
    buffer_deg = 2.0  # Smaller buffer like reference
    lat_min = min(p.lat for p in all_points) - buffer_deg
    lat_max = max(p.lat for p in all_points) + buffer_deg
    lon_min = min(p.lon for p in all_points) - buffer_deg
    lon_max = max(p.lon for p in all_points) + buffer_deg
    
    # Create unique output directory for this request
    request_id = hashlib.md5(f"{request.start.lat}{request.start.lon}{request.end.lat}{request.end.lon}{time.time()}".encode()).hexdigest()[:8]
    request_output_dir = f"{OUTPUT_DIR}/{request_id}"
    Path(request_output_dir).mkdir(parents=True, exist_ok=True)
    
    # Fetch weather data from OpenMeteo
    print(f"[Route] Fetching weather data for region: ({lat_min:.1f}, {lon_min:.1f}) to ({lat_max:.1f}, {lon_max:.1f})")
    
    try:
        # Run weather fetch in thread pool to not block
        loop = asyncio.get_event_loop()
        weather_ds = await loop.run_in_executor(
            executor,
            fetch_marine_grid,
            lat_min, lat_max, lon_min, lon_max,
            1.5,  # resolution in degrees (coarser grid, fewer API calls)
            7     # forecast days
        )
        
        # Save weather data
        weather_path = f"{DATA_DIR}/weather_{request_id}.nc"
        weather_ds.to_netcdf(weather_path)
        print(f"[Route] Weather data saved to {weather_path}")
        
    except Exception as e:
        return RouteResponse(
            success=False,
            error=f"Failed to fetch weather data: {str(e)}",
            execution_time_ms=(time.time() - start_time) * 1000
        )
    
    # Create synthetic depth data for the region (WRT requires DEPTH_DATA to be a string)
    try:
        depth_path = await loop.run_in_executor(
            executor,
            create_synthetic_depth,
            lat_min, lat_max, lon_min, lon_max,
            0.5,  # resolution
            f"{DATA_DIR}/depth_{request_id}.nc"
        )
    except Exception as e:
        print(f"[Depth] Warning: Failed to create depth data: {e}")
        # Use a default path - will create minimal depth data
        depth_path = f"{DATA_DIR}/depth_default.nc"
        try:
            create_synthetic_depth(lat_min, lat_max, lon_min, lon_max, 0.5, depth_path)
        except:
            pass  # Continue anyway, WRT may work without depth constraints
    
    # Create WRT config
    config = {
        "DEFAULT_ROUTE": [request.start.lat, request.start.lon, request.end.lat, request.end.lon],
        "DEFAULT_MAP": [lat_min, lon_min, lat_max, lon_max],
        "DEPARTURE_TIME": departure,
        "INTERMEDIATE_WAYPOINTS": [[p.lat, p.lon] for p in (request.waypoints or [])],
        
        "ALGORITHM_TYPE": "isofuel" if request.weather_optimization else "gcr_slider",
        "BOAT_TYPE": "direct_power_method",
        "BOAT_DRAUGHT_AFT": vessel.draught,
        "BOAT_DRAUGHT_FORE": vessel.draught,
        "BOAT_BREADTH": vessel.breadth,
        "BOAT_LENGTH": vessel.length,
        "BOAT_SPEED": vessel.speed_knots * 0.5144,  # Convert knots to m/s
        "BOAT_SMCR_SPEED": vessel.speed_knots * 0.5144,
        "BOAT_FUEL_RATE": 167,
        "BOAT_HBR": 30,
        "BOAT_SMCR_POWER": 6502,
        
        "CONSTRAINTS_LIST": ["land_crossing_global_land_mask", "on_map"],
        "TIME_FORECAST": 96,  # 4 days in hours (must be less than weather data coverage)
        "ROUTING_STEPS": 40,
        "DELTA_TIME_FORECAST": 3,
        "DELTA_FUEL": 3000,
        "ROUTER_HDGS_SEGMENTS": 30,
        "ROUTER_HDGS_INCREMENTS_DEG": 6,
        "ISOCHRONE_PRUNE_SECTOR_DEG_HALF": 91,
        "ISOCHRONE_PRUNE_SEGMENTS": 20,
        "ISOCHRONE_MINIMISATION_CRITERION": "squareddist_over_disttodest",
        "ISOCHRONE_NUMBER_OF_ROUTES": 1,
        
        "WEATHER_DATA": weather_path,
        "DEPTH_DATA": depth_path,
        "ROUTE_PATH": request_output_dir,
    }
    
    # Run WRT
    print(f"[Route] Running WRT routing...")
    try:
        result = await loop.run_in_executor(
            executor,
            run_wrt_sync,
            config,
            weather_path,
            request_output_dir
        )
    except Exception as e:
        return RouteResponse(
            success=False,
            error=f"Routing failed: {str(e)}",
            execution_time_ms=(time.time() - start_time) * 1000
        )
    
    if not result.get("success"):
        return RouteResponse(
            success=False,
            error=result.get("error", "Unknown routing error"),
            execution_time_ms=(time.time() - start_time) * 1000
        )
    
    # Calculate metrics for optimized route
    opt_coords = result["coords"]
    opt_distance = calculate_route_distance(opt_coords)
    opt_time = opt_distance / vessel.speed_knots if vessel.speed_knots > 0 else 0
    
    optimized = CalculatedRoute(
        waypoints=opt_coords,
        total_distance_nm=round(opt_distance, 2),
        total_time_hours=round(opt_time, 2)
    )
    
    # Also return direct route for comparison
    direct_coords = [
        [request.start.lat, request.start.lon],
        *[[p.lat, p.lon] for p in (request.waypoints or [])],
        [request.end.lat, request.end.lon]
    ]
    direct_distance = calculate_route_distance(direct_coords)
    direct_time = direct_distance / vessel.speed_knots if vessel.speed_knots > 0 else 0
    
    direct = CalculatedRoute(
        waypoints=direct_coords,
        total_distance_nm=round(direct_distance, 2),
        total_time_hours=round(direct_time, 2)
    )
    
    print(f"[Route] ✓ Routing complete in {(time.time() - start_time):.1f}s")
    
    return RouteResponse(
        success=True,
        route=direct,
        optimized_route=optimized,
        provider="52north",
        execution_time_ms=round((time.time() - start_time) * 1000, 2),
        weather_cached=False  # TODO: track this properly
    )

# ============================================================
# API Endpoints
# ============================================================
@app.get("/health", response_model=HealthResponse)
async def health_check():
    """Health check endpoint"""
    ensure_directories()
    wrt_available = check_wrt_installation()
    
    return HealthResponse(
        status="healthy" if wrt_available else "degraded",
        service="52north-routing",
        version="2.0.0",
        wrt_available=wrt_available,
        cache_entries=get_cache_entry_count()
    )

@app.post("/route", response_model=RouteResponse)
async def calculate_route(request: RouteRequest):
    """Calculate a maritime route with optional weather optimization"""
    result = await calculate_route_async(request)
    if not result.success:
        raise HTTPException(status_code=500, detail=result.error or "Unknown error")
    return result

@app.delete("/cache")
async def clear_cache():
    """Clear the weather data cache"""
    cache_path = Path(CACHE_DIR)
    if cache_path.exists():
        for f in cache_path.glob("*.pkl"):
            f.unlink()
    return {"message": "Cache cleared", "success": True}

@app.get("/")
async def root():
    """Root endpoint"""
    return {
        "service": "52North Weather Routing Service",
        "version": "2.0.0",
        "provider": "52north",
        "endpoints": {
            "health": "/health",
            "route": "/route (POST)",
            "cache": "/cache (DELETE)",
            "docs": "/docs"
        }
    }

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
