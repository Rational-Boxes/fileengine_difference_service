# Back end for version comparisons

Plugin based comparison. Accepts a file identifier and version to
generate a comparison for. Difference between the specified version
and the previous one.

For 2D type data generate a scriptable SVG that can show old, new, and
the difference by color code. Red for deleted, green added, orange for
modified.

For 3D, likewise, a before and after with the option to show just the
change using boolean intersections.

## Primary 2D targets

### PDFs

Convert the PDFs to per-page SVG files with appropriate scriptable
hooks for the front end to show before, after, and color coded difference.

### 3D

Convert the two models into hugh-level geometry objects for the old,
new, and difference boolean intersection. THe three views are top
levels of the object tree so the user can select between them using
show/hide/x-ray mode.

Final file needs to be the Xeokit compressed format for the viewer.

## Service design

Implement behind a FastAPI service. Cache results for future requests.

Internally implement a generalized interface and format specific 
plug-ins for each supported format.