# Quick Testing Guide - GCR Slider Polygon Land Detection

## Prerequisites

### For Raster Mode (Always Works)
- No prerequisites needed
- Uses global_land_mask package (already installed)

### For Polygon Mode (Optional)
- PostGIS database configured
- Database connection in config: `config.engine`
- Table `land_polygons` with `wkb_geometry` column
- Shapely and geopandas installed

## Configuration

### Test Raster Mode (Baseline)
```python
config = {
    "START": [39.5, 8.0],
    "FINISH": [42.84, 6.69],
    "INTERMEDIATE_WAYPOINTS": [[39.84, 11.16]],
    "GCR_SLIDER_USE_POLYGON_LAND_DETECTION": False,  # ← Raster mode
    # ... other GCR_SLIDER settings ...
}
```

### Test Polygon Mode (Requires PostGIS)
```python
config = {
    "START": [39.5, 8.0],
    "FINISH": [42.84, 6.69],
    "INTERMEDIATE_WAYPOINTS": [[39.84, 11.16]],
    "GCR_SLIDER_USE_POLYGON_LAND_DETECTION": True,  # ← Polygon mode
    # ... other GCR_SLIDER settings ...
}
```

## Expected Behavior

### Initialization Logs

#### With Raster Mode
```
INFO - Polygon-based land detection disabled (using raster)
```

#### With Polygon Mode (PostGIS Available)
```
INFO - Polygon-based land detection enabled.
```

#### With Polygon Mode (PostGIS Unavailable)
```
WARNING - Failed to initialize polygon land detection: [error]. Falling back to raster.
```

## Test Scenarios

### Scenario 1: Simple Route (No Land Crossing)
```python
START: [40.0, 0.0]   # Atlantic Ocean
FINISH: [41.0, 1.0]  # Atlantic Ocean
```
**Expected**: No land crossings in both modes

### Scenario 2: Sardinia Route (Known Issue)
```python
START: [39.5, 8.0]        # West of Sardinia
WAYPOINT: [39.84, 11.16]  # East of Sardinia
FINISH: [42.84, 6.69]     # North of Sardinia
```
**Expected with Raster**: ~118 false positive land crossings
**Expected with Polygons**: ~0 land crossings (accurate)

### Scenario 3: Gibraltar Strait (Narrow Passage)
```python
START: [35.5, -6.0]   # Atlantic side
FINISH: [36.0, -5.0]  # Mediterranean side
```
**Expected**: Both modes should detect narrow strait, but polygon mode more accurate

## Debugging

### Check Polygon Detection Status
```python
from WeatherRoutingTool.algorithms.gcrslider import GcrSliderAlgorithm

algorithm = GcrSliderAlgorithm(config)

print(f"Polygon detection enabled: {algorithm.use_polygon_detection}")
print(f"Detector initialized: {algorithm.land_polygon_detector is not None}")

if algorithm.land_polygon_detector:
    print(f"Map bounds: {algorithm._calculate_map_bounds()}")
```

### Check Land Detection for Specific Point
```python
# Test a known land point (Rome, Italy)
is_on_land = algorithm.is_land(41.9028, 12.4964)
print(f"Rome on land: {is_on_land}")  # Should be True

# Test a known water point (Mediterranean)
is_on_water = algorithm.is_land(40.0, 15.0)
print(f"Mediterranean on land: {is_on_water}")  # Should be False
```

### Check Line Segment
```python
from geographiclib.geodesic import Geodesic

geod = Geodesic.WGS84
line = geod.InverseLine(39.5, 8.0, 39.84, 11.16)

crosses_land = algorithm.has_point_on_land(line)
print(f"Line crosses land: {crosses_land}")
```

## Performance Comparison

### Metrics to Measure
1. **Execution Time**: Time to generate route
2. **Point Count**: Number of waypoints in final route
3. **Land Crossings**: Number of detected land crossings
4. **Accuracy**: Visual inspection of route on map

### Example Measurement
```python
import time

# Raster mode
start_time = time.time()
route_raster, _ = algorithm_raster.execute()
raster_time = time.time() - start_time

# Polygon mode
start_time = time.time()
route_polygon, _ = algorithm_polygon.execute()
polygon_time = time.time() - start_time

print(f"Raster mode: {raster_time:.2f}s, {len(route_raster.lats_per_step)} points")
print(f"Polygon mode: {polygon_time:.2f}s, {len(route_polygon.lats_per_step)} points")
```

## Troubleshooting

### Issue: "Polygon detection not enabled"
**Solution**: Check config has `GCR_SLIDER_USE_POLYGON_LAND_DETECTION = True`

### Issue: "Failed to initialize polygon land detection"
**Causes**:
- PostGIS database not configured
- No `engine` attribute in config
- `land_polygons` table missing
- Database connection failed

**Solution**: 
1. Ensure PostGIS is running
2. Verify database credentials
3. Check `land_polygons` table exists
4. Or use raster mode as fallback

### Issue: "Polygon point check failed"
**Causes**:
- Malformed geometry
- Database query timeout
- Invalid coordinates

**Solution**: Check logs for specific error, algorithm will fallback to raster

## Validation Checklist

- [ ] Raster mode works (baseline)
- [ ] Polygon mode initializes with PostGIS
- [ ] Polygon mode falls back without PostGIS
- [ ] Routes generated match expectations
- [ ] No syntax or runtime errors
- [ ] Logs show correct mode being used
- [ ] Performance acceptable
- [ ] Sardinia test case shows improvement

## Next Steps After Testing

1. Document results in PR
2. Add automated tests
3. Update user documentation
4. Consider adding config validation
5. Add performance benchmarks
