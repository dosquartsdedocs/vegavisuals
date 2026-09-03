from __future__ import annotations

import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from vegavisuals import Registry


def independent_make_environment() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key not in {"MAKEFLAGS", "MFLAGS", "MAKEOVERRIDES"}
    }


class FactoryStartupTest(unittest.TestCase):
    def test_mcp_environment_key_tracks_version_and_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            factories = [root / "factory-one", root / "factory-two"]
            for factory in factories:
                (factory / "scripts").mkdir(parents=True)
                (factory / "src/vegavisuals").mkdir(parents=True)
                for relative in (
                    "Makefile",
                    "pyproject.toml",
                    "hatch_build.py",
                    "scripts/mcp-env-bootstrap",
                ):
                    source = REPO_ROOT / relative
                    destination = factory / relative
                    shutil.copy2(source, destination)
                shutil.copy2(
                    REPO_ROOT / "src/vegavisuals/_version.py",
                    factory / "src/vegavisuals/_version.py",
                )

            def environment_key(factory: pathlib.Path) -> str:
                completed = subprocess.run(
                    ["make", "--no-print-directory", "-np", "help"],
                    cwd=factory,
                    env=independent_make_environment(),
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=20,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                prefix = "MCP_ENV_KEY := "
                values = [
                    line.removeprefix(prefix)
                    for line in completed.stdout.splitlines()
                    if line.startswith(prefix)
                ]
                self.assertEqual(len(values), 1)
                self.assertRegex(values[0], r"^[0-9a-f]{24}$")
                return values[0]

            first = environment_key(factories[0])
            second_checkout = environment_key(factories[1])
            (factories[0] / "src/vegavisuals/_version.py").write_text(
                '__version__ = "99.0.0"\n',
                encoding="utf-8",
            )
            changed_version = environment_key(factories[0])

            self.assertNotEqual(first, second_checkout)
            self.assertNotEqual(first, changed_version)

    def test_concurrent_cold_starts_build_once_and_preserve_literal_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            factory = root / "factory $dollar $(touch injected-curdir) `touch injected-curdir-tick`"
            scripts = factory / "scripts"
            scripts.mkdir(parents=True)
            shutil.copy2(REPO_ROOT / "Makefile", factory / "Makefile")
            shutil.copy2(REPO_ROOT / "scripts/mcp-env-bootstrap", scripts / "mcp-env-bootstrap")
            shutil.copy2(REPO_ROOT / "scripts/mcp-stdio-launcher", scripts / "mcp-stdio-launcher")
            (factory / "pyproject.toml").write_text("[project]\nname='startup-test'\n", encoding="utf-8")

            fake_python = root / "fake-python"
            fake_python.write_text(
                """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "-c" ]]; then
  printf '%s\n' "${FAKE_ENV_KEY:-test-key}"
  exit 0
fi
if [[ "${1:-}" == "-m" && "${2:-}" == "venv" ]]; then
  venv="${3:?}"
  printf '%s\n' build >>"$(dirname -- "$0")/bootstrap-count"
  mkdir -p -- "${venv}/bin"
  sleep 0.2
  cp -- "$0" "${venv}/bin/python"
  cat >"${venv}/bin/vegavisuals" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
project=""
if [[ "${1:-}" == "--project" ]]; then
  project="${2:?}"
  shift 2
fi
case "${1:-}" in
  install-check|ensure-renderer)
    exit 0
    ;;
  mcp)
    [[ "${2:-}" == "serve" ]]
    [[ -f "$(dirname -- "$0")/../.installed" ]]
    printf '%s' "${project}" >"${STARTUP_RESULT:?}"
    ;;
  *)
    exit 2
    ;;
esac
EOF
  chmod +x "${venv}/bin/python" "${venv}/bin/vegavisuals"
  exit 0
fi
if [[ "${1:-}" == "-m" && "${2:-}" == "pip" ]]; then
  if [[ "${FAIL_INSTALL:-0}" == "1" ]]; then
    exit 9
  fi
  exit 0
fi
exit 2
""",
                encoding="utf-8",
            )
            fake_python.chmod(fake_python.stat().st_mode | stat.S_IXUSR)

            consumers = [
                root / "consumer one $dollar $(shell touch injected-make) `touch injected-tick`",
                root / "consumer two $$ $(touch injected-second)",
            ]
            results = [root / "first-result", root / "second-result"]
            for consumer in consumers:
                consumer.mkdir()

            processes: list[subprocess.Popen[str]] = []
            stdouts: list[str] = []
            for consumer, result in zip(consumers, results, strict=True):
                environment = {
                    **independent_make_environment(),
                    "PYTHON": str(fake_python),
                    "MCP_CONSUMER_WORKSPACE": str(consumer),
                    "STARTUP_RESULT": str(result),
                }
                processes.append(
                    subprocess.Popen(
                        ["make", "--no-print-directory", "-C", str(factory), "mcp-stdio"],
                        cwd=root,
                        env=environment,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                )

            failures: list[str] = []
            for process in processes:
                stdout, stderr = process.communicate(timeout=20)
                stdouts.append(stdout)
                if process.returncode != 0:
                    failures.append(f"returncode={process.returncode}\nstdout={stdout}\nstderr={stderr}")

            self.assertEqual(failures, [])
            self.assertEqual(stdouts, ["", ""])
            self.assertEqual((root / "bootstrap-count").read_text(encoding="utf-8"), "build\n")
            self.assertEqual(
                [result.read_text(encoding="utf-8") for result in results],
                [str(consumer) for consumer in consumers],
            )
            self.assertTrue((factory / ".cache/vegavisuals/mcp-venvs/test-key/.installed").is_file())

            make_result = root / "make-result"
            make_environment = {
                **independent_make_environment(),
                "PYTHON": str(fake_python),
                "MCP_CONSUMER_WORKSPACE": str(consumers[0]),
                "STARTUP_RESULT": str(make_result),
            }
            completed = subprocess.run(
                ["make", "--no-print-directory", "-C", str(factory), "mcp-stdio"],
                cwd=root,
                env=make_environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=20,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout, "")
            self.assertEqual(make_result.read_text(encoding="utf-8"), str(consumers[0]))
            self.assertEqual((root / "bootstrap-count").read_text(encoding="utf-8"), "build\n")

            failed_environment = {
                **independent_make_environment(),
                "PYTHON": str(fake_python),
                "FAKE_ENV_KEY": "retry-key",
                "FAIL_INSTALL": "1",
            }
            failed = subprocess.run(
                ["make", "mcp-env"],
                cwd=factory,
                env=failed_environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=20,
                check=False,
            )
            self.assertNotEqual(failed.returncode, 0)
            self.assertFalse((factory / ".cache/vegavisuals/mcp-venvs/retry-key").exists())

            retry_environment = {key: value for key, value in failed_environment.items() if key != "FAIL_INSTALL"}
            retry = subprocess.run(
                ["make", "mcp-env"],
                cwd=factory,
                env=retry_environment,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=20,
                check=False,
            )
            self.assertEqual(retry.returncode, 0, retry.stderr)
            self.assertTrue((factory / ".cache/vegavisuals/mcp-venvs/retry-key/.installed").is_file())
            self.assertEqual((root / "bootstrap-count").read_text(encoding="utf-8"), "build\nbuild\nbuild\n")
            for injected in (
                "injected-curdir",
                "injected-curdir-tick",
                "injected-make",
                "injected-tick",
                "injected-second",
            ):
                self.assertFalse((factory / injected).exists())

    def test_cross_checkout_concurrent_cold_renderer_ensures_build_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = pathlib.Path(temporary)
            bin_dir = root / "bin"
            state = root / "docker-state"
            bin_dir.mkdir()
            state.mkdir()
            consumers = [root / "first consumer", root / "second consumer"]
            for consumer in consumers:
                consumer.mkdir()

            checkouts = []
            for name in ("checkout-one", "checkout-two"):
                checkout = root / name
                shutil.copytree(REPO_ROOT / "src/vegavisuals", checkout / "src/vegavisuals")
                checkouts.append(checkout)

            probe = Registry(consumers[0])
            try:
                contract = probe._renderer_contract(probe.assets / "compat/vl-convert-1.9.0.json")
            finally:
                probe.close()

            fake_docker = bin_dir / "docker"
            fake_docker.write_text(
                """#!/usr/bin/env bash
set -euo pipefail
state="${FAKE_DOCKER_STATE:?}"
case "${1:-} ${2:-}" in
  "image inspect")
    printf '%s\n' inspect >>"${state}/inspect-count"
    [[ -f "${state}/ready" ]] || exit 1
    printf 'sha256:%064d\t%s\n' 1 "${FAKE_RENDERER_CONTRACT:?}"
    ;;
  "build --build-arg")
    printf '%s\n' build >>"${state}/build-count"
    sleep 0.3
    : >"${state}/ready"
    ;;
  *)
    exit 2
    ;;
esac
""",
                encoding="utf-8",
            )
            fake_docker.chmod(fake_docker.stat().st_mode | stat.S_IXUSR)

            processes = []
            for checkout, consumer in zip(checkouts, consumers, strict=True):
                environment = {
                    **os.environ,
                    "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
                    "PYTHONPATH": str(checkout / "src"),
                    "XDG_CACHE_HOME": str(checkout / "cache"),
                    "DOCKER_HOST": f"unix://{root}/fake-docker.sock",
                    "FAKE_DOCKER_STATE": str(state),
                    "FAKE_RENDERER_CONTRACT": contract,
                }
                processes.append(
                    subprocess.Popen(
                        [
                            sys.executable,
                            "-m",
                            "vegavisuals.cli",
                            "--project",
                            str(consumer),
                            "ensure-renderer",
                        ],
                        cwd=checkout,
                        env=environment,
                        text=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                )
            failures = []
            payloads = []
            for process in processes:
                stdout, stderr = process.communicate(timeout=20)
                if process.returncode != 0:
                    failures.append(f"returncode={process.returncode}\nstdout={stdout}\nstderr={stderr}")
                else:
                    payloads.append(json.loads(stdout))

            self.assertEqual(failures, [])
            self.assertEqual((state / "build-count").read_text(encoding="utf-8"), "build\n")
            self.assertEqual((state / "inspect-count").read_text(encoding="utf-8"), "inspect\n" * 3)
            self.assertEqual(sorted(payload["built"] for payload in payloads), [False, True])
            self.assertEqual({payload["image_id"] for payload in payloads}, {"sha256:" + "0" * 63 + "1"})


if __name__ == "__main__":
    unittest.main()
