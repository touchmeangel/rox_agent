import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass
class FoundryConfig:
    src: str = "src"
    test: str = "test"
    script: str = "script"
    libs: tuple[str, ...] = ("lib",)


def load_foundry_config(project_path: str) -> FoundryConfig:
    toml_path = Path(project_path) / "foundry.toml"
    if not toml_path.exists():
        return FoundryConfig()

    with open(toml_path, "rb") as f:
        data = tomllib.load(f)

    profile = data.get("profile", {}).get("default", {})
    libs = profile.get("libs", ["lib"])
    return FoundryConfig(
        src=profile.get("src", "src"),
        test=profile.get("test", "test"),
        script=profile.get("script", "script"),
        libs=tuple(libs) if isinstance(libs, list) else ("lib",),
    )
