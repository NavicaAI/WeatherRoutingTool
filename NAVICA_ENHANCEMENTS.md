# NavicaAI Enhancements to 52North WeatherRoutingTool

## Overview

This fork contains bugfixes and enhancements to the 52North WeatherRoutingTool for improved maritime routing accuracy. All changes are designed to be contributed upstream via PRs.

## Changes Made

### 1. GCR Slider Land Crossing Fix (PR-Ready)

**Branch**: `fix/gcrslider-land-crossing`  
**Commit**: See latest commit  
**Status**: ✅ Ready for upstream PR

#### Problem

The GCR Slider algorithm had two critical issues causing routes to cross land:

1. **FIXME in `is_land()` method**: The buffer check only sampled points on a circle at buffer distance, missing narrow land features between the center point and buffer points.

2. **Missing segment verification**: After moving a waypoint orthogonally to avoid land, the algorithm didn't verify that the NEW segments (start→waypoint and waypoint→end) were actually land-free.

**Real-world impact**: Routes between Mediterranean islands (e.g., around Sardinia) would cut straight across 50+ km of land.

#### Solution

**Fix 1 - Enhanced Buffer Checking** (`is_land()` method, line ~254):
- Now checks intermediate points along the line from center to each buffer sample point
- Uses adaptive interval: `min(1000m, buffer_distance / 3)` to ensure at least 3 checks
- Prevents missing narrow peninsulas, isthmuses, and small islands

**Fix 2 - Segment Verification** (`split_segments()` method, line ~353):
- After finding a water point, explicitly verify both new segments are land-free
- Adds warning logging when segments still cross land (allows recursion to continue)
- Ensures the recursive algorithm eventually finds a valid path

#### Testing

Test the fix with this problematic route:
```python
# Route that previously crossed southern Sardinia
start = (39.5, 8.0)      # West of Sardinia
waypoint = (39.84, 11.16) # East of Sardinia  
end = (42.84, 6.69)       # Near Toulon, France

# With old code: Segment from (39.06, 8.27) to (39.01, 9.16) crossed 56km of land
# With fix: Route properly avoids all land masses
```

#### Files Changed

- `WeatherRoutingTool/algorithms/gcrslider/__init__.py`: 30 insertions, 2 deletions

---

### 2. Polygon GIS Land Detection (Planned)

**Branch**: `feature/polygon-gis-land-detection` (to be created)  
**Status**: 📋 Planned

#### Goals

1. Add configurable land detection backend system
2. Support PostGIS/PostgreSQL with polygon geometries (not just raster mask)
3. Enable dynamic obstacle avoidance (weather zones, restricted areas)
4. Maintain backward compatibility with `global_land_mask`

#### Design

```python
# New config options
LAND_DETECTION_BACKEND: Literal['global_land_mask', 'postgis'] = 'global_land_mask'
POSTGIS_CONNECTION_STRING: Optional[str] = None
POSTGIS_LAND_TABLE: str = 'land_polygons'
```

New file: `WeatherRoutingTool/land_detection.py`:
```python
class LandDetector(ABC):
    @abstractmethod
    def is_land(self, lat: float, lon: float) -> bool:
        pass

class GlobalLandMaskDetector(LandDetector):
    # Wraps existing global_land_mask

class PostGISDetector(LandDetector):
    # Queries PostGIS for point-in-polygon checks
    # Uses spatial indexes for performance
```

---

## Development Workflow

### Setting Up

```bash
# Clone and setup
git clone https://github.com/NavicaAI/WeatherRoutingTool.git
cd WeatherRoutingTool
git remote add upstream https://github.com/52North/WeatherRoutingTool.git

# Install dependencies
python -m venv venv
source venv/bin/activate  # or venv\Scripts\activate on Windows
pip install -e .
pip install -r requirements.test.txt
```

### Running Tests

```bash
# Run all tests
pytest

# Run specific test
pytest tests/test_gcrslider.py -v

# With coverage
pytest --cov=WeatherRoutingTool
```

### Creating a PR

```bash
# Ensure you're on your feature branch
git checkout fix/gcrslider-land-crossing

# Rebase on latest upstream
git fetch upstream
git rebase upstream/main

# Push to your fork
git push origin fix/gcrslider-land-crossing

# Open PR on GitHub: your-fork → 52North/WeatherRoutingTool
```

### Keeping Up to Date

```bash
# Sync main branch with upstream
git checkout main
git fetch upstream
git merge upstream/main
git push origin main

# Rebase feature branches
git checkout fix/gcrslider-land-crossing
git rebase main
```

---

## Integration with NavicaAI Platform

Our platform uses this fork via Docker:

```dockerfile
# Dockerfile in NavicaAI/NavicaAIPlatform/services/weather-routing
FROM python:3.11-slim
RUN git clone https://github.com/NavicaAI/WeatherRoutingTool.git /opt/wrt
RUN cd /opt/wrt && git checkout fix/gcrslider-land-crossing
RUN cd /opt/wrt && pip install -e .
```

Our FastAPI wrapper: `/services/weather-routing/app.py`
- Calls WRT via subprocess with JSON config
- Applies these config tweaks for better land avoidance:
  - `GCR_SLIDER_THRESHOLD`: 5000m (more detail around coastlines)
  - `GCR_SLIDER_LAND_BUFFER`: 1852m (1 nautical mile)
  - `GCR_SLIDER_DISTANCE_MOVE`: 2000m (smaller increments when moving points)

---

## Performance Considerations

**Buffer Checking Enhancement**:
- Adds ~3-5 extra land checks per buffer angle
- Default: 12 angles (360° / 30°) = ~36-60 extra checks per `is_land()` call
- Impact: Negligible (~5-10ms per waypoint evaluation) for vastly improved accuracy

**Segment Verification**:
- Adds 2 extra `has_point_on_land()` calls per moved waypoint
- Each checks a geodesic line at 1km intervals
- Impact: Worth it to prevent land-crossing routes

---

## Contact & Contributions

- **Maintained by**: NavicaAI Platform Engineering  
- **Issues**: https://github.com/NavicaAI/WeatherRoutingTool/issues  
- **Upstream**: https://github.com/52North/WeatherRoutingTool  
- **License**: Apache 2.0 (same as upstream)

All enhancements are offered to 52North as PRs. We welcome collaboration!

## Update: Additional Optimization Needed

### Testing Results (2026-01-07)

After implementing the initial fix (buffer/5, min 500m checks), testing revealed the Sant Antioco 
island case still shows 30 land crossing points on a 92km segment:

```
Sant Antioco route: (39.5, 8.0) → (38.838, 8.644)
Land crossings: 30 points between 43.5km-66km along segment
```

This indicates the algorithm needs **even finer granularity** for detecting narrow passages 
like the Sant Antioco channel (< 5km width).

### Recommended Next Steps

**Option 1: Ultra-fine line checking** (keeps PR focused)
- Reduce to `min(250m, buffer/10)` for 4x current granularity
- May impact performance but ensures comprehensive land detection

**Option 2: Polygon GIS database** (future enhancement)
- Use PostGIS with OSM land polygons for precise geometry checks
- Eliminates raster resolution limitations
- Better performance for complex coastlines
- Recommended for production deployment

