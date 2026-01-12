# Quick Start: Polygon-Based Land Detection

## 30-Second Setup

```bash
# 1. Set environment variables
export WRT_DB_HOST=localhost
export WRT_DB_PORT=5433
export WRT_DB_DATABASE=gis_db
export WRT_DB_USERNAME=gis_user
export WRT_DB_PASSWORD=postgis_password

# 2. Test setup
python test_polygon_land_detection.py

# 3. Run with polygon detection
python cli.py --config config.corsica_polygon_test.json
```

## What Changed?

✅ **All algorithms now support polygon-based land detection**
- Isochrone/IsoFuel: Automatic via constraints ✅
- GCR Slider: Already implemented ✅  
- Dijkstra: Enhanced with polygons ✅
- Genetic: Automatic via constraints ✅

## Enable Polygon Detection

Add to your config JSON:
```json
{
  "CONSTRAINTS_LIST": [
    "land_crossing_global_land_mask",
    "land_crossing_polygons"
  ]
}
```

## How It Works

**Old Way (Raster Only)**:
- ~1.8km resolution
- Point checks only
- Misses narrow passages
- False positives near complex coasts

**New Way (Raster + Polygons)**:
- High-resolution vector coastlines
- Line segment intersection tests
- Accurate for complex geometries
- Hybrid approach: raster for speed, polygons for accuracy

## Database Requirement

Required table in PostGIS:
```sql
public.land_polygons (
    id SERIAL,
    wkb_geometry GEOMETRY(MULTIPOLYGON, 4326)
)
```

Data sources:
- OpenStreetMap Land Polygons (recommended)
- Natural Earth 1:10m
- GSHHG high-resolution

## Verification

Success indicators in logs:
```
INFO - ContinuousCheck: Connected to database gis_db at localhost:5433
INFO - LandPolygonsCrossing: Loaded 1523 land polygon features.
INFO - LandPolygonsCrossing: Successfully initialized
```

## Troubleshooting

| Issue | Solution |
|-------|----------|
| "Database credentials not configured" | Set WRT_DB_* environment variables |
| "No land polygons found" | Check land_polygons table has data |
| "Connection failed" | Verify PostgreSQL is running on port 5433 |
| Routes still cross land | Check CONSTRAINTS_LIST includes both constraints |

## Files

- `POLYGON_INTEGRATION_SUMMARY.md` - Full implementation details
- `POLYGON_LAND_DETECTION_GUIDE.md` - Complete documentation
- `test_polygon_land_detection.py` - Validation script
- `config.corsica_polygon_test.json` - Example config
- `.env.template` - Environment variable template

## Benefits

For your Corsica testing:
- ✅ Accurate island navigation
- ✅ Proper narrow passage detection  
- ✅ Reduced false positives
- ✅ Works across all algorithms
- ✅ Backwards compatible

## Next Steps

1. Adjust route in `config.corsica_polygon_test.json`
2. Run test: `python test_polygon_land_detection.py`
3. Run routing: `python cli.py --config config.corsica_polygon_test.json`
4. Compare before/after results

**Note**: System gracefully falls back to raster-only if database unavailable. Your existing configs continue to work unchanged.
