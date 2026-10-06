"""Заповнює геометрію в road_geometry.toml за osm_ways з OpenStreetMap API.

    uv run python scripts/road_geometry.py

Лінії кожної споруди мають іти одна за одною в напрямку руху; скрипт перевіряє,
що вони стикуються, і переписує записи, зберігаючи коментарі між ними.
"""

import json
import re
import sys
import tomllib
import urllib.request

from bridges_monitor.geometry import GEOMETRY_RESOURCE, distance_m, point_along

OSM_API = "https://api.openstreetmap.org/api/0.6"
HEADERS = {"User-Agent": "bridges-monitor (road geometry generator)"}


def way_points(way_id: int) -> list[tuple[float, float]]:
    req = urllib.request.Request(f"{OSM_API}/way/{way_id}/full.json", headers=HEADERS)
    with urllib.request.urlopen(req, timeout=60) as resp:
        elements = json.load(resp)["elements"]
    nodes = {e["id"]: (e["lat"], e["lon"]) for e in elements if e["type"] == "node"}
    way = next(e for e in elements if e["type"] == "way")
    return [nodes[n] for n in way["nodes"]]


def chain(way_ids: list[int]) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for way_id in way_ids:
        pts = way_points(way_id)
        if points and distance_m(points[-1], pts[0]) > 1:
            sys.exit(f"way {way_id} does not continue the previous one")
        points.extend(pts[1:] if points else pts)
    return points


def fmt(p: tuple[float, float]) -> str:
    return f"[{p[0]:.6f}, {p[1]:.6f}]"


def render(entry: dict) -> str:
    lines = [
        "[[road]]",
        f'bridge = "{entry["bridge"]}"',
        f'direction = "{entry["direction"]}"',
        "segments = [",
    ]
    polylines = [chain(seg["osm_ways"]) for seg in entry["segments"]]
    for seg, pts in zip(entry["segments"], polylines, strict=True):
        ways = ", ".join(str(w) for w in seg["osm_ways"])
        start, mid, end = pts[0], point_along(pts, 0.5), pts[-1]
        lines.append(
            f"  {{ osm_ways = [{ways}], "
            f"points = [{fmt(start)}, {fmt(mid)}, {fmt(end)}] }},"
        )
    route_a = point_along(polylines[0], 0.2)
    route_b = point_along(polylines[-1], 0.8)
    lines += ["]", f"route_points = [{fmt(route_a)}, {fmt(route_b)}]"]
    return "\n".join(lines) + "\n"


def main() -> None:
    path = GEOMETRY_RESOURCE
    blocks = re.split(r"(?=^\[\[road\]\]$)", path.read_text(), flags=re.MULTILINE)
    out = [blocks[0]]
    for block in blocks[1:]:
        entry = tomllib.loads(block)["road"][0]
        # Коментарі й порожні рядки в кінці блоку стосуються наступного запису.
        lines = block.splitlines(keepends=True)
        tail_start = len(lines)
        while tail_start > 0 and (
            not lines[tail_start - 1].strip()
            or lines[tail_start - 1].lstrip().startswith("#")
        ):
            tail_start -= 1
        out.append(render(entry) + "".join(lines[tail_start:]))
        print(f"{entry['bridge']:18} {entry['direction']:14} ok")
    path.write_text("".join(out))


if __name__ == "__main__":
    main()
