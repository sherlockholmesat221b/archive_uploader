from pathlib import Path
from unittest.mock import patch

from archive_uploader.mega_backup import mega_backup_release
from archive_uploader.models import Release


def test_mega_backup_creates_remote_parent_chain_for_album(tmp_path):
    album = tmp_path / "Album"
    album.mkdir()
    (album / "01.flac").write_bytes(b"audio")
    rel = Release(kind="album", dir_or_file=album, artist="Artist", title="Album")

    calls = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(args, **kwargs):
        calls.append(args)
        return Result()

    with patch("archive_uploader.mega_backup.shutil.which", return_value="/usr/bin/tool"),          patch("archive_uploader.mega_backup._mega_auth_args", return_value=["--username", "u", "--password", "p"]),          patch("archive_uploader.mega_backup.subprocess.run", side_effect=fake_run):
        assert mega_backup_release(rel, remote_root="/archive_uploader_backups") is True

    mkdirs = [c[-1] for c in calls[:3]]
    assert mkdirs == [
        "/archive_uploader_backups",
        "/archive_uploader_backups/artist-album",
    ]
    assert calls[2][0] == "megacopy"


def test_mega_backup_keeps_going_when_parent_already_exists(tmp_path):
    album = tmp_path / "Album"
    album.mkdir()
    (album / "01.flac").write_bytes(b"audio")
    rel = Release(kind="album", dir_or_file=album, artist="Artist", title="Album")

    calls = []

    class Result:
        def __init__(self, returncode=0, stderr=""):
            self.returncode = returncode
            self.stdout = ""
            self.stderr = stderr

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[0] == "megamkdir" and args[-1] == "/archive_uploader_backups":
            return Result(1, "directory already exists")
        return Result()

    with patch("archive_uploader.mega_backup.shutil.which", return_value="/usr/bin/tool"),          patch("archive_uploader.mega_backup._mega_auth_args", return_value=["--username", "u", "--password", "p"]),          patch("archive_uploader.mega_backup.subprocess.run", side_effect=fake_run):
        assert mega_backup_release(rel, remote_root="/archive_uploader_backups") is True

    assert calls[-1][0] == "megacopy"
