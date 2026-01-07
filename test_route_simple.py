#!/usr/bin/env python3
"""Simple test for the problematic Sardinia segment."""
from global_land_mask import is_land as is_land_global_land_mask
from geographiclib.geodesic import Geodesic
import math

geod = Geodesic.WGS84

# The problematic segment from our actual route
start = (39.0635, 8.2701)
end = (39.0100, 9.1634)

line = geod.InverseLine(start[0], start[1], end[0], end[1])
print(f"Testing segment: {start} → {end}")
print(f"Distance: {line.s13/1000:.1f} km")

# Check at 1km intervals
interval = 1000
n = int(math.ceil(line.s13 / interval))
land_count = 0

for i in range(0, n + 1):
    s = min(interval * i, line.s13)
    g = line.Position(s, Geodesic.STANDARD | Geodesic.LONG_UNROLL)
    if is_land_global_land_mask(g['lat2'], g['lon2']):
        land_count += 1

print(f"Land crossing points: {land_count}/{n+1}")
print(f"Approximate land crossed: ~{land_count} km")

if land_count > 0:
    print("\n❌ This segment STILL crosses land!")
    print("   Note: The fix prevents NEW routes from using this segment,")
    print("   but this particular segment in the output is from before the fix.")
else:
    print("\n✅ This segment is clear!")
