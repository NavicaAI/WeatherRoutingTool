# Polygon-Based Land Detection Setup Guide

## Overview

The Weather Routing Tool now supports high-resolution polygon-based land detection in addition to the traditional raster-based method. This significantly improves coastline accuracy, especially around islands and complex coastal geometries like Corsica, Sardinia, and the Mediterranean.

## Key Improvements

### Before (Raster Only)
- ❌ ~1.8km resolution (1 arc-minute)
- ❌ Blocky coastlines
- ❌ False positives near complex islands
- ❌ Misses narrow passages

### After (Raster + Polygons)
- ✅ High-resolution vector coastlines
- ✅ Accurate line-segment intersection testing
- ✅ Works across ALL algorithms (Isochrone, IsoFuel, Dijkstra, GCR Slider, Genetic)
- ✅ Graceful fallback to raster if database unavailable

## Architecture

### Dual Detection System

The tool now uses a **hybrid approach**:

1. **Discrete Checks (Raster)**: Fast point-based checks using `global_land_mask`
   - Used by: `LandCrossing` constraint
   - Checks: Individual waypoints
   - Speed: Very fast (~microseconds)

2. **Continuous Checks (Polygons)**: Accurate line-segment checks using PostGIS
   - Used by: `LandPolygonsCrossing` constraint
   - Checks: Line segments between waypoints
   - Speed: Fast with STRtree indexing (~milliseconds)

### Algorithm Integration

| Algorithm | Method | Polygon Support |
|-----------|--------|-----------------|
| **Isochrone/IsoFuel** | Uses `ConstraintsList.safe_crossing()` | ✅ Automatic via continuous checks |
| **GCR Slider** | Custom `has_point_on_land()` + `is_land()` | ✅ Direct polygon integration |
| **Dijkstra** | Graph edges filtered via `has_point_on_land()` | ✅ Enhanced with polygon checks |
| **Genetic** | Delegates to `ConstraintsList` | ✅ Automatic via continuous checks |

## Database Setup

### Required PostGIS Database

You need a PostgreSQL database with PostGIS extension and a `land_polygons` table.

#### Database Schema

```sql
CREATE TABLE public.land_polygons (
    id SERIAL PRIMARY KEY,
    wkb_geometry GEOMETRY(MULTIPOLYGON, 4326) NOT NULL
);

-- Create spatial index for performance
CREATE INDEX land_polygons_geom_idx 
ON public.land_polygons 
USING GIST (wkb_geometry);
```

#### Data Sources

Recommended sources for land polygon data:

1. **OpenStreetMap Land Polygons** (Recommended)
   - URL: https://osmdata.openstreetmap.de/data/land-polygons.html
   - Format: Shapefile
   - Coverage: Global
   - Resolution: Excellent for coastlines
   
2. **Natural Earth**
   - URL: https://www.naturalearthdata.com/
   - Scale: 1:10m (high detail)
   - Format: Shapefile
   
3. **GSHHG (Global Self-consistent Hierarchical High-resolution Geography)**
   - URL: https://www.soest.hawaii.edu/pwessel/gshhg/
   - Resolution: Very high
   - Format: Shapefile

#### Loading Data

```bash
# Example: Load OSM land polygons shapefile into PostGIS
ogr2ogr -f "PostgreSQL" \
  PG:"host=localhost port=5433 dbname=gis_db user=gis_user password=postgis_password" \
  land-polygons-split-4326/land_polygons.shp \
  -nln land_polygons \
  -overwrite \
  -lco GEOMETRY_NAME=wkb_geometry
```

## Environment Configuration

### Required Environment Variables

Create a `.env` file or export these variables:

```bash
# Database Connection
export WRT_DB_HOST=localhost
export WRT_DB_PORT=5433
export WRT_DB_DATABASE=gis_db
export WRT_DB_USERNAME=gis_user
export WRT_DB_PASSWORD=postgis_password

# Optional (defaults shown)
export POSTGRES_SCHEMA=public  # Default schema
```

### Docker Compose Example

If using Docker for PostGIS:

```yaml
version: '3.8'
services:
  postgis:
    image: postgis/postgis:15-3.3
    container_name: wrt_postgis
    environment:
      POSTGRES_DB: gis_db
      POSTGRES_USER: gis_user
      POSTGRES_PASSWORD: postgis_password
    ports:
      - "5433:5432"
    volumes:
      - postgis_data:/var/lib/postgresql/data
    
volumes:
  postgis_data:
```

## Configuration

### Minimal Config (Raster Only - Default)

```json
{
  "CONSTRAINTS_LIST": [
    "land_crossing_global_land_mask"
  ]
}
```

### Enhanced Config (Raster + Polygons - Recommended)

```json
{
  "CONSTRAINTS_LIST": [
    "land_crossing_global_land_mask",
    "land_crossing_polygons"
  ]
}
```

### Full Corsica Test Config

See `config.corsica_polygon_test.json` for a complete example.

## Usage

### Basic Usage

```python
from WeatherRoutingTool.config import Config
from WeatherRoutingTool.execute_routing import execute_routing

# Load config with both land detection methods
config = Config.from_json("config.corsica_polygon_test.json")

# Execute routing - polygon checks happen automatically
route, status = execute_routing(config)
```

### Verifying Polygon Detection

Check logs during initialization:

```
INFO - WRT.constraints: ContinuousCheck: Connected to database gis_db at localhost:5433
INFO - WRT.constraints: LandPolygonsCrossing: Loading land polygons for bbox: POLYGON((...)
INFO - WRT.constraints: LandPolygonsCrossing: Loaded 1523 land polygon features.
INFO - WRT.constraints: LandPolygonsCrossing: Successfully initialized polygon-based land detection.
```

### Troubleshooting

#### Database Connection Failed

```
WARNING - WRT.constraints: Database credentials not fully configured in environment variables.
```

**Solution**: Ensure all required environment variables are set.

#### No Polygons Loaded

```
WARNING - WRT.constraints: LandPolygonsCrossing: No land polygons found in the specified bounding box.
```

**Solution**: 
- Check your bounding box covers the desired area
- Verify `land_polygons` table contains data
- Check coordinate system (should be EPSG:4326)

#### Fallback to Raster

```
WARNING - WRT.constraints: LandPolygonsCrossing: Falling back - continuous land checks will be disabled.
```

**Solution**: This is expected behavior if database is unavailable. Routing will continue with raster-only detection.

## Performance Considerations

### Memory Usage

- **Raster**: ~900MB for global mask (loaded once)
- **Polygons**: Depends on bounding box size
  - Small region (Corsica): ~50MB
  - Large region (Mediterranean): ~500MB

### Query Optimization

The tool automatically:
- Queries only polygons within the routing bounding box
- Uses STRtree spatial indexing for O(log n) lookups
- Caches polygon data for the session

### Execution Time

For a typical Corsica route (50-100 waypoints):
- **Raster only**: ~10-20 seconds
- **Raster + Polygons**: ~15-30 seconds
- **Improvement**: Significantly fewer false land crossings

## Testing

### Quick Test

```bash
# Set environment variables
source .env

# Run Corsica test
python cli.py --config config.corsica_polygon_test.json

# Check for successful polygon initialization in logs
```

### Verify Land Detection

```python
from WeatherRoutingTool.constraints.constraints import LandPolygonsCrossing
from WeatherRoutingTool.utils.maps import Map

# Initialize detector
map_size = Map(41.0, 8.0, 43.5, 10.0)  # Corsica
detector = LandPolygonsCrossing(map_size=map_size)

# Check a segment
import numpy as np
lat_start = np.array([41.5])
lon_start = np.array([8.5])
lat_end = np.array([41.7])
lon_end = np.array([8.3])

crosses_land = detector.check_crossing(lat_start, lon_start, lat_end, lon_end)
print(f"Crosses land: {crosses_land[0]}")
```

## Migration Guide

### Existing Projects

1. **Add environment variables** for database connection
2. **Update config** to include `land_crossing_polygons`
3. **Test** with existing routes to compare results
4. **No code changes needed** - all algorithms automatically use new system

### Backwards Compatibility

- ✅ Old configs without `land_crossing_polygons` still work
- ✅ Graceful degradation if database unavailable
- ✅ No breaking changes to API

## Algorithm-Specific Notes

### GCR Slider

Additional config option:
```json
{
  "GCR_SLIDER_USE_POLYGON_LAND_DETECTION": true
}
```

This enables polygon checks in the algorithm's custom land detection methods.

### Dijkstra

Polygon detection enhances the graph construction phase. Edges that cross land are not added to the graph, ensuring cleaner routes.

### Isochrone/IsoFuel

Continuous checks are enabled by default when `land_crossing_polygons` is in `CONSTRAINTS_LIST`. No additional configuration needed.

## Support

For issues or questions:
1. Check logs for initialization messages
2. Verify database connection with `psql`
3. Ensure land_polygons table has data in your region
4. Review this documentation

## Future Enhancements

Potential improvements:
- [ ] Distance-from-coast buffer using polygon distance calculations
- [ ] Multi-resolution polygon loading (coarse for open ocean, fine for coast)
- [ ] Caching of commonly-used regions
- [ ] Integration with additional GIS layers (depth, restricted zones)
