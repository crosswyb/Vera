# Audio regression fixture

`opus-in-m4a.m4a` contains one second of a generated 440 Hz tone, stereo at 48 kHz,
encoded as Opus in an MPEG-4 container. It contains no speech or meeting data.
On the affected macOS runtime, `AVAudioFile.length` is zero and reading PCM fails,
although `AVAssetReader` can decode it. A filename-only M4A test misses this bug.

Generated with FFmpeg (development fixture creation only; the application uses
Apple frameworks and does not require FFmpeg):

```sh
ffmpeg -f lavfi -i 'sine=frequency=440:sample_rate=48000:duration=1' \
  -c:a libopus -b:a 24k -ac 2 -f mp4 opus-in-m4a.m4a
```
