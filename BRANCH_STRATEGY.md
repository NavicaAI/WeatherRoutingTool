# WeatherRoutingTool Branch Strategy

## Current Branches

### 1. `fix/gcrslider-land-crossing` (Ready for PR)
**Status**: ✅ Ready to submit to 52North  
**Base**: `main`  
**Commits**: 5 commits  
**Focus**: Line-checking optimization (incremental improvement)

**What it fixes**:
- Sardinia main island routing (56km → 0km land crossing)
- FIXME in buffer checking logic
- Missing segment verification

**What it doesn't fix**:
- Sant Antioco narrow channel (5km passage)
- Sub-kilometer coastline precision

**Submit this first** - demonstrates value, builds relationship with maintainers

### 2. `feature/polygon-gis-land-detection` (In Progress)
**Status**: 🚧 Planning/Implementation  
**Base**: `fix/gcrslider-land-crossing` (includes line fixes)  
**Commits**: 1 commit (plan document)  
**Focus**: Comprehensive polygon-based solution

**What it will fix**:
- Sant Antioco and all narrow passages
- Sub-kilometer precision for coastlines
- Dynamic obstacle detection (future)

**Submit second** - after line-checking PR is reviewed/merged

## Workflow

### Scenario 1: Line-checking PR accepted quickly
```bash
# 52North merges your PR
cd ~/Development/Navica/WeatherRoutingTool

# Update main branch
git checkout main
git pull upstream main

# Rebase polygon feature on updated main
git checkout feature/polygon-gis-land-detection
git rebase main

# Now polygon branch is based on merged changes
# Can be submitted as new PR
```

### Scenario 2: Line-checking PR needs revisions
```bash
# Make changes on fix/gcrslider-land-crossing branch
git checkout fix/gcrslider-land-crossing
# ... make changes ...
git add .
git commit -m "Address PR feedback: ..."
git push origin fix/gcrslider-land-crossing --force-with-lease

# Sync changes to polygon branch
git checkout feature/polygon-gis-land-detection
git rebase fix/gcrslider-land-crossing
```

### Scenario 3: Work on polygon while waiting for PR review
```bash
# Continue development on polygon branch
git checkout feature/polygon-gis-land-detection

# Create abstraction layer
# ... implement LandDetector ...
git add .
git commit -m "Add LandDetector abstraction layer"

# Implement PostGISDetector
# ... implement polygon detection ...
git add .
git commit -m "Implement PostGISDetector with OSM polygons"

# Line-checking branch remains unchanged for PR
```

## Commit Organization

### fix/gcrslider-land-crossing
```
31d652c Fix GCR Slider land crossing detection
4a9bb12 Add NavicaAI enhancements documentation
a158ac5 Add PR summary for GCR Slider land crossing fix
40c270e Optimize land crossing detection with balanced granularity
d462dee Update PR documentation for balanced optimization approach
```

### feature/polygon-gis-land-detection  
```
799c815 Add polygon GIS land detection implementation plan
[Future] Add LandDetector abstraction layer
[Future] Implement GlobalLandMaskDetector wrapper
[Future] Implement PostGISDetector
[Future] Add configuration system
[Future] Integration tests and benchmarks
```

## PR Strategy

### PR #1: Line-Checking Optimization
**Title**: "Fix GCR Slider land crossing detection"  
**Scope**: Focused, easy to review (~100 lines changed)  
**Value**: Immediate improvement for common cases  
**Timeline**: Submit now, review 1-2 weeks

### PR #2: Polygon GIS Detection
**Title**: "Add polygon-based land detection with PostGIS"  
**Scope**: Larger architectural change (~500+ lines)  
**Value**: Comprehensive solution for all edge cases  
**Timeline**: Submit after PR#1 review complete (4-6 weeks)

**Rationale for separation**:
- Easier for maintainers to review incrementally
- PR#1 provides value immediately while PR#2 is in progress
- If PR#1 has issues, PR#2 isn't blocked
- Demonstrates collaborative approach vs "big bang" changes

## Integration with NavicaAI Platform

Both branches already integrate via Docker:
- `docker-compose.dev.yml` uses local fork
- Can switch between branches by rebuilding container
- Test both solutions in production workloads

```bash
# Test line-checking optimization
cd ~/Development/Navica/WeatherRoutingTool
git checkout fix/gcrslider-land-crossing
cd ~/Development/Navica/NavicaAIPlatform
docker-compose -f docker-compose.dev.yml build --no-cache weather-routing

# Test polygon solution (once implemented)
cd ~/Development/Navica/WeatherRoutingTool
git checkout feature/polygon-gis-land-detection
cd ~/Development/Navica/NavicaAIPlatform
docker-compose -f docker-compose.dev.yml build --no-cache weather-routing
```

## Success Criteria

### PR #1 Accepted
- ✅ Merged into 52North main branch
- ✅ Sardinia test case passes
- ✅ No breaking changes
- ✅ Maintainers express interest in polygon solution

### PR #2 Accepted
- ✅ Merged into 52North main branch
- ✅ Sant Antioco test case passes (0 land crossings)
- ✅ Performance meets targets (<100ms)
- ✅ Backward compatible with PR#1 changes

---

**Current Action**: Submit PR#1, continue polygon development on feature branch
