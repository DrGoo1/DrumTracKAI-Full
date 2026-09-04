# TracKAI Shared Platform

DrumTracKAI is the first instrument implementation of a shared TracKAI research, calibration, and production platform. New instruments reuse provenance, reviewer identity, artifacts, jobs, calibration workflow, model/version governance, and StudioMind contracts rather than creating separate admin applications.

## Instrument modules

- `drums` / DrumTracKAI — existing production and calibration implementation; current database and API fields remain backward compatible.
- `bass` / BassTracKAI — registered platform module with bass-specific conditioning and calibration semantics; generation/calibration execution remains disabled until source assimilation and a production model are provisioned.

Future GuitarTracKAI, KeyboardTracKAI, HornTracKAI, and VocalTracKAI modules should extend the same registry and shared services.

## Shared administration

The existing calibration admin remains available at `/calibration-admin`. `/trackai-admin` is the shared-platform alias. The goal is one research/admin console with an instrument selector, not one admin deployment per instrument.

Shared concerns include source/provenance intake, extraction status, artifact inspection, reviewer administration, job queues, model/version registry, calibration evidence, quality gates, promotion state, audit history, and StudioMind integration.

## Compatibility rule

Existing drum wire/storage contracts such as `target_drummer_slug`, `drummer_slug`, `kit_balance`, and `fill_behavior` are not renamed in place. Generic instrument semantics are layered above them, and migrations must be additive. This avoids breaking the existing calibration database, reviewer clients, or production deployment.

## BassTracKAI foundation

BassTracKAI conditions on song structure plus harmony and rhythm-section context. Initial required inputs are tempo/meter, section map, chord map, kick events, drum groove, vocal melody where available, and existing instruments.

Its calibration rubric adds kick-lock relationship, note-length behavior, harmonic accuracy, and bass articulation to the shared authenticity, groove, dynamics, phrasing, human-realism, and usefulness dimensions.

## Authority

Registering an instrument or exposing its rubric does not authorize generation or model promotion. BassTracKAI remains non-executable until exact provider/model identity, source provenance, artifact persistence, calibration readiness, and human-reviewed quality gates are satisfied.
