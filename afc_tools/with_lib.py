"""Run one command with lib/ staged into every runtime, exactly as deploy_all.py does, then
remove the copies again (on success, failure or Ctrl+C). Unlike deploy_all.py it never
bootstraps and never adds --yes: it runs only the command you give it.

From C:\\dev\\afc\\agentic-football-sample-agents\\ai-team-strands-balanced:
    ..\\..\\.venv\\Scripts\\python.exe ..\\afc_tools\\with_lib.py agentcore deploy --diff
    ..\\..\\.venv\\Scripts\\python.exe ..\\afc_tools\\with_lib.py agentcore deploy --dry-run
    ..\\..\\.venv\\Scripts\\python.exe ..\\afc_tools\\with_lib.py agentcore package

--write-target  first fill agentcore/aws-targets.json from sts:GetCallerIdentity (read-only)
                and AWS_REGION, as deploy_all.py does (it must not be empty for a deploy).
"""

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

TEAM = Path.cwd()
LIB = TEAM.parent / "lib"
CONFIG = TEAM / "agentcore" / "agentcore.json"


def rmtree(path):
    def retry(func, p, _):  # Windows: clear the read-only bit and retry
        os.chmod(p, stat.S_IWRITE)
        func(p)
    shutil.rmtree(path, **({"onexc": retry} if sys.version_info >= (3, 12) else {"onerror": retry}))


def write_target():
    import boto3
    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
    if not region:
        sys.exit("set AWS_REGION first (no default is assumed)")
    account = boto3.client("sts", region_name=region).get_caller_identity()["Account"]
    target = [{"name": "default", "account": account, "region": region}]
    path = TEAM / "agentcore" / "aws-targets.json"
    if json.loads(path.read_text(encoding="utf-8") or "[]") != target:
        path.write_text(json.dumps(target, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {path.relative_to(TEAM)}: account {account}, region {region}")


def main(argv):
    if argv[:1] == ["--write-target"]:
        write_target()
        argv = argv[1:]
    if not argv:
        sys.exit(__doc__)
    if not CONFIG.exists():
        sys.exit(f"run from the team directory (no {CONFIG})")
    runtimes = json.loads(CONFIG.read_text(encoding="utf-8"))["runtimes"]
    staged = []
    try:
        for rt in runtimes:
            dest = TEAM / rt["codeLocation"] / "lib"
            if dest.exists():
                rmtree(dest)
            shutil.copytree(LIB, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
            staged.append(dest)
        
        exe = shutil.which(argv[0])

        if os.name == "nt" and argv[0].lower() == "agentcore":
            cmd_exe = shutil.which("agentcore.cmd")
            if cmd_exe:
                exe = cmd_exe

        exe = exe or argv[0]
        print(f"> {' '.join(argv)}   (lib staged into {len(staged)} runtimes)")

        if os.name == "nt" and exe.lower().endswith((".cmd", ".bat")):
            command = subprocess.list2cmdline([exe, *argv[1:]])
            return subprocess.run(command, cwd=TEAM, shell=True).returncode

        return subprocess.run([exe, *argv[1:]], cwd=TEAM).returncode

    finally:
        for dest in staged:
            if dest.exists():
                rmtree(dest)
        print(f"removed {len(staged)} staged lib copies")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
