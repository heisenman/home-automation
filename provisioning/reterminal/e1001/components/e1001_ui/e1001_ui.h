// e1001_ui — ESPHome external component: local renderer of the shared BFF spec on the UC8179 ePaper.
// Implements ADR-0019 tier (b): fetch the server-authored spec, render LOCALLY. NOT a server-rasterized frame.
// Design + rationale: docs/design/e1001-epaper-renderer.md. Contract source: server/api/viewmodel.py.
//
// Decomposition (4 seams): spec_client (JSON -> PanelModel) | tiles (draw prims) | layout (model -> placements)
// | refresh (ePaper slow-refresh policy). The PanelModel + tile interface below are dependency-light on purpose
// so spec_client parse + layout geometry are HOST-testable (test/, plain cc) with no ePaper/ESPHome needed.
#pragma once
#include <string>
#include <vector>
#include <cstdint>

namespace e1001_ui {

// ── The consumed spec (mirrors server/api/viewmodel.py; only the fields the mono panel uses) ──
// metric_spec = {key,label,unit,color,precision,graph}; the panel ignores `color` (1-bit) but honors the rest.
struct MetricSpec {
  std::string key;         // "temperature_c"
  std::string label;       // "Temperature"
  std::string unit;        // "°C"   (glyphs ° µ ³ ² ₂ % must be in the baked font)
  int precision = 1;       // decimal places for the value
};

struct MetricValue { std::string key; double value = 0; };  // resolved against MetricSpec at draw time

// One device from GET /api/v1/sensors -> sensors[]
struct Sensor {
  std::string device_id;
  std::string name;        // overlay name or device_id
  std::string area;        // room
  double age_s = 0;        // staleness (drives "unreachable")
  std::vector<MetricValue> metrics;  // in METRIC_CATALOG order (server-authored ordering preserved)
};

// build_alerts(...) -> already severity-sorted (critical|warning|info)
enum class Severity { kCritical, kWarning, kInfo };
struct Alert { Severity severity; std::string name; std::string detail; };

// The whole render input, produced by spec_client from one snapshot fetch.
struct PanelModel {
  std::vector<MetricSpec> catalog;   // top-level `metrics` from /api/v1/sensors
  std::vector<Sensor>     sensors;
  std::vector<Alert>      alerts;
  std::string             scene;     // active house scene (GET /api/v1/house)
  int  battery_pct = -1;             // -1 = unknown (footer)
  bool online = false;               // last snapshot fetch succeeded
};

// ── Geometry ──
struct Rect { int x, y, w, h; };

// ── Tile primitives (seam 2): fixed library, each fills its given rect on the 1-bit buffer. ──
// The Display* is templated at the call site to the ESPHome display buffer; host tests pass a stub recorder.
enum class TileKind { kAlertBanner, kSceneHeader, kSensorTile, kStatusFooter };

// ── Layout (seam 3): PanelModel + panel geometry -> ordered placements. v1 = fixed status layout. ──
struct Placement { TileKind kind; Rect rect; int data_index; };  // data_index -> sensors[]/alerts[] as applicable
std::vector<Placement> layout_status_v1(const PanelModel& m, int width, int height);

// ── spec_client (seam 1): parse a /api/v1/sensors (+ /house) JSON snapshot into a PanelModel. Pure. ──
// Returns false on malformed input (PanelModel.online stays false). Host-tested against test/sensors.fixture.json.
bool parse_sensors(const std::string& sensors_json, PanelModel& out);
bool parse_house(const std::string& house_json, PanelModel& inout);   // fills `scene`

}  // namespace e1001_ui
