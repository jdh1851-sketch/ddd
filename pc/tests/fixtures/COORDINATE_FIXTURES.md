The native_coordinate_zero_157.png and native_coordinate_zero_161.png fixtures
contain only the bottom numeric-coordinate ROI from two user-provided native
coordinate-window captures, pasted at its original location on a blank canvas.
Player names, the minimap contents, addresses and packet payloads are omitted.

They cover a pixel-preprocessing regression where baseline OCR produces X.0.
The implementation does not rewrite OCR strings: it retries two fixed binary
thresholds and requires exact agreement with a veto on conflicting valid reads.
These development samples do not establish general live OCR accuracy or movement
tolerances.
