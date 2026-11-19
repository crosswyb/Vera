import math
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

from live import workbench_audio_codec as codec


@unittest.skipUnless(sys.platform == 'darwin' and Path('/usr/bin/afconvert').is_file(), 'macOS Core Audio')
class CaptureCodecTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        self.source = self.dir / 'source.caf'
        self.dest = self.dir / 'verified.m4a'

    def make_track(self, rate=48000, channels=1, frames=48019):
        # A different frequency in each channel catches dropped or swapped inputs.
        samples = [int(10000 * math.sin(i * (c + 1) * 2 * math.pi * 440 / rate))
                   for i in range(frames) for c in range(channels)]
        wav = self.dir / 'fixture.wav'
        with wave.open(str(wav), 'wb') as stream:
            stream.setparams((channels, 2, rate, 0, 'NONE', ''))
            stream.writeframes(struct.pack(f'<{len(samples)}h', *samples))
        subprocess.run(['/usr/bin/afconvert', '-f', 'caff', '-d', 'LEF32', wav, self.source], check=True)
        return samples

    def test_real_round_trip_retains_each_channel_and_all_frames(self):
        for rate, ch in [(48000, 1), (44100, 2)]:
            with self.subTest(rate=rate, channels=ch):
                samples = self.make_track(rate, ch)
                before = self.source.read_bytes()
                target = self.dir / f'archive-{ch}.m4a'
                codec.encode_caf(self.source, target)
                self.assertEqual(self.source.read_bytes(), before)
                self.assertEqual(target.stat().st_mode & 0o777, 0o600)
                self.assertLess(target.stat().st_size, len(before) // 4)
                wav = self.dir / 'playback.wav'
                subprocess.run(['/usr/bin/afconvert', '-f', 'WAVE', '-d', 'LEI16', target, wav], check=True)
                with wave.open(str(wav), 'rb') as stream:
                    self.assertEqual((stream.getframerate(), stream.getnchannels(), stream.getnframes()),
                                     (rate, ch, len(samples) // ch))
                    actual = struct.unpack(f'<{len(samples)}h', stream.readframes(stream.getnframes()))
                energy = sum(x * x for x in samples)
                error = sum((x - y) ** 2 for x, y in zip(samples, actual))
                self.assertLess(error, energy / 40)  # More than 16 dB signal-to-error for this tone.

    def test_invalid_source_and_dangling_target_link_remain_untouched(self):
        self.source.write_bytes(b'not a CAF')
        with self.assertRaises(codec.AudioCodecError):
            codec.encode_caf(self.source, self.dest)
        self.assertFalse(self.dest.exists())
        self.dest.symlink_to(self.dir / 'missing')
        with self.assertRaises(FileExistsError):
            codec.encode_caf(self.source, self.dest)
        self.assertTrue(self.dest.is_symlink())
        self.assertEqual(self.source.read_bytes(), b'not a CAF')

    def test_long_cjk_paths_and_xml_specials_transcode(self):
        self.make_track(frames=5123)
        folder = self.dir / ('会议归档' * 17 + " & <客户> '\"")
        folder.mkdir()
        src = self.source.rename(folder / ('原始音频' * 16 + " '<&>\".caf"))
        dest = folder / "归档 & <录音> '\".m4a"
        codec.encode_caf(src, dest)
        self.assertEqual(codec._track(dest, 'm4af', 'aac'), (48000, 1, 5123))
        self.assertTrue(src.is_file())

    def test_rival_publisher_is_not_replaced(self):
        self.make_track(frames=2001)
        link = os.link

        def first_publisher_wins(staged, target):
            target.write_bytes(b'other publisher')
            link(staged, target)

        with patch.object(codec.os, 'link', side_effect=first_publisher_wins), self.assertRaises(FileExistsError):
            codec.encode_caf(self.source, self.dest)
        self.assertEqual(self.dest.read_bytes(), b'other publisher')
        self.assertTrue(self.source.is_file())
        self.assertEqual(list(self.dir.glob('.encode-*')), [])

    def test_truncated_payload_is_not_published(self):
        self.make_track(frames=5000)
        original_run = codec._run

        def truncate_after_encoding(args, timeout, **kwargs):
            result = original_run(args, timeout, **kwargs)
            if args[0] == '/usr/bin/afconvert':
                path = args[-1]
                with path.open('r+b') as stream:
                    stream.truncate(path.stat().st_size - 150)
            return result

        with patch.object(codec, '_run', side_effect=truncate_after_encoding), self.assertRaises(codec.AudioCodecError):
            codec.encode_caf(self.source, self.dest)
        self.assertTrue(self.source.is_file())
        self.assertFalse(self.dest.exists())
        self.assertEqual(list(self.dir.glob('.encode-*')), [])

    def test_source_changes_before_publication(self):
        self.make_track(frames=2001)
        decode = codec._check_decoded

        def resume_writing(path, track):
            decode(path, track)
            with self.source.open('ab') as stream:
                stream.write(b'new source data')

        with patch.object(codec, '_check_decoded', side_effect=resume_writing), self.assertRaisesRegex(
                codec.AudioCodecError, 'Source changed'):
            codec.encode_caf(self.source, self.dest)
        self.assertTrue(self.source.read_bytes().endswith(b'new source data'))
        self.assertFalse(self.dest.exists())
