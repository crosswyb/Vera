"""Encode a closed capture CAF locally; leave source removal to its owner."""

import ctypes as C
import math
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from typing import NamedTuple


class AudioCodecError(RuntimeError):
    pass


class _Track(NamedTuple):
    rate: float
    channels: int
    frames: int


def _run(args, timeout, *, pass_fds=()):
    try:
        result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True,
                                check=True, timeout=timeout, pass_fds=pass_fds)
    except (OSError, subprocess.SubprocessError) as exc:
        raise AudioCodecError(f"{Path(args[0]).name} failed: {exc}") from exc
    return result.stdout


def _track(path, container, codec):
    # afinfo's XML writer corrupts some UTF-8 path bytes (including names in
    # warning elements). Give it a short ASCII alias to the same open inode.
    with path.open('rb') as stream:
        fd = stream.fileno()
        info = _run(['/usr/bin/afinfo', '-x', f'/dev/fd/{fd}'], timeout=60, pass_fds=(fd,))
        size = os.fstat(fd).st_size
    try:
        file = ET.fromstring(info).find('{*}audio_file')
        if file is None or file.findtext('{*}file_type', '').strip("' ") != container:
            raise ValueError('unexpected container')
        track, = file.findall('./{*}tracks/{*}track')
        if track.findtext('{*}format_type', '').strip() != codec:
            raise ValueError('unexpected audio codec')
        offset, length = int(track.findtext('{*}data_offset')), int(track.findtext('{*}audio_bytes'))
        if offset < 0 or length < 1 or offset + length > size:
            raise ValueError('missing audio payload')
        result = _Track(float(track.findtext('{*}sample_rate')), int(track.findtext('{*}num_channels')),
                        int(track.findtext('./{*}packet_table_info/{*}valid_frames')))
        if not math.isfinite(result.rate) or result.rate <= 0 or not 1 <= result.channels <= 8 or result.frames < 1:
            raise ValueError('invalid audio format or empty track')
        return result
    except (ValueError, TypeError, ET.ParseError) as exc:
        raise AudioCodecError(f'Invalid {container} audio: {exc}') from exc


def _check_decoded(path, track):
    """Read every frame via AudioToolbox in bounded memory; no second PCM file."""
    u32, ptr = C.c_uint32, C.c_void_p

    class StreamFormat(C.Structure):  # AudioStreamBasicDescription
        _fields_ = [('rate', C.c_double)] + [(name, u32) for name in
                    ('format', 'flags', 'packet_bytes', 'packet_frames', 'frame_bytes', 'channels', 'bits', 'reserved')]

    class AudioBuffer(C.Structure):
        _fields_ = [('channels', u32), ('size', u32), ('data', ptr)]

    class BufferList(C.Structure):
        _fields_ = [('count', u32), ('buffer', AudioBuffer)]

    def bind(lib, name, result, *args):
        f = getattr(lib, name)
        f.restype, f.argtypes = result, args
        return f

    def check(status):
        if status:
            raise AudioCodecError(f'Core Audio could not decode AAC (OSStatus {status})')

    cf = C.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
    audio = C.CDLL('/System/Library/Frameworks/AudioToolbox.framework/AudioToolbox')
    new_url = bind(cf, 'CFURLCreateFromFileSystemRepresentation', ptr, ptr, C.c_char_p, C.c_long, C.c_ubyte)
    release = bind(cf, 'CFRelease', None, ptr)
    open_file = bind(audio, 'ExtAudioFileOpenURL', C.c_int32, ptr, C.POINTER(ptr))
    set_format = bind(audio, 'ExtAudioFileSetProperty', C.c_int32, ptr, u32, u32, ptr)
    read = bind(audio, 'ExtAudioFileRead', C.c_int32, ptr, C.POINTER(u32), C.POINTER(BufferList))
    close = bind(audio, 'ExtAudioFileDispose', C.c_int32, ptr)
    raw_path = os.fsencode(path)
    url = new_url(None, raw_path, len(raw_path), 0)
    if not url:
        raise AudioCodecError('Core Audio could not open AAC path')
    handle = ptr()
    try:
        check(open_file(url, C.byref(handle)))
    finally:
        release(url)
    try:
        frame_bytes = track.channels * 2
        # Native endian, signed and packed interleaved Int16 (flags 4 | 8).
        pcm = StreamFormat(track.rate, int.from_bytes(b'lpcm', 'big'), 12, frame_bytes, 1,
                           frame_bytes, track.channels, 16, 0)
        check(set_format(handle, int.from_bytes(b'cfmt', 'big'), C.sizeof(pcm), C.byref(pcm)))
        block = C.create_string_buffer(16384 * frame_bytes)
        buffers = BufferList(1, AudioBuffer(track.channels, len(block), C.addressof(block)))
        total = 0
        while True:
            count = u32(16384)
            buffers.buffer.size = len(block)
            check(read(handle, C.byref(count), C.byref(buffers)))
            total += count.value
            if total > track.frames or (count.value and buffers.buffer.size != count.value * frame_bytes):
                raise AudioCodecError('AAC decoder returned inconsistent PCM data')
            if not count.value:
                break
        if total != track.frames:
            raise AudioCodecError(f'AAC decoded {total} of {track.frames} source frames')
    finally:
        close(handle)


def _identity(path):
    s = path.lstat()
    if not stat.S_ISREG(s.st_mode):
        raise AudioCodecError('Capture is not a regular file')
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns


def verify_caf_archive(src, encoded):
    source = _track(src, 'caff', 'lpcm')
    if _track(encoded, 'm4af', 'aac') != source:
        raise AudioCodecError('AAC sample rate, channel count or valid frame count changed')
    _check_decoded(encoded, source)


def encode_caf(src: Path, dest: Path) -> None:
    """Publish verified AAC at dest without replacement. Both paths remain on success.

    All scratch is private and beneath dest.parent. Pass a new target even on
    retries; an existing file is never treated as proof that encoding succeeded.
    """
    src, dest = Path(src).absolute(), Path(dest).absolute()
    if os.path.lexists(dest):
        raise FileExistsError(dest)
    identity = _identity(src)
    source = _track(src, 'caff', 'lpcm')
    with tempfile.TemporaryDirectory(prefix='.encode-', dir=dest.parent) as temporary:
        encoded = Path(temporary) / 'track.m4a'
        _run(['/usr/bin/afconvert', '-f', 'm4af', '-d', f'aac@{source.rate:g}', '-b',
              str(64000 * source.channels), '-s', '0', src, encoded],
             timeout=max(120, source.frames / source.rate * 2 + 60))
        verify_caf_archive(src, encoded)
        with encoded.open('rb') as checked:
            os.fchmod(checked.fileno(), 0o600)
            os.fsync(checked.fileno())
        if _identity(src) != identity:
            raise AudioCodecError('Source changed during encoding')
        os.link(encoded, dest)  # Atomic no-replace, even if a rival publisher just won.
