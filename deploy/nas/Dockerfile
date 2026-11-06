# syntax=docker/dockerfile:1
FROM python:3.14-slim-trixie AS package
WORKDIR /src
RUN pip install --no-cache-dir build
COPY . .
ARG MASS_VERSION
RUN python -c 'import os,re,pathlib; p=pathlib.Path("pyproject.toml"); p.write_text(re.sub(r"^version = .*", "version = \"" + os.environ["MASS_VERSION"] + "\"", p.read_text(), count=1, flags=re.M))'
RUN test ! -e music_assistant/helpers/app_secrets.json && python -m build --wheel

# Keep Music Assistant's shipped runtime and bundled integrations in place.
# Only our application wheel is overlaid; no upstream credentials are extracted.
FROM ghcr.io/music-assistant/server:2.10.9@sha256:4ac0d5844363fbbefd6a61251df09d96cd546e05e01b8c1cc1a28803f0935e07
COPY --from=package /src/dist /tmp/ma-fork-wheel
RUN python - <<'INSTALL'
import importlib.metadata
import pathlib
import shutil
import sysconfig
import zipfile
assert importlib.metadata.version("music-assistant") == "2.10.9"
site = pathlib.Path(sysconfig.get_path("purelib"))
for metadata in site.glob("music_assistant-*.dist-info"):
    shutil.rmtree(metadata)
wheel = next(pathlib.Path("/tmp/ma-fork-wheel").glob("*.whl"))
with zipfile.ZipFile(wheel) as archive:
    assert not any(name.endswith("app_secrets.json") for name in archive.namelist())
    archive.extractall(site)
shutil.rmtree("/tmp/ma-fork-wheel")
INSTALL
RUN python -m compileall -q /app/venv/lib/python*/site-packages/music_assistant
ARG MA_REVISION
LABEL org.opencontainers.image.source="https://github.com/willvanwazer/music-assistant-server" \
      org.opencontainers.image.revision=$MA_REVISION
