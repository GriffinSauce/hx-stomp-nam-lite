#!/usr/bin/env python3
"""record_gap.py - measure the HX Stomp's preset-change gap over USB, repeatably.

Plays a steady tone into the Stomp over USB audio, records the Stomp's USB
output, and sends MIDI program and snapshot changes at scheduled times, then
reports the fade-out, gap and fade-in around each event (measure_gap.py's
analysis). The Stomp must be connected over USB; nothing else is needed.

    python3 tools/research/record_gap.py --out-pair 3 \\
        --events "1.0:pc1,2.5:pc0,4.0:cc69=1,5.0:cc69=2,6.0:cc69=0" --wav gap.wav

--out-pair selects the USB channels the tone goes to: 1 = USB 1/2 (the
Stomp monitors them straight to its outputs), 3 = USB 3/4, 5 = USB 5/6
(sources the preset's Input block can be set to, so the tone runs through
the chain). Events: `pcN` program change N, `cc69=N` snapshot N (0-based),
`ccC=V` any controller, on --midi-channel (1-based, default 1). --probe
plays the tone for a moment and prints each input channel's level, to find
the path. Needs numpy, sounddevice, mido and python-rtmidi.
"""
import argparse
import json
import math
import struct
import sys
import threading
import time

import numpy as np
import sounddevice as sd

FS = 48000


def parse_events(text):
    out = []
    for item in filter(None, (t.strip() for t in text.split(','))):
        when, what = item.split(':')
        out.append((float(when), what))
    return sorted(out)


def midi_message(what, channel):
    import mido
    if what.startswith('pc'):
        return mido.Message('program_change', channel=channel, program=int(what[2:]))
    cc, value = what[2:].split('=')
    return mido.Message('control_change', channel=channel, control=int(cc), value=int(value))


def write_wav(path, data):
    """32-bit float WAV, as many channels as data has columns."""
    frames, channels = data.shape
    pcm = data.astype('<f4').tobytes()
    with open(path, 'wb') as f:
        f.write(b'RIFF' + struct.pack('<I', 36 + len(pcm)) + b'WAVE')
        f.write(b'fmt ' + struct.pack('<IHHIIHH', 16, 3, channels, FS, FS * 4 * channels, 4 * channels, 32))
        f.write(b'data' + struct.pack('<I', len(pcm)) + pcm)


def record(device, out_pair, freq, level_db, duration, events, midi_channel, midi_port):
    amp = 10 ** (level_db / 20)
    n_total = int(FS * duration)
    recorded = np.zeros((n_total + FS, 8), np.float32)
    state = {'frames': 0, 'phase': 0.0}
    lock = threading.Lock()

    def callback(indata, outdata, frames, time_info, status):
        if status:
            print('stream status:', status, file=sys.stderr)
        with lock:
            start = state['frames']
            if start + frames <= recorded.shape[0]:
                recorded[start:start + frames] = indata
            state['frames'] = start + frames
            ph = state['phase']
        t = ph + np.arange(frames)
        tone = (amp * np.sin(2 * np.pi * freq * t / FS)).astype(np.float32)
        outdata.fill(0)
        outdata[:, out_pair - 1] = tone
        outdata[:, out_pair] = tone
        state['phase'] = (ph + frames) % FS

    sent = []
    port = None
    if events:
        import mido
        port = mido.open_output(midi_port)
    with sd.Stream(device=device, samplerate=FS, channels=(8, 8), dtype='float32', blocksize=256, callback=callback):
        t0 = time.monotonic()
        while True:                                       # the stream must actually run
            with lock:
                running = state['frames'] > 0
            if running:
                break
            if time.monotonic() - t0 > 3.0:
                raise SystemExit('no audio callbacks in 3 s: is the Stomp in use by another program, '
                                 'or does the terminal lack microphone permission?')
            time.sleep(0.01)
        t0 = time.monotonic()
        for when, what in events:
            while time.monotonic() - t0 < when:
                time.sleep(0.001)
            with lock:
                at = state['frames']
            port.send(midi_message(what, midi_channel - 1))
            sent.append({'t': when, 'what': what, 'sample': at})
            print(f'  {when:6.3f} s  sample {at:8d}  {what}')
        while True:
            with lock:
                done = state['frames'] >= n_total
            if done:
                break
            time.sleep(0.01)
    if port:
        port.close()
    return recorded[:n_total], sent


def analyse(data, sent, threshold_db):
    sys.path.insert(0, __import__('os').path.dirname(__import__('os').path.abspath(__file__)))
    from measure_gap import envelope_db, find_gaps
    for ch in (0, 1):
        env, step = envelope_db(data[:, ch:ch + 1], FS, 1.0)
        gaps = find_gaps(env, step, threshold_db)
        print(f'channel {ch + 1}: level {np.median(env):.1f} dBFS, {len(gaps)} gap(s)')
        for g in gaps:
            near = [s for s in sent if abs(s['sample'] / FS - g['at_s']) < 0.3]
            label = ', '.join(f"{s['what']} {1000 * (g['at_s'] - s['sample'] / FS):+.0f} ms" for s in near) or 'no event nearby'
            print(f"  at {g['at_s']:7.3f} s: fade-out {g['fade_out_ms']:6.1f} ms, gap {g['gap_ms']:6.1f} ms, "
                  f"fade-in {g['fade_in_ms']:6.1f} ms, floor {g['floor_db']:.1f} dB   [{label}]")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--device', default='HX Stomp')
    ap.add_argument('--midi-port', default='HX Stomp')
    ap.add_argument('--midi-channel', type=int, default=1)
    ap.add_argument('--out-pair', type=int, default=3, choices=(1, 3, 5, 7), help='USB channel pair for the tone')
    ap.add_argument('--freq', type=float, default=440.0)
    ap.add_argument('--level-db', type=float, default=-12.0)
    ap.add_argument('--duration', type=float, default=None)
    ap.add_argument('--events', default='')
    ap.add_argument('--threshold-db', type=float, default=-40.0)
    ap.add_argument('--wav', help='save the 8-channel recording here')
    ap.add_argument('--probe', action='store_true', help='play 2 s and print each input channel\'s level')
    a = ap.parse_args()
    events = parse_events(a.events)
    duration = a.duration or (2.0 if a.probe else (events[-1][0] + 1.5 if events else 3.0))
    print(f'tone {a.freq:.0f} Hz at {a.level_db} dBFS on USB {a.out_pair}/{a.out_pair + 1}, {duration:.1f} s')
    data, sent = record(a.device, a.out_pair, a.freq, a.level_db, duration, events, a.midi_channel, a.midi_port)
    if a.probe:
        half = data[len(data) // 2:]
        for ch in range(8):
            rms = float(np.sqrt(np.mean(half[:, ch] ** 2))) + 1e-12
            print(f'  USB in {ch + 1}: {20 * math.log10(rms):7.1f} dBFS')
        return
    if a.wav:
        write_wav(a.wav, data)
        with open(a.wav + '.json', 'w') as f:
            json.dump(sent, f, indent=1)
        print(f'saved {a.wav} (+ .json with the event samples)')
    analyse(data, sent, a.threshold_db)


if __name__ == '__main__':
    main()
