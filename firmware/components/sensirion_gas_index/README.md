# sensirion_gas_index — VOC/NOx Gas Index Algorithm (vendored, ADR-0020)

Sensirion's official **Gas Index Algorithm** — converts a raw SGPxx gas signal (SRAW) into a
comparable **0..500 index** (VOC or NOx; 100 = VOC baseline). Adaptive: it learns the local baseline
over hours, so the index means "relative to normal here," not an absolute concentration.

**Vendored verbatim** from <https://github.com/Sensirion/gas-index-algorithm> (BSD-3-Clause; see
`LICENSE`). Kept in the upstream flat layout (`.c`/`.h` at the component root) so it can be re-synced
from upstream without reshuffling — the one deliberate exception to the `include/` component convention.
Do not hand-edit the algorithm sources; update by re-fetching a tagged release.

## Usage
```c
GasIndexAlgorithmParams voc;
GasIndexAlgorithm_init(&voc, GasIndexAlgorithm_ALGORITHM_TYPE_VOC);
// every 1.0 s:
int32_t index;
GasIndexAlgorithm_process(&voc, sraw, &index);   // index: 0 during ~45 s warm-up, then 1..500
```
Contract: a **fixed 1 Hz** sampling cadence (the default sampling interval). Optional
`GasIndexAlgorithm_get_states()/set_states()` let a node persist the learned baseline across reboots.

## Platform support
Pure fixed-/floating-point math, no I/O — any target. Pair with a sensor driver such as
[`sgp40`](../sgp40/) for the SRAW input.

## Consumed by
- `c6-bench` edge node via [`ha_gas`](../../edge/esp32c6/main/ha_gas.c).
