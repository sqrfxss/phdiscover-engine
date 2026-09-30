"""
Log gh in from a token held in memory, then push. Nothing touches disk.

The token is read from stdin so it never appears in a shell argument, in
~/.bash_history, or in this file. gh stores the resulting credential in the
OS credential store, which is where it belongs.
"""
import getpass
import json
import subprocess
import sys
from pathlib import Path

GH = Path.home() / "bin" / "gh.exe"
if not GH.exists():
    GH = "gh"


def run(args, *, token=None, capture=False):
    env = None
    if token:
        import os
        env = os.environ.copy()
        env["GH_TOKEN"] = token
    return subprocess.run(
        args, capture_output=capture, text=True, env=env,
        encoding="utf-8", errors="replace",
    )


def main():
    token = sys.stdin.readline().strip()
    if not token:
        print("no token on stdin", file=sys.stderr)
        return 1

    # Reject anything that is not a GitHub token, before it reaches a subprocess.
    if not token.startswith(("ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_")):
        print("that does not look like a GitHub token", file=sys.stderr)
        return 1

    who = run([str(GH), "api", "user", "--jq", ".login"], token=token, capture=True)
    if who.returncode != 0:
        print("token rejected:", (who.stderr or "").strip()[:300], file=sys.stderr)
        return 1
    login = who.stdout.strip()
    print(f"authenticated as {login}")

    scopes = run([str(GH), "api", "/user", "--jq", ".login"], token=token, capture=True)
    hdr = run([str(GH), "auth", "status", "--hostname", "github.com"],
              token=token, capture=True)
    print("scopes:", next((l.strip() for l in (hdr.stdout or "").splitlines()
                           if "Token scopes" in l), "unknown"))

    # Persist the credential through gh so later git pushes work unattended.
    save = subprocess.run(
        [str(GH), "auth", "login", "--hostname", "github.com",
         "--git-protocol", "https", "--with-token"],
        input=token, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    if save.returncode != 0:
        print("could not save the credential:", (save.stderr or "").strip()[:300],
              file=sys.stderr)
        return 1
    print("credential saved to the OS credential store")
    print(json.dumps({"login": login}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
