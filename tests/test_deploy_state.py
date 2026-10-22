import json
import os
import sqlite3
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def setup(tmp_path):
    target = tmp_path / "app"
    target.mkdir()
    (target / "docker-compose.yml").write_text("old compose")
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    with sqlite3.connect(legacy / "trades_test.db") as db:
        db.execute("CREATE TABLE discord_messages(message_id TEXT)")
        db.execute("INSERT INTO discord_messages VALUES ('100')")
    pending = legacy / "trades_test.db.discord-pending"
    pending.mkdir()
    (pending / "101.json").write_text('{"message_id":"101"}')
    release = tmp_path / "release"
    release.mkdir()
    (release / "docker-compose.yml").write_text("new compose")
    archive = tmp_path / "app.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(release / "docker-compose.yml", arcname="./docker-compose.yml")
    env_file = tmp_path / "new.env"
    env_file.write_text("DB_DIRECTORY=./data\n")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    docker = binaries / "docker"
    docker.write_text("""#!/usr/bin/env python3
import json,os,pathlib,shutil,sys
args=sys.argv[1:]
with open(os.environ['DOCKER_LOG'],'a') as log: log.write(json.dumps(args)+'\\n')
if args[0]=='compose' and 'ps' in args: print('old-container')
elif args[0]=='inspect': print(os.environ.get('MOUNT_SOURCE',''))
elif args[0]=='cp': shutil.copytree(os.environ['LEGACY_DATA'],args[-1],dirs_exist_ok=True)
""")
    docker.chmod(0o755)
    env = dict(
        os.environ,
        PATH=str(binaries) + os.pathsep + os.environ["PATH"],
        LEGACY_DATA=str(legacy),
        DOCKER_LOG=str(tmp_path / "docker.log"),
    )
    return target, legacy, archive, env_file, env


def test_deploy_migrates_old_container_and_preserves_pending_ids(tmp_path):
    target, legacy, archive, env_file, env = setup(tmp_path)
    result = subprocess.run(
        ["bash", str(ROOT / "ops/deploy.sh"), str(archive), str(env_file), str(target)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    with sqlite3.connect(target / "data/trades_test.db") as db:
        assert db.execute("SELECT message_id FROM discord_messages").fetchall() == [
            ("100",)
        ]
    assert (target / "data/trades_test.db.discord-pending/101.json").exists()
    assert (target / "docker-compose.yml").read_text() == "new compose"
    calls = [
        json.loads(line) for line in Path(env["DOCKER_LOG"]).read_text().splitlines()
    ]
    stop = next(i for i, call in enumerate(calls) if call[0] == "stop")
    copy = next(i for i, call in enumerate(calls) if call[0] == "cp")
    down = next(
        i for i, call in enumerate(calls) if call[0] == "compose" and "down" in call
    )
    assert stop < copy < down
    # A subsequent release uses the persistent bind mount without overwriting DBs.
    env["MOUNT_SOURCE"] = str(target / "data")
    Path(env["DOCKER_LOG"]).write_text("")
    subprocess.run(
        ["bash", str(ROOT / "ops/deploy.sh"), str(archive), str(env_file), str(target)],
        env=env,
        check=True,
    )
    assert not any(
        json.loads(line)[0] == "cp"
        for line in Path(env["DOCKER_LOG"]).read_text().splitlines()
    )
    with sqlite3.connect(target / "data/trades_test.db") as db:
        assert db.execute("SELECT message_id FROM discord_messages").fetchall() == [
            ("100",)
        ]


def test_conflicting_legacy_db_aborts_before_container_removal(tmp_path):
    target, legacy, archive, env_file, env = setup(tmp_path)
    (target / "data").mkdir()
    (target / "data/trades_test.db").write_bytes(b"different existing data")
    result = subprocess.run(
        ["bash", str(ROOT / "ops/deploy.sh"), str(archive), str(env_file), str(target)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "Conflicting persistent data" in result.stderr
    assert (target / "data/trades_test.db").read_bytes() == b"different existing data"
    calls = [
        json.loads(line) for line in Path(env["DOCKER_LOG"]).read_text().splitlines()
    ]
    assert not any("down" in call for call in calls)
