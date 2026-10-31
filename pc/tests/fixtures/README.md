# Gauge replay fixtures

`fishing_gauges.npz` contains only boolean green-pixel masks from cropped gauge regions, plus original presentation timestamps, from the user-provided fishing samples ending in `205445319` and `205626241`. No full gameplay frames, chat or player names are included.

The masks preserve the gauge's initial appearance, descent and rebound. Replay tests reconstruct green-on-black crops to test the same contour detector and controller. Message classification is supplied separately in these replay tests; they do not claim end-to-end OCR verification.
