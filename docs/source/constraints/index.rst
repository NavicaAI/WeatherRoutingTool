.. constraints:

Constraints
===========

The constraints module
----------------------

The input parameters
--------------------

.. figure:: /_static/constraint_arguments.png
   :alt: constraint_arguments
   :width: 400
   :align: center

   Fig.4: Figure for illustrating the concept of passing the information on the routing segments that are to be checked by the constraint module to the respective function. Variable names printed in orange correspond to the naming scheme for the first routing step while variables printed in blue correspond to the naming scheme for the second routing step.

As described above [ToDo], the constraint module can be used to check constraints for a complete routing segment. Thereby, several routing segments can be processed in only one request. This means that for the genetic algorithm, only one request needs to be performed for every route that is considered in a single generation and for the isofuel algorithm, only one request needs to be performed for every single routing step. This implementation minimises computation time and is achieved by passing arrays of latitudes and longitudes to the constraint module i.e. if the constraint module is called like this

.. code-block:: python

   safe_crossing(lat_start, lat_end, lon_start, lon_end)


then, the arguments ``lat_start``, ``lat_end``, ``lon_start`` and ``lon_end`` correspond to arrays for which every element characterises a different routing segment. Thus the length of the arrays is equal to the number of routing segments that are to be checked. While for the genetic algorithm, the separation of a closed route into different routing segments is rather simple, the separation for the isofuel algorithm is more complex. This is, why the passing of the latitudes and longitudes shall be explained in more detail for the isofuel algorithm in the following.

Let's consider only two routing steps of the form that is sketched in Fig. XXX. The parameters that are passed to the constraints module for the first routing step are the latitudes and longitudes of start and end points for the routing segments `a` to `e` which are

- :math:`lat\_start = (lat\_start_{abcde}, lat\_start_{abcde}, lat\_start_{abcde}, lat\_start_{abcde}, lat\_start_{abcde})`
- :math:`lat\_end = (lat\_end_{a}, lat\_end_{b}, lat\_end_{c}, lat\_end_{d}, lat\_end_{e})`
- :math:`lon\_start = (lon\_start_{abcde}, lon\_start_{abcde}, lon\_start_{abcde}, lon\_start_{abcde}, lon\_start_{abcde})`
- :math:`lon\_end = (lon\_end_{a}, lon\_end_{b}, lon\_end_{c}, lon\_end_{d}, lon\_end_{e})`

i.e. since the start coordinates are matching for all routing segments, the elements for the start latitudes and longitudes are all the same.<br>
The arguments that are passed for the second routing step are the start and end coordinates of the routing segments :math:`\alpha` to :math:`\zeta`:

- :math:`lat\_start = (lat\_start_{\alpha\beta\gamma}, lat\_start_{\alpha\beta\gamma}, lat\_start_{\alpha\beta\gamma}, lat\_start_{\delta\epsilon\zeta}, lat\_start_{\delta\epsilon\zeta}, lat\_start_{\delta\epsilon\zeta})`
- :math:`lat\_end = (lat\_end_{\alpha}, lat\_end_{\beta}, lat\_end_{\gamma}, lat\_end_{\delta}, lat\_end_{\epsilon}, lat\_end_{\zeta})`
- :math:`lon\_start = (lon\_start_{\alpha\beta\gamma}, lon\_start_{\alpha\beta\gamma}, lon\_start_{\alpha\beta\gamma}, lon\_start_{\delta\epsilon\zeta}, lon\_start_{\delta\epsilon\zeta},lon\_start_{\delta\epsilon\zeta})`
- :math:`lon\_end =  (lon\_end_{\alpha}, lon\_end_{\beta}, lon\_end_{\gamma}, lon\_end_{\delta}, lon\_end_{\epsilon}, lon\_end_{\zeta})`

i.e. the latitudes of the end points from the first routing step are now the start coordinates of the current routing step. In contrast to the first routing step, the start coordinates of the second routing step differ for several route segments.

Route Postprocessing
--------------------

When the optional config variable ``ROUTE_POSTPROCESSING`` is enabled, the route is forwarded for postprocessing to follow Traffic Separation Scheme (TSS) rules.
Pgsnapshot schema with Osmosis were used to import OpenSeaMap data into the PostGIS+PostgreSQL database to retrieve TSS related data. The key OpenSeaMap TSS tags considered for route postprocessing are ``inshore_traffic_zone``, ``separation_boundary``, ``separation_lane``, ``separation_boundary`` and ``separation_line``.
The primary TSS rules have been addressed in the current development phase are:
1. If the current route is crossing any Inshore Traffic Zone or other TTS element, then the route should enter and leave the nearest separation lane which is heading to the direction of destination.

.. figure:: /_static/follow_separation_lane.png
   :alt: follow_separation_lane
   :width: 400
   :align: center

2. If the current route is intersecting the Traffic Separation Lanes and the angle between the route nodes before the intersection and after the intersection is between 60° to 120°, the new route segment is introduced as it is perpendicular to the separation lane and extends towards the last route segment, perpendicularly.

.. figure:: /_static/right_angle_crossing.png
   :alt: right_angle_crossing
   :width: 400
   :align: center

Furthermore, if the starting node or the ending node is located inside a traffic separation zone, route postprocessing is not further executed.

Land Crossing Detection
-----------------------

The WRT provides two methods for detecting land crossings, which can be used individually or in combination:

**Raster-based detection** (``land_crossing_global_land_mask``):

Uses the `global-land-mask <https://github.com/toddkarin/global-land-mask>`_ library which provides a raster grid at approximately 1.8 km resolution. This method is fast and requires no external dependencies, but may miss narrow passages or produce false positives near complex coastlines.

**Polygon-based detection** (``land_crossing_polygons``):

Uses high-resolution vector coastline data stored in a PostGIS database. This method performs line segment intersection tests against land polygons, providing accurate detection even for complex geometries such as islands, narrow straits, and fjords.

The recommended approach is to use both constraints together:

.. code-block:: json

   {
     "CONSTRAINTS_LIST": [
       "land_crossing_global_land_mask",
       "land_crossing_polygons"
     ]
   }

This hybrid approach uses the raster method for initial fast filtering and the polygon method for accurate validation of segments near coastlines.

**Database requirements for polygon detection:**

The polygon-based method requires a PostGIS database with a ``land_polygons`` table containing coastline geometry data. The expected schema is:

.. code-block:: sql

   CREATE TABLE public.land_polygons (
       gid SERIAL PRIMARY KEY,
       wkb_geometry GEOMETRY(MULTIPOLYGON, 4326)
   );
   CREATE INDEX ON land_polygons USING GIST (wkb_geometry);

Recommended data sources:

* `OpenStreetMap Land Polygons <https://osmdata.openstreetmap.de/data/land-polygons.html>`_ (recommended, updated weekly)
* `Natural Earth 1:10m <https://www.naturalearthdata.com/downloads/10m-physical-vectors/>`_
* `GSHHG high-resolution <https://www.soest.hawaii.edu/pwessel/gshhg/>`_

The database connection is configured via environment variables (see :ref:`configuration`). If the database is unavailable, the system gracefully falls back to raster-only detection.

Useful links:
-------------

* https://en.wikipedia.org/wiki/Traffic_separation_scheme
* https://wiki.openstreetmap.org/wiki/Seamarks/Seamark_Objects
* Szlapczynski, Rafal. (2012). Evolutionary approach to ship's trajectory planning within Traffic Separation Schemes. Polish Maritime Research. 19. DOI: `10.2478/v10012-012-0002-x <https://www.researchgate.net/publication/271052992_Evolutionary_approach_to_ship's_trajectory_planning_within_Traffic_Separation_Schemes>`_