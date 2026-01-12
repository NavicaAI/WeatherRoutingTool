#!/usr/bin/env python3
"""
Local Development Server for WeatherRoutingTool

This is a simplified FastAPI wrapper for rapid local development.
It directly imports and runs WRT instead of using subprocess.

Usage:
  cd /home/insectile/Development/Navica/WeatherRoutingTool
  source venv/bin/activate
  python local_dev_server.py

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
from typing import List, Optional
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

# PostGIS config (connects to NavicaAIPlatform's PostGIS container)
os.environ.setdefault('WRT_DB_HOST', 'localhost')
os.environ.setdefault('WRT_DB_PORT', '5433')
os.environ.setdefault('WRT_DB_DATABASE', 'gis_db')
os.environ.setdefault('WRT_DB_USERNAME', 'gis_user')
os.environ.setdefault('WRT_DB_PASSWORD', 'postgis_password')

# Thread pool for CPU-bound operations
executor = ThreadPoolExecutor(max_workers=2)

# Ensure directories exist
Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
Path(CACHE_DIR).mkdir(parents=True, exist_ok=True)

# ============================================================
# Weather Data Cache
# ============================================================
def get_cache_key(lat: float, lon: float, forecast_days: int) -> str:
    today = datetime.now().strftime("%Y-%m-%d")
    key = f"{lat:.2f}_{lon:.2f}_{forecast_days}_{today}"
    return hashlib.md5(key.encode()).hexdigest()[:12]


def get_cached_response(cache_key: str) -> dict | None:
    cache_path = Path(CACHE_DIR)
    cache_file = cache_path / f"{cache_key}.pkl"
    if cache_file.exists():
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
    cache_path = Path(CACHE_DIR)
    cache_path.mkdir(parents=True, exist_ok=True)
    cache_file = cache_path / f"{cache_key}.pkl"


def cleanup_stale_cache():
    """
    Remove cache entries that are in the past (based on the date encoded in the cache key).
    Cache keys include the date, so any file older than today's date is stale.
    """
    cache_path = Path(CACHE_DIR)
    if not cache_path.exists():
        return 0
    
    today = datetime.now().strftime("%Y-%m-%d")
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
    try:
        with open(cache_file, 'wb') as f:
            pickle.dump(data, f)
    except:
        pass


# ============================================================
# OpenMeteo Weather Fetching
# ============================================================
def fetch_marine_grid(
    lat_min: float, lat_max: float,
    lon_min: float, lon_max: float,
    resolution: float = 1.0,  # Increased from 0.5 for faster fetching
    forecast_days: int = 3    # Reduced from 7 for faster fetching
) -> xr.Dataset:
    lats = np.arange(lat_min, lat_max + resolution, resolution)
    lons = np.arange(lon_min, lon_max + resolution, resolution)
    
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
    
    for lat in lats:
        for lon in lons:
            current += 1
            cache_key = get_cache_key(lat, lon, forecast_days)
            cached = get_cached_response(cache_key)
            
            if cached:
                all_data.append(cached)
                continue
            
            try:
                marine_response = requests.get(marine_url, params={
                    "latitude": lat, "longitude": lon,
                    "hourly": ",".join(hourly_vars),
                    "forecast_days": forecast_days
                }, timeout=10)
                
                weather_response = requests.get(weather_url, params={
                    "latitude": lat, "longitude": lon,
                    "hourly": ",".join(wind_vars),
                    "forecast_days": forecast_days
                }, timeout=10)
                
                point_data = {
                    "lat": lat, "lon": lon,
                    "marine": marine_response.json() if marine_response.ok else {},
                    "weather": weather_response.json() if weather_response.ok else {}
                }
                
                save_to_cache(cache_key, point_data)
                all_data.append(point_data)
                
                if current % 5 == 0:
                    print(f"[Weather] Progress: {current}/{total_points}", flush=True)
                    
            except Exception as e:
                print(f"[Weather] Error at ({lat}, {lon}): {e}", flush=True)
                all_data.append({"lat": lat, "lon": lon, "marine": {}, "weather": {}})
    
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
    output_path: str = None
) -> xr.Dataset:
    """Create synthetic weather data for fast local development."""
    lats = np.arange(lat_min, lat_max + resolution, resolution)
    lons = np.arange(lon_min, lon_max + resolution, resolution)
    n_lats, n_lons = len(lats), len(lons)
    n_times = time_hours  # Use provided time range
    
    # Use numpy datetime64 for netCDF compatibility
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
    algorithm: Optional[str] = "isofuel"  # Options: "isofuel", "genetic", "gcrslider"
    weather_source: Optional[str] = None  # Deprecated - now controlled by weather_optimization flag
    time_forecast_hours: Optional[int] = Field(default=None, description="Forecast window in hours. If not provided, uses 168 for calm weather or 144 for real weather. Tip: use calm route time estimate to size this.")

class CalculatedRoute(BaseModel):
    waypoints: List[List[float]]
    total_distance_nm: float
    total_time_hours: float

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
    if not iso_string:
        dt = datetime.now(timezone.utc) + timedelta(hours=1)
    else:
        try:
            dt = datetime.fromisoformat(iso_string.replace('Z', '+00:00'))
        except:
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


def run_wrt_direct(config_dict: dict, output_dir: str) -> dict:
    """Run WRT directly using Python imports instead of subprocess."""
    import tempfile
    
    config_path = Path(tempfile.mktemp(suffix='.json'))
    try:
        with open(config_path, 'w') as f:
            json.dump(config_dict, f, indent=2)
        
        print(f"[WRT] Running with config: {config_path}", flush=True)
        print(f"[WRT] Algorithm: {config_dict.get('ALGORITHM_TYPE')}", flush=True)
        print(f"[WRT] Constraints: {config_dict.get('CONSTRAINTS_LIST')}", flush=True)
        
        config = Config.assign_config(config_path)
        execute_routing(config)
        
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
                    return {"success": True, "coords": coords}
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
    time_forecast_hours: Optional[int] = None
) -> dict:
    """
    Run a single WRT routing pass with specified weather source.
    Returns {"success": True, "coords": [...], "weather_cached": bool} or {"success": False, "error": "..."}
    
    Args:
        time_forecast_hours: Override for TIME_FORECAST. If None, uses defaults (168 for calm, 144 for real weather).
                            Tip: Use the calm route's estimated time to size this for real weather pass.
    """
    request_output_dir = f"{OUTPUT_DIR}/{pass_id}"
    Path(request_output_dir).mkdir(parents=True, exist_ok=True)
    
    # Determine time_forecast:
    # - If caller provided time_forecast_hours, use it
    # - Otherwise use defaults: 168 for calm, 144 for real weather
    if time_forecast_hours is not None:
        time_forecast = time_forecast_hours
        print(f"[Weather:{pass_id}] Using caller-provided TIME_FORECAST: {time_forecast} hours", flush=True)
    elif weather_source == "real":
        time_forecast = 144  # 6 days to leave margin for WRT's internal validation
    else:
        time_forecast = 168 if algorithm in ["genetic", "isofuel"] else 96
    
    weather_time_hours = time_forecast + 12  # Buffer
    
    # Generate weather data
    weather_cached = False
    if weather_source == "calm":
        print(f"[Weather:{pass_id}] Using calm synthetic data (no wind, 0.1m waves)", flush=True)
        weather_path = f"{DATA_DIR}/weather_{pass_id}.nc"
        create_synthetic_weather(lat_min, lat_max, lon_min, lon_max, 1.0, weather_time_hours, weather_path)
        weather_cached = True
    else:  # "real"
        print(f"[Weather:{pass_id}] Fetching real weather from OpenMeteo...", flush=True)
        try:
            # Fetch enough days to cover the forecast window
            forecast_days = max(7, (time_forecast // 24) + 1)
            weather_ds = fetch_marine_grid(lat_min, lat_max, lon_min, lon_max, resolution=1.0, forecast_days=forecast_days)
            weather_path = f"{DATA_DIR}/weather_{pass_id}.nc"
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
        # Use 'progress' for calm weather (rewards moving toward destination)
        # Use 'squareddist_over_disttodest' for real weather (exploits weather gradients)
        "ISOCHRONE_MINIMISATION_CRITERION": "progress" if weather_source == "calm" else "squareddist_over_disttodest",
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
        
        "WEATHER_DATA": weather_path,
        "DEPTH_DATA": depth_path,
        "ROUTE_PATH": request_output_dir,
        "COURSES_FILE": request_output_dir,
    }
    
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
        "weather_source": weather_source
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
    
    request_id = hashlib.md5(f"{request.start.lat}{request.start.lon}{request.end.lat}{request.end.lon}{time.time()}".encode()).hexdigest()[:8]
    
    # Algorithm selection (default: isofuel)
    algorithm = request.algorithm or "isofuel"
    if algorithm not in ["isofuel", "genetic", "gcrslider"]:
        return RouteResponse(
            success=False,
            error=f"Unknown algorithm: {algorithm}. Options: isofuel, genetic, gcrslider",
            execution_time_ms=(time.time() - start_time) * 1000
        )
    
    # Validate waypoint support
    algorithms_with_waypoint_support = ["isofuel"]
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
        time_forecast_hours=request.time_forecast_hours
    )
    
    if not calm_result.get("success"):
        return RouteResponse(
            success=False,
            error=f"Calm weather routing failed: {calm_result.get('error')}",
            execution_time_ms=(time.time() - start_time) * 1000
        )
    
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
        
        return RouteResponse(
            success=True,
            route=calm_route,
            optimized_route=None,  # No optimization requested
            provider="52north-local",
            execution_time_ms=exec_time,
            weather_cached=True,
            algorithm_used=algorithm,
            weather_source_used="calm"
        )
    
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
    
    real_result = await run_single_routing_pass(
        request=request,
        weather_source="real",
        pass_id=f"{request_id}_real",
        vessel=vessel,
        departure=departure,
        lat_min=lat_min, lat_max=lat_max,
        lon_min=lon_min, lon_max=lon_max,
        algorithm=algorithm,
        time_forecast_hours=real_weather_forecast_hours
    )
    
    if not real_result.get("success"):
        # If real weather fails, return calm route with error note
        exec_time = (time.time() - start_time) * 1000
        print(f"[Route] Real weather routing failed, returning calm route only", flush=True)
        return RouteResponse(
            success=True,
            route=calm_route,
            optimized_route=None,
            provider="52north-local",
            execution_time_ms=exec_time,
            weather_cached=True,
            algorithm_used=algorithm,
            weather_source_used="calm (real weather fetch failed)"
        )
    
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
    
    return RouteResponse(
        success=True,
        route=calm_route,              # Baseline with calm weather
        optimized_route=optimized_route,  # Optimized with real weather
        provider="52north-local",
        execution_time_ms=exec_time,
        weather_cached=real_result.get("weather_cached", False),
        algorithm_used=algorithm,
        weather_source_used="calm vs real"
    )


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
    print(f"""
╔══════════════════════════════════════════════════════════════╗
║  52North WeatherRoutingTool - Local Development Server       ║
╠══════════════════════════════════════════════════════════════╣
║  WRT Repo: {str(WRT_REPO_DIR):<47} ║
║  Data Dir: {DATA_DIR:<47} ║
║  PostGIS:  localhost:5433/gis_db                             ║
╠══════════════════════════════════════════════════════════════╣
║  Starting on http://localhost:8001                           ║
╚══════════════════════════════════════════════════════════════╝
""")
    uvicorn.run(app, host="0.0.0.0", port=8001, reload=False)
