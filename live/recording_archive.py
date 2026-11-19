"""Archive closed live tracks locally. The capture path stays playable until commit."""

import errno
import os
import shutil
import tempfile
from pathlib import Path

from live.store import LiveError, require
from live.workbench_audio_codec import AudioCodecError, encode_caf, verify_caf_archive


def identity(path):
    st = path.lstat()
    return [st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns]


def clean_audio_stage(writer, recording):
    stage = recording.get('archive_stage')
    if stage:
        directory = writer.path(stage['path'])
        if directory.is_dir() and identity(directory)[:2] == stage['inode']:
            shutil.rmtree(directory)


class RecordingArchive:
    def next_audio_archive(self):
        with self.store.transaction() as db:
            for meeting in self.store._all(db, 'wb_meeting'):
                if meeting['state'] != 'ended':
                    continue
                for r in meeting.get('recordings', []):
                    if self._eligible_track(r):
                        return meeting['id'], r['id']

    @staticmethod
    def _eligible_track(r):
        return (r.get('source') in {'system', 'microphone'} and r.get('state') == 'saved'
                and r.get('archive_state') in {'pending', 'converting', 'cleanup'})

    def _archive_track(self, db, mid, rid):
        m = self.store._get(db, 'wb_meeting', mid)
        require(m['state'] == 'ended', '会议尚未结束或已清理', 409)
        r = next((v for v in m.get('recordings', []) if v['id'] == rid), None)
        require(r and self._eligible_track(r), '录音未就绪或已归档', 409)
        return m, r

    def archive_recording(self, mid, rid):
        try:
            with self.store.transaction() as db:
                m, r = self._archive_track(db, mid, rid)
                cleanup = r['archive_state'] == 'cleanup'
                if not cleanup:
                    src = self.writer.path(r['path'])
                    dst = self.writer.path(str(Path(r['path']).with_suffix('.m4a')))
                    require(src.suffix == '.caf' and src.parent == self.writer.path(m['directory']), '录音路径无效', 409)
                    if not r.get('archive_stage'):
                        original = identity(src)
                        directory = Path(tempfile.mkdtemp(prefix='.zsjh-aac-', dir=src.parent))
                        r['archive_stage'] = {'path': str(directory.relative_to(self.writer.root)),
                                              'inode': identity(directory)[:2], 'source': original}
                    stage = r['archive_stage']
                    directory = self.writer.path(stage['path'])
                    require(directory.is_dir() and identity(directory)[:2] == stage['inode'] and identity(src) == stage['source'],
                            '录音或暂存目录已变化', 409)
                    staged = directory / 'verified.m4a'
                    r['archive_state'] = 'converting'
                    self._save(db, m)
            if not cleanup:
                if not staged.exists():
                    encode_caf(src, staged)
                else:  # Staging recovered after an interrupted publication.
                    verify_caf_archive(src, staged)
                with self.store.transaction() as db:
                    m, r = self._archive_track(db, mid, rid)
                    require(r.get('archive_stage') == stage and identity(src) == stage['source'] and not staged.is_symlink(),
                            '录音在归档期间已变化', 409)
                    # A crash between the link and commit can leave our own inode at dst.
                    if not (dst.exists() and dst.samefile(staged)):
                        os.link(staged, dst)
                    r.update(raw_path=r['path'], path=str(dst.relative_to(self.writer.root)), bytes=dst.stat().st_size,
                             archive_state='cleanup', archive_identity=identity(dst))
                    self._event(db, m, 'recording.archived', {'recording_id': rid})
                    self._save(db, m)
            # Only after the playback path is durably committed may we remove the CAF.
            with self.store.transaction() as db:
                m, r = self._archive_track(db, mid, rid)
                primary = self.writer.path(r['path'])
                require(identity(primary) == r['archive_identity'], 'M4A 文件在归档期间已变化', 409)
                raw = self.writer.path(r['raw_path'])
                if not r['keep_original_caf']:
                    if raw.exists():
                        require(identity(raw) == r['archive_stage']['source'], 'CAF 文件在归档期间已变化', 409)
                        raw.unlink()
                    r.pop('raw_path')
                clean_audio_stage(self.writer, r)
                r.pop('archive_stage'); r.pop('archive_identity')
                r.update(archive_state='saved')
                self._save(db, m)
            self.publish_index(mid)
        except (AudioCodecError, OSError, LiveError) as err:
            with self.store.transaction() as db:
                try:
                    m, r = self._archive_track(db, mid, rid)
                except LiveError:  # The user deleted the meeting while the encoder was running.
                    return
                after_commit = r['archive_state'] == 'cleanup'
                r.update(archive_state='cleanup_failed' if after_commit else 'failed',
                         archive_error=('归档文件重名' if isinstance(err, FileExistsError) else
                                        '磁盘空间不足' if getattr(err, 'errno', None) == errno.ENOSPC else
                                        '文件权限不足' if isinstance(err, PermissionError) else
                                        str(err) if isinstance(err, LiveError) else
                                        '本机音频转码或完整性校验失败' if isinstance(err, AudioCodecError) else '本机录音文件无法读写'))
                if not after_commit and r.get('archive_stage'):
                    try:
                        clean_audio_stage(self.writer, r)
                        r.pop('archive_stage')
                    except (LiveError, OSError):
                        pass  # Preserve ownership for subsequent cleanup.
                self._event(db, m, 'recording.archive_failed', {'recording_id': rid})
                self._save(db, m)
            self.publish_index(mid)
