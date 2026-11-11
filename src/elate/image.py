"""Region screenshots, pixel probes, and tolerant image comparison.

Coordinates are Emacs frame pixels: logical pixels (points on macOS)
relative to the top-left of the frame's native area -- the space
`window-pixel-edges' and mouse posns use. A GUI capture is in device
pixels (2x on a Retina display) and includes the window decorations, so
every crop maps frame pixels through the frame's outer/native edges and
the capture's measured scale.

Needs Pillow (the ``elate[image]`` extra).
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import ElateError

if TYPE_CHECKING:  # pragma: no cover
    from .session import Session

INSTALL_HINT = ("needs Pillow: install the image extra -- `uv tool install "
                "'elate[image]'`, `pip install 'elate[image]'`, or run "
                "`uvx --from 'elate[image]' elate ...`")


def _pil() -> Any:
    try:
        from PIL import Image
    except ImportError:
        raise ElateError(f"image operations {INSTALL_HINT}") from None
    return Image


def frame_geometry(sess: "Session") -> dict[str, Any]:
    """The selected frame's window-info record (pixel edges, windows)."""
    from . import session as S

    frames = S.window_info(sess).get("frames") or []
    frame = frames[0] if frames else None
    if not frame or not frame.get("graphic"):
        raise ElateError("region capture needs a GUI frame")
    return frame


def _outer_size(frame: dict[str, Any]) -> tuple[float, float]:
    outer = frame["outer-edges"]
    return float(outer[2] - outer[0]), float(outer[3] - outer[1])


def _window_box(frame: dict[str, Any], buffer: str | None,
                body: bool = True) -> tuple[int, int, int, int]:
    """Absolute pixel edges of the window showing BUFFER (else selected)."""
    windows = frame.get("windows") or []
    if buffer is None:
        win = next((w for w in windows if w.get("selected")), None)
    else:
        win = next((w for w in windows if w.get("buffer") == buffer), None)
    if win is None:
        shown = ", ".join(sorted({w.get("buffer", "?") for w in windows}))
        raise ElateError(f"no window shows buffer {buffer!r} "
                         f"(visible: {shown or 'none'})")
    edges = win["body-edges" if body else "edges"]
    return tuple(int(v) for v in edges)  # type: ignore[return-value]


def _reference(frame: dict[str, Any], size: tuple[int, int],
               ) -> tuple[list[int], float]:
    """The frame rectangle a capture of SIZE shows, and its scale.

    macOS captures the outer window; an X11 capture of the outer window id
    leaves out window-manager decorations that the outer edges include, so
    it can match the native edges instead. The rectangle whose size maps
    onto SIZE by one common factor wins (outer first).
    """
    native = [int(v) for v in frame["native-edges"]]
    candidates = [[int(v) for v in frame["outer-edges"]], native]
    for edges in candidates:
        w, h = edges[2] - edges[0], edges[3] - edges[1]
        if w <= 0 or h <= 0:
            continue
        sx, sy = size[0] / w, size[1] / h
        if abs(sx - sy) <= 0.02 * max(sx, sy):
            return edges, sx
    # An X11 toolkit window: the native width, plus an external menu/tool
    # bar above the native area (not in the native edges, and the window
    # manager's decorations are in the outer ones).
    width = native[2] - native[0]
    if width > 0:
        scale = size[0] / width
        if abs(scale - round(scale)) <= 0.02 and round(scale) >= 1:
            height = size[1] / scale
            if height >= native[3] - native[1]:
                return [native[0], round(native[3] - height), native[2],
                        native[3]], scale
    raise ElateError(
        f"the capture ({size[0]}x{size[1]}) does not match the frame's "
        f"outer or native size; is the selected frame the captured window?")


def crop_box(frame: dict[str, Any], size: tuple[int, int],
             region: tuple[int, int, int, int] | None = None,
             buffer: str | None = None, window: bool = False,
             ) -> tuple[tuple[int, int, int, int], float, dict[str, int]]:
    """Device-pixel box in a capture of SIZE, its scale, and the frame box.

    REGION is (x, y, w, h) in frame pixels; WINDOW crops to the body of
    the window showing BUFFER (else the selected window); neither crops to
    the frame's native area (no title bar). Returns (box, scale, box in
    frame pixels).
    """
    native = [int(v) for v in frame["native-edges"]]
    outer, scale = _reference(frame, size)
    if window:
        left, top, right, bottom = _window_box(frame, buffer)
    elif region is not None:
        x, y, w, h = region
        if w <= 0 or h <= 0:
            raise ElateError(f"region {region} needs a positive width and height")
        left, top = native[0] + x, native[1] + y
        right, bottom = left + w, top + h
    else:
        left, top, right, bottom = native
    rel = {"x": left - native[0], "y": top - native[1],
           "w": right - left, "h": bottom - top}

    def dev(v: float, limit: int) -> int:
        return max(0, min(limit, round(v * scale)))

    if (left < native[0] or top < native[1] or right > native[2]
            or bottom > native[3]):
        raise ElateError(f"region {rel} lies outside the frame "
                         f"({native[2] - native[0]}x{native[3] - native[1]})")
    box = (dev(left - outer[0], size[0]), dev(top - outer[1], size[1]),
           dev(right - outer[0], size[0]), dev(bottom - outer[1], size[1]))
    return box, scale, rel


def capture(sess: "Session", out: Path, region: tuple[int, int, int, int]
            | None = None, buffer: str | None = None, window: bool = False,
            points: bool = False) -> dict[str, Any]:
    """Capture SESS's frame into OUT, cropped and/or scaled to points."""
    from . import screenshot as shot

    image_mod = _pil()
    frame = frame_geometry(sess)
    with tempfile.TemporaryDirectory(prefix="elate-shot-") as tmp:
        full = Path(tmp) / "full.png"
        shot.capture_gui(sess, full, _outer_size(frame))
        with image_mod.open(full) as img:
            img.load()
            box, scale, rel = crop_box(frame, img.size, region, buffer, window)
            part = img.crop(box)
    if points and scale != 1:
        part = part.resize((max(1, round(part.width / scale)),
                            max(1, round(part.height / scale))),
                           image_mod.Resampling.LANCZOS)
    out.parent.mkdir(parents=True, exist_ok=True)
    part.save(out, "PNG")
    return {"path": str(out), "width": part.width, "height": part.height,
            "format": "png", "scale": round(scale, 3),
            "units": "points" if points or scale == 1 else "device-pixels",
            "region": rel}


def pixel(sess: "Session", x: int, y: int, buffer: str | None = None,
          window: bool = False) -> dict[str, Any]:
    """The color at frame pixel (X, Y) -- relative to the window body of
    BUFFER's window (else the selected one) with WINDOW."""
    from . import screenshot as shot

    image_mod = _pil()
    frame = frame_geometry(sess)
    if window:
        left, top, _, _ = _window_box(frame, buffer)
        native = frame["native-edges"]
        fx, fy = left - int(native[0]) + x, top - int(native[1]) + y
    else:
        fx, fy = x, y
    with tempfile.TemporaryDirectory(prefix="elate-px-") as tmp:
        full = Path(tmp) / "full.png"
        shot.capture_gui(sess, full, _outer_size(frame))
        with image_mod.open(full) as img:
            img = to_srgb(img).convert("RGBA")
            box, scale, _ = crop_box(frame, img.size, (fx, fy, 1, 1))
            # The device pixel at the logical pixel's centre.
            px = min(img.width - 1, box[0] + int(scale // 2))
            py = min(img.height - 1, box[1] + int(scale // 2))
            r, g, b, a = img.getpixel((px, py))
    return {"x": x, "y": y, "frame_x": fx, "frame_y": fy,
            "rgb": f"#{r:02x}{g:02x}{b:02x}", "rgba": [r, g, b, a],
            "scale": round(scale, 3)}


def to_srgb(img: Any) -> Any:
    """IMG in sRGB: macOS captures carry the display's ICC profile, whose
    values differ from the colors Emacs drew by tens of units."""
    profile = img.info.get("icc_profile")
    if not profile:
        return img
    try:
        import io

        from PIL import ImageCms

        src = ImageCms.ImageCmsProfile(io.BytesIO(profile))
        dst = ImageCms.createProfile("sRGB")
        mode = "RGBA" if img.mode in ("RGBA", "LA", "P") else "RGB"
        return ImageCms.profileToProfile(img.convert(mode), src, dst,
                                         outputMode=mode)
    except Exception:  # noqa: BLE001 - an unusable profile: raw values
        return img


def parse_color(text: str) -> tuple[int, int, int]:
    """#rrggbb (or rrggbb) to an RGB tuple."""
    raw = text.strip().lstrip("#")
    if len(raw) != 6:
        raise ElateError(f"color must be #rrggbb, got {text!r}")
    try:
        return int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16)
    except ValueError:
        raise ElateError(f"color must be #rrggbb, got {text!r}") from None


def compare(actual: Path, golden: Path, tolerance: int = 0,
            max_diff: float = 0.0, diff_out: Path | None = None
            ) -> dict[str, Any]:
    """Compare two PNGs pixel by pixel.

    A pixel differs when any RGBA channel differs by more than TOLERANCE
    (0-255). The images match when the differing pixels are at most
    MAX_DIFF -- a count when >= 1, a fraction of all pixels when < 1. On
    a mismatch DIFF_OUT receives the actual image with differing pixels
    painted red. Different sizes never match.
    """
    image_mod = _pil()
    from PIL import ImageChops

    with image_mod.open(actual) as a_img, image_mod.open(golden) as g_img:
        a = a_img.convert("RGBA")
        g = g_img.convert("RGBA")
    if a.size != g.size:
        return {"match": False, "reason": "size",
                "actual_size": list(a.size), "golden_size": list(g.size)}
    delta = ImageChops.difference(a, g)
    # Per pixel, the largest channel difference.
    bands = delta.split()
    peak = bands[0]
    for band in bands[1:]:
        peak = ImageChops.lighter(peak, band)
    mask = peak.point(lambda v: 255 if v > tolerance else 0)
    total = a.width * a.height
    differing = total - mask.histogram()[0]
    allowed = max_diff if max_diff >= 1 else max_diff * total
    result: dict[str, Any] = {"match": differing <= allowed,
                              "differing_pixels": differing,
                              "total_pixels": total,
                              "tolerance": tolerance}
    if not result["match"] and diff_out is not None:
        marked = a.copy()
        red = image_mod.new("RGBA", a.size, (255, 0, 0, 255))
        marked.paste(red, (0, 0), mask)
        diff_out.parent.mkdir(parents=True, exist_ok=True)
        marked.save(diff_out, "PNG")
        result["diff"] = str(diff_out)
    return result


def parse_region(text: str) -> tuple[int, int, int, int]:
    """"X,Y,W,H" (frame pixels) to a tuple."""
    parts = [p.strip() for p in text.split(",")]
    try:
        x, y, w, h = (int(p) for p in parts)
    except ValueError:
        raise ElateError(f"region must be X,Y,W,H in frame pixels, got "
                         f"{text!r}") from None
    return x, y, w, h


def gui_screenshot(sess: "Session", out: Path,
                   region: tuple[int, int, int, int] | None = None,
                   window: bool = False, buffer: str | None = None,
                   points: bool = False) -> dict[str, Any]:
    """A GUI screenshot, whole or cropped; the result states its scale.

    "scale" is device pixels per frame pixel (2 on a Retina display) and
    "units" says whether the PNG is in device pixels or points. Cropping
    or POINTS needs Pillow; a whole-window capture does not.
    """
    from . import screenshot as shot

    if region is not None or window or buffer is not None or points:
        return capture(sess, out, region=region, buffer=buffer,
                       window=window or buffer is not None, points=points)
    result = shot.capture_gui(sess, out)
    try:
        frame = frame_geometry(sess)
        _, scale = _reference(frame, (result["width"], result["height"]))
        result["scale"] = round(scale, 3)
        result["units"] = "device-pixels" if scale != 1 else "points"
    except ElateError:
        pass
    return result
