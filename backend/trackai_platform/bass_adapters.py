"""Adapters feeding canonical BassNoteEvent records from MIDI or analyzed audio."""
from __future__ import annotations
from pathlib import Path
from typing import Iterable, Mapping
from .bass_features import BassNoteEvent, normalize_audio_events, normalize_midi_notes

def midi_file_to_note_events(path: str | Path) -> tuple[BassNoteEvent, ...]:
    try:
        import mido
    except ImportError as exc:
        raise RuntimeError("mido is required for BassTracKAI MIDI ingestion") from exc
    mid=mido.MidiFile(str(path))
    tempo=500000
    absolute=0.0
    active: dict[tuple[int,int], tuple[float,int]]={}
    notes=[]
    for msg in mido.merge_tracks(mid.tracks):
        absolute += mido.tick2second(msg.time, mid.ticks_per_beat, tempo)
        if msg.type == "set_tempo":
            tempo=msg.tempo
            continue
        if msg.type == "note_on" and msg.velocity>0:
            active[(getattr(msg,"channel",0),msg.note)]=(absolute,msg.velocity)
        elif msg.type in {"note_off","note_on"}:
            key=(getattr(msg,"channel",0),msg.note)
            start=active.pop(key,None)
            if start is not None:
                onset,velocity=start
                notes.append({"onset_sec":onset,"duration_sec":max(0.001,absolute-onset),"midi_note":msg.note,"velocity":velocity/127.0,"articulation":"normal"})
    return normalize_midi_notes(notes)

def analyzed_audio_to_note_events(events: Iterable[Mapping[str, object]]) -> tuple[BassNoteEvent, ...]:
    return normalize_audio_events(events)
