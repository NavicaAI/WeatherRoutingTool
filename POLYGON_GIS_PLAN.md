# Polygon GIS Land Detection - Implementation Plan

**Branch**: `feature/polygon-gis-land-detection`  
**Base**: `fix/gcrslider-land-crossing` (includes line-checking improvements)  
**Goal**: Replace raster-based `global_land_mask` with precise polygon geometry

## Problem Statement

Current `global_land_mask` limitations:
- Raster resolution: ~1km (misses narrow passages like Sant Antioco channel)
- No geometry precision for coastlines
- Cannot detect dynamic obstacles (restricted zones, ice fields)
- 30 land crossing points in Sant Antioco test case

## Solution Architecture

### 1. Abstraction Layer: `LandDetector` Base Class

```python
# WeatherRoutingTool/land_detection.py

from abc import ABC, abstractmethod
from typing import Tuple, Optional

class LandDetector(ABC):
    """Abstract base class for land detection strategies"""
    
    @abstractmethod
    def is_land(self, lat: float, lon: float) -> bool:
        """Check if point is on land"""
        pass
    
    @abstractmethod
    def crosses_land(self, lat1: float, lon1: float, 
                     lat2: float, lon2: float, 
                     check_interval_m: float = 500) -> bool:
        """Check if line segment crosses land"""
        pass
    
    @abstractmethod
    def get_name(self) -> str:
        """Return detector implementation name"""
        pass
```

### 2. Implementations

#### GlobalLandMaskDetector (Existing)
```python
class GlobalLandMaskDetector(LandDetector):
    """Wrapper for existing global_land_mask raster approach"""
    
    def is_land(self, lat: float, lon: float) -> bool:
        from global_land_mask import globe
        return globe.is_land(lat, lon)
```

#### PostGISDetector (New - Production Quality)
```python
class PostGISDetector(LandDetector):
    """Polygon-based detection using PostGIS + OSM land polygons"""
    
    def __init__(self, db_config: dict):
        self.connection = psycopg2.connect(**db_config)
        self.srid = 4326  # WGS84
    
    def is_land(self, lat: float, lon: float) -> bool:
        """Check point intersection with land polygons"""
        query = """
            SELECT EXISTS(
                SELECT 1 FROM land_polygons
                WHERE ST_Intersects(
                    geom,
                    ST_SetSRID(ST_MakePoint(%s, %s), 4326)
                )
            )
        """
        # Execute and return result
    
    def crosses_land(self, lat1: float, lon1: float,
                     lat2: float, lon2: float, 
                     check_interval_m: float = 500) -> bool:
        """Check line intersection with land polygons"""
        query = """
            SELECT EXISTS(
                SELECT 1 FROM land_polygons
                WHERE ST_Intersects(
                    geom,
                    ST_MakeLine(
                        ST_SetSRID(ST_MakePoint(%s, %s), 4326),
                        ST_SetSRID(ST_MakePoint(%s, %s), 4326)
                    )
                )
            )
        """
        # Execute and return result
```

### 3. Data Source

**OpenStreetMap Land Polygons**:
- Source: https://osmdata.openstreetmap.de/data/land-polygons.html
- Format: Shapefile (simplified-land-polygons-complete-3857.zip)
- Coverage: Global coastlines with high precision
- Size: ~500MB compressed, ~1.5GB uncompressed

**Import Process**:
```bash
# Download
wget https://osmdata.openstreetmap.de/download/land-polygons-split-4326.zip

# Import to PostGIS
ogr2ogr -f PostgreSQL \
  "PG:host=postgis port=5432 dbname=gis_db user=gis_user password=..." \
  land-polygons-split-4326/land_polygons.shp \
  -nln land_polygons \
  -nlt PROMOTE_TO_MULTI \
  -lco GEOMETRY_NAME=geom \
  -lco SPATIAL_INDEX=GIST

# Create spatial index (if not created automatically)
CREATE INDEX land_polygons_geom_idx ON land_polygons USING GIST(geom);
```

### 4. Configuration

Add to WRT config:
```yaml
land_detection:
  backend: "postgis"  # or "global_land_mask"
  
  postgis:
    host: "postgis"
    port: 5432
    database: "gis_db"
    user: "gis_user"
    password: "${GIS_DB_PASSWORD}"
    table: "land_polygons"
    
  # Fallback settings
  check_interval_m: 500
  cache_results: true
  cache_ttl_seconds: 3600
```

### 5. Integration with GcrSliderAlgorithm

```python
class GcrSliderAlgorithm(RoutingAlg):
    def __init__(self, ..., land_detector: Optional[LandDetector] = None):
        # Default to global_land_mask for backward compatibility
        self.land_detector = land_detector or GlobalLandMaskDetector()
    
    def is_land(self, lat: float, lon: float) -> bool:
        # Use injected detector instead of direct global_land_mask call
        return self.land_detector.is_land(lat, lon)
```

## Performance Optimizations

### 1. Spatial Indexing
- PostGIS GIST index on geometry column
- Query optimizer uses index for point/line intersections
- Sub-millisecond lookups for typical queries

### 2. Bounding Box Pre-filtering
```python
def crosses_land(self, lat1, lon1, lat2, lon2):
    # Quick bounding box check first
    bbox_query = """
        SELECT EXISTS(
            SELECT 1 FROM land_polygons
            WHERE geom && ST_MakeEnvelope(%s, %s, %s, %s, 4326)
        )
    """
    # Only do expensive intersection if bbox matches
```

### 3. Connection Pooling
```python
from psycopg2.pool import ThreadedConnectionPool

class PostGISDetector(LandDetector):
    _pool = None
    
    def __init__(self, db_config: dict, min_conn=1, max_conn=5):
        if PostGISDetector._pool is None:
            PostGISDetector._pool = ThreadedConnectionPool(
                min_conn, max_conn, **db_config
            )
```

### 4. Result Caching
```python
from functools import lru_cache

class CachedPostGISDetector(PostGISDetector):
    @lru_cache(maxsize=10000)
    def is_land(self, lat: float, lon: float) -> bool:
        return super().is_land(lat, lon)
```

## Testing Strategy

### 1. Unit Tests
- Test each detector implementation independently
- Mock PostGIS connection for unit tests
- Verify abstraction layer works with both backends

### 2. Integration Tests
- Sant Antioco route (current failure case)
- Strait of Gibraltar (narrow passage)
- Suez Canal (very narrow)
- Island chains (Greek islands, Caribbean)

### 3. Performance Benchmarks
```python
# Compare raster vs polygon performance
test_routes = [
    ("Sardinia", [(39.5, 8.0), (39.1982, 10.3370), (41.7385, 6.7887)]),
    ("Gibraltar", [...]),
    # ... more test cases
]

for name, waypoints in test_routes:
    # Test with GlobalLandMaskDetector
    # Test with PostGISDetector
    # Compare: accuracy, speed, land crossing count
```

## Migration Path

### Phase 1: Abstraction (This Branch)
- Create `LandDetector` base class
- Implement `GlobalLandMaskDetector` wrapper
- Refactor GcrSlider to use detector interface
- Backward compatible (no behavior change)

### Phase 2: PostGIS Implementation
- Implement `PostGISDetector`
- Add configuration system
- Import OSM polygon data
- Testing with both backends

### Phase 3: Production Deployment
- Docker Compose updates for PostGIS service
- Environment variable configuration
- Documentation and migration guide
- Optional: Hybrid mode (PostGIS primary, raster fallback)

## Success Metrics

- ✅ Sant Antioco: 30 land crossings → 0 land crossings
- ✅ Performance: <100ms per route (same as current)
- ✅ Accuracy: Sub-kilometer precision for coastlines
- ✅ Backward compatible: Existing code works without changes
- ✅ Configurable: Easy to switch between backends

## Dependencies

**New Python packages**:
- `psycopg2-binary>=2.9.0` (already in WRT requirements)
- `shapely>=2.0.0` (already in WRT requirements)

**Infrastructure**:
- PostGIS database (already in NavicaAI platform)
- OSM land polygon data (~1.5GB)

## Timeline Estimate

- Week 1: Abstraction layer + GlobalLandMaskDetector wrapper
- Week 2: PostGISDetector implementation + testing
- Week 3: Integration with GcrSlider + performance optimization
- Week 4: Documentation + deployment guide

## Open Questions

1. Should we support multiple polygon layers (land, restricted zones, ice)?
2. Do we need real-time polygon updates (chart corrections)?
3. Should PostGIS be required or optional dependency?
4. Hybrid mode: Use PostGIS near coast, raster for open ocean?

---

**Next Steps**: Start with Phase 1 abstraction to make this PR reviewable incrementally.
