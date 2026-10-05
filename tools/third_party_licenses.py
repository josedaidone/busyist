"""
Write the licenses of everything bundled into the packaged app.

    python tools/third_party_licenses.py dist/Busyist/THIRD_PARTY_LICENSES.txt

Run with the build environment's Python: it lists that environment's
packages, minus the build tools that are not shipped.
"""
import importlib.metadata as md
import sys
from pathlib import Path

NOT_SHIPPED = {
    "pip", "setuptools", "wheel", "pyinstaller", "pyinstaller-hooks-contrib",
    "altgraph", "pefile", "packaging", "pywin32-ctypes",
}


def license_name(meta) -> str:
    named = meta.get("License-Expression") or ""
    if not named:
        first = (meta.get("License") or "").strip().splitlines()
        named = first[0] if first else ""
    classifiers = [c.split("::")[-1].strip() for c in meta.get_all("Classifier") or [] if c.startswith("License")]
    return named[:80] or ", ".join(classifiers) or "see below"


def license_texts(dist) -> list[str]:
    texts = []
    for file in dist.files or []:
        name = Path(str(file)).name.upper()
        if name.startswith(("LICENSE", "LICENCE", "COPYING", "NOTICE", "AUTHORS")) and ".dist-info" in str(file):
            path = Path(dist.locate_file(file))
            if path.is_file():
                texts.append(path.read_text(encoding="utf-8", errors="replace").strip())
    return texts


def main(out: str) -> None:
    lines = [
        "Busyist bundles the following third-party software.",
        "Each is used under its own license, reproduced below.",
        "LGPL components (pystray, zeroconf) are shipped as separate, replaceable",
        "Python modules inside the _internal folder.",
        "",
    ]
    dists = sorted(
        (d for d in md.distributions() if d.metadata["Name"].lower() not in NOT_SHIPPED),
        key=lambda d: d.metadata["Name"].lower(),
    )
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    sections = [("Python", ".".join(map(str, sys.version_info[:3])), "PSF-2.0", "https://www.python.org",
                 [python_license.read_text(encoding="utf-8", errors="replace")] if python_license.exists() else [])]
    for dist in dists:
        meta = dist.metadata
        home = meta.get("Home-page") or next(
            (u.split(",", 1)[-1].strip() for u in meta.get_all("Project-URL") or []), "")
        sections.append((meta["Name"], dist.version, license_name(meta), home, license_texts(dist)))
    for name, version, lic, home, texts in sections:
        lines += ["=" * 78, f"{name} {version}", f"License: {lic}"]
        if home:
            lines.append(f"Home: {home}")
        lines.append("")
        lines += texts or ["(No license file shipped with the package; see its home page.)"]
        lines.append("")
    Path(out).write_text("\n".join(lines), encoding="utf-8")
    print(f"wrote {out} ({len(sections)} components)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "THIRD_PARTY_LICENSES.txt")
