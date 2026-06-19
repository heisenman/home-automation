# ADR-0003 — Wasm Firmware Split (Foundational + Peripheral Modules)

**Date:** 2026-06-19  
**Status:** Accepted — Phase 8

## Decision

Firmware is split into two layers: (1) foundational C firmware (cable-flashed, trusted,
rarely changed) that hosts a WebAssembly runtime (WAMR primary, wasm3 fallback); and
(2) peripheral driver modules compiled to sandboxed Wasm, OTA-loadable.

## Context

Peripheral drivers need frequent updates as sensors are added. Updating foundational
firmware carries higher risk. The Wasm sandbox bounds the blast radius of a bad module.

## Consequences

- WAMR (~85 KB interpreter / ~50 KB AOT) or wasm3 embedded in ESP-IDF foundational firmware
- Peripheral modules can only call exported host-API functions (no credential/lock access)
- Bad peripheral module → misbehaves in sandbox; OTA a fix without re-flashing foundation
- RAM constraint: 64 KB Wasm pages on bare C6 — module memory must be sized tightly
- Phase 8 work; not relevant to Phases 1–4

## Tradeoffs accepted

- Native C would be faster/lower-power; Wasm overhead acceptable for non-hot-path peripheral logic
- Energy cost of Wasm on battery nodes needs measurement before committing to frequent-poll modules
- Most ambitious single piece of the build — one-time platform investment
