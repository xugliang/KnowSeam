"""Run database E2E in a new disposable container, never the production database.

Requires an already-present PostgreSQL 17 + pgvector image. No image pull,
host volume, system change, broad cleanup or existing-container operation.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
import uuid


def docker(*args):
    return subprocess.check_output(["docker", *args], text=True, stderr=subprocess.DEVNULL).strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True, help="Existing PostgreSQL/pgvector image; never pulled")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    name = "knowseam-test-" + uuid.uuid4().hex[:12]
    password = secrets.token_urlsafe(32)
    identifier = None
    with tempfile.TemporaryDirectory(prefix="knowseam-db-test-") as directory:
        env_file = Path(directory) / "postgres.env"
        env_file.write_text("POSTGRES_USER=knowseam\nPOSTGRES_DB=knowseam_test\nPOSTGRES_PASSWORD=" + password + "\n")
        env_file.chmod(0o600)
        try:
            identifier = docker("run", "--rm", "--detach", "--pull=never", "--name", name,
                                "--label", "knowseam.test=" + name, "--memory=512m", "--cpus=1", "--pids-limit=128",
                                "--tmpfs", "/var/lib/postgresql/data:rw,size=512m",
                                "--publish", "127.0.0.1::5432", "--env-file", str(env_file), args.image)
            port = json.loads(docker("inspect", identifier))[0]["NetworkSettings"]["Ports"]["5432/tcp"][0]["HostPort"]
            for _ in range(60):
                check = subprocess.run(["docker", "exec", identifier, "pg_isready", "-U", "knowseam",
                                        "-d", "knowseam_test"], capture_output=True)
                if check.returncode == 0:
                    break
                time.sleep(.5)
            else:
                raise RuntimeError("test database did not become ready")
            env = dict(os.environ)
            env["SUITE_KNOWLEDGE_TEST_DSN"] = f"postgresql://knowseam:{password}@127.0.0.1:{port}/knowseam_test"
            result = subprocess.run([sys.executable, "-m", "pytest", "tests/test_postgres_e2e.py", "-q",
                                     "--tb=short", "--basetemp", str(Path(directory) / "pytest")], cwd=root, env=env)
            return result.returncode
        finally:
            if identifier:
                info = json.loads(docker("inspect", identifier))[0]
                if info["Id"] == identifier and info["Config"]["Labels"].get("knowseam.test") == name:
                    docker("stop", "--time=5", identifier)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:
        print("Isolated database verification failed; production containers were not targeted.", file=sys.stderr)
        raise SystemExit(1) from None
