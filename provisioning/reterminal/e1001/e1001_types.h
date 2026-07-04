#pragma once
// P4c.1 render model — one parsed sensor tile (native-ESPHome realization of the e1001_ui PanelModel).
// Kept deliberately tiny: the fetch lambda formats each sensor into (label, line) strings; the display
// lambda just lays them out. Full spec-driven formatting (units/precision from the catalog) comes in P4c.2.
#include <string>
#include <vector>

struct SensorTile {
  std::string label;   // room (or device_id fallback)
  std::string line;    // pre-formatted primary reading(s), e.g. "22.5 C   44 %"
};
