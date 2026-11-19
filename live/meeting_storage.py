"""Persist the default destination and bind each meeting to its own stable root."""

import hashlib
import os
import tempfile
import threading
from pathlib import Path

from live.artifact_writer import ArtifactWriter
from live.store import LiveError, require


class MeetingStorage:
    def _init_storage(self, root):
        initial = str(Path(root).expanduser().resolve())
        with self.store.transaction() as db:
            rows = self.store._all(db, "wb_storage")
            first_install = not rows and not self.store._all(db, "wb_meeting")
            if not rows:
                settings = {"output_root": initial, "default_output_root": initial}
                self.store._put(db, "wb_storage", "default", settings)
            else:
                settings = self.store._get(db, "wb_storage", "default")
            # Bind legacy meetings before any setting can change. Reopening with
            # a different CLI default must not relocate previously saved files.
            for meeting in self.store._all(db, "wb_meeting"):
                if not meeting.get("output_root"):
                    meeting["output_root"] = settings["default_output_root"]
                    self.store._put(db, "wb_meeting", meeting["id"], meeting)
        self._writer_lock = threading.Lock()
        registry = self.store.path.with_name(self.store.path.name + ".temporary.sqlite3")
        self.writer = ArtifactWriter(settings["default_output_root"], registry_path=registry, create_root=first_install)
        self._writers = {str(self.writer.root): self.writer}

    def writer_for(self, meeting):
        root = meeting["output_root"]
        # A volume/directory replaced by a symlink must not redirect old meetings.
        require(str(Path(root).resolve()) == root, "会议保存目录已变化，请恢复原目录后重试", 409)
        with self._writer_lock:
            if root not in self._writers:
                key = hashlib.sha256(root.encode("utf-8")).hexdigest()
                registry = self.store.path.with_name(self.store.path.name + ".temporary-" + key + ".sqlite3")
                self._writers[root] = ArtifactWriter(root, registry_path=registry, create_root=False)
            return self._writers[root]

    def storage(self, principal):
        with self.store.transaction() as db:
            self.store._acl(db, principal, principal["project_id"], {"owner"})
            return self.store._get(db, "wb_storage", "default")

    def set_storage(self, principal, value):
        require(isinstance(value, dict) and set(value) == {"output_root"}, "请提供会议保存目录 output_root", 400)
        raw = value["output_root"]
        require(isinstance(raw, str) and bool(raw.strip()) and len(raw) <= 4096
                and not any(ord(c) < 32 or ord(c) == 127 for c in raw), "保存目录无效", 400)
        path = Path(raw).expanduser()
        require(path.is_absolute(), "请选择绝对路径的文件夹", 400)
        try:
            path = path.resolve()
            require(path != Path(path.anchor), "不能将文件系统根目录作为会议保存目录", 400)
            with self.store.transaction() as db:
                self.store._acl(db, principal, principal["project_id"], {"owner"})
                for meeting in self.store._all(db, "wb_meeting"):
                    if meeting.get("directory"):
                        directory = Path(meeting["output_root"]) / meeting["directory"]
                        require(not path.is_relative_to(directory), "不能把保存目录设在已有会议文件夹内", 409)
                path.mkdir(parents=True, exist_ok=True, mode=0o700)
                # Test the actual write path. os.access alone cannot establish
                # that a mounted or ACL-controlled directory accepts new files.
                with tempfile.TemporaryFile(prefix=".zsjh-storage-check-", dir=path) as probe:
                    probe.write(b"ZSJH storage check\n")
                    probe.flush()
                    os.fsync(probe.fileno())
                settings = self.store._get(db, "wb_storage", "default")
                settings["output_root"] = str(path)
                self.store._put(db, "wb_storage", "default", settings)
                return settings
        except (OSError, RuntimeError) as exc:
            raise LiveError("无法写入所选文件夹，请检查目录权限、磁盘空间和磁盘连接后重试", 400) from exc
