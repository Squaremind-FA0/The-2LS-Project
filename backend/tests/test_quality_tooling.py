from pathlib import Path


def test_requirements_declare_backend_quality_tools() -> None:
    requirements = Path(__file__).resolve().parents[1] / "requirements.txt"
    declared_packages = {
        line.split(">=", 1)[0].split("==", 1)[0].split("[", 1)[0].strip().lower()
        for line in requirements.read_text().splitlines()
        if line and not line.startswith("#")
    }

    assert {"pytest", "ruff"} <= declared_packages
