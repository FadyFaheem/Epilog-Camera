"""
Epilog eView Camera Client

Capture live camera frames from Epilog Fusion laser cutters over WebSocket.

Protocol reverse-engineered from the Epilog Pulse web interface. The laser
runs an Express server on port 80 that proxies to an internal ODROID-based
camera service. Clients open a WebSocket, send a viewport region (in inches),
and receive JSON messages containing JPEG image data.

Configuration
~~~~~~~~~~~~~

The CLI reads connection settings from ``epilog_config.json``.
On first run the file is created automatically — fill in your laser's IP::

    {
      "ip": "192.168.1.100",
      "port": 80
    }

Library usage::

    import asyncio
    from epilog_camera import EpilogCamera

    async def main():
        async with EpilogCamera("192.168.1.100") as cam:
            frame = await cam.snapshot()
            frame.save("bed.jpg")

    asyncio.run(main())

CLI usage::

    python epilog_camera.py snapshot
    python epilog_camera.py stream --max-frames 10
    python epilog_camera.py info
    python epilog_camera.py probe
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import time
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator, Callable

import websockets

__all__ = ["EpilogCamera", "CameraFrame", "Viewport", "MachineInfo", "load_config"]

DEFAULT_PORT = 80
MAX_WS_SIZE = 50 * 1024 * 1024  # 50 MB per message
CONFIG_FILE = Path("epilog_config.json")
CONFIG_EXAMPLE = Path("epilog_config.example.json")


def load_config(path: Path = CONFIG_FILE) -> dict:
    """Load connection settings from a JSON config file.

    Returns a dict with keys ``ip`` and optionally ``port``.
    Raises ``SystemExit`` with a helpful message if the file is missing.
    """
    if not path.exists():
        print(f"No config file found at: {path.resolve()}\n")
        print(f"Copy the example and fill in your laser's IP address:\n")
        print(f"  cp {CONFIG_EXAMPLE} {path}")
        print(f"\nThen set \"ip\" to your laser's IP, e.g. \"ip\": \"192.168.1.100\"")
        sys.exit(1)

    with open(path) as f:
        data = json.load(f)

    if not data.get("ip"):
        print(f"Error: \"ip\" is missing or empty in {path}")
        print("Set it to your laser's IP address, e.g.  \"ip\": \"192.168.1.100\"")
        sys.exit(1)

    return data


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Viewport:
    """Region of the laser bed in inches."""
    x: float = 0
    y: float = 0
    w: float = 24
    h: float = 12

    def to_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}

    @classmethod
    def full_bed(cls, bed_w: float = 24, bed_h: float = 12) -> Viewport:
        return cls(0, 0, bed_w, bed_h)


@dataclass
class MachineInfo:
    """Machine metadata returned by the ``/info`` endpoint."""
    type: int = 0
    bed_width: float = 24
    bed_height: float = 12
    center_x: float = 12
    center_y: float = 6
    laser_config: list[int] = field(default_factory=list)
    can_control_air_assist: bool = False
    raw: dict = field(default_factory=dict, repr=False)

    @classmethod
    def from_json(cls, data: dict) -> MachineInfo:
        bed = data.get("bedSize", {})
        center = data.get("centerPoint", {})
        return cls(
            type=data.get("type", 0),
            bed_width=bed.get("x", 24),
            bed_height=bed.get("y", 12),
            center_x=center.get("x", 12),
            center_y=center.get("y", 6),
            laser_config=data.get("laserConfig", []),
            can_control_air_assist=data.get("canControlAirAssist", False),
            raw=data,
        )


@dataclass
class CameraFrame:
    """A single camera image with position metadata."""
    jpeg: bytes
    x: float
    y: float
    width: float
    height: float
    full_w: float
    full_h: float

    def save(self, path: str | Path) -> Path:
        """Write the JPEG data to *path* and return it."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(self.jpeg)
        return p

    @property
    def size_kb(self) -> float:
        return len(self.jpeg) / 1024

    @classmethod
    def from_message(cls, data: dict) -> CameraFrame | None:
        """Parse a WebSocket JSON message into a ``CameraFrame``."""
        jpeg = _extract_jpeg(data)
        if jpeg is None:
            return None
        return cls(
            jpeg=jpeg,
            x=data.get("x", 0),
            y=data.get("y", 0),
            width=data.get("width", 0),
            height=data.get("height", 0),
            full_w=data.get("fullW", 0),
            full_h=data.get("fullH", 0),
        )


# ---------------------------------------------------------------------------
# Main client
# ---------------------------------------------------------------------------

class EpilogCamera:
    """Async client for the Epilog eView camera system.

    Parameters
    ----------
    ip : str
        IP address of the laser (required).
    port : int
        HTTP / WebSocket port (default 80).
    force : bool
        When *True* (default) the server opens a fresh connection to the
        internal camera service.  Set to *False* to reuse the session
        already established by the Epilog Pulse web UI.
    """

    def __init__(self, ip: str, port: int = DEFAULT_PORT, *, force: bool = True):
        self.ip = ip
        self.port = port
        self.force = force

        port_str = f":{port}" if port != 80 else ""
        self._ws_url = f"ws://{ip}{port_str}/"
        self._http_url = f"http://{ip}{port_str}"
        self._headers = {
            "Origin": f"http://{ip}{port_str}",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/145.0.0.0 Safari/537.36"
            ),
        }
        self._ws: websockets.ClientConnection | None = None

    # -- context manager -----------------------------------------------------

    async def __aenter__(self) -> EpilogCamera:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    # -- HTTP ----------------------------------------------------------------

    async def get_info(self) -> MachineInfo:
        """Fetch machine info from the ``/info`` REST endpoint."""
        try:
            import httpx
        except ImportError as exc:
            raise ImportError("Install httpx for HTTP requests: pip install httpx") from exc

        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self._http_url}/info", timeout=5)
            resp.raise_for_status()
            return MachineInfo.from_json(resp.json())

    # -- WebSocket -----------------------------------------------------------

    def _build_uri(self) -> str:
        client_id = str(int(time.time() * 1000))
        force_str = "true" if self.force else "false"
        return f"{self._ws_url}?client={client_id}&force={force_str}"

    async def _connect(self, retries: int = 3, delay: float = 2.0) -> websockets.ClientConnection:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

        last_exc: Exception | None = None
        for attempt in range(1, retries + 1):
            uri = self._build_uri()
            try:
                self._ws = await websockets.connect(
                    uri, max_size=MAX_WS_SIZE, additional_headers=self._headers,
                )
                return self._ws
            except (websockets.exceptions.InvalidMessage, EOFError, ConnectionError, OSError) as exc:
                last_exc = exc
                if attempt < retries:
                    await asyncio.sleep(delay * attempt)

        raise ConnectionError(
            f"Could not connect to {self._ws_url} after {retries} attempts. "
            f"Make sure the laser is powered on and reachable. "
            f"Last error: {last_exc}"
        )

    async def snapshot(self, viewport: Viewport | None = None, *, timeout: float = 15) -> CameraFrame:
        """Capture a single camera frame and return it.

        Raises ``TimeoutError`` if no frame arrives within *timeout* seconds.
        """
        viewport = viewport or Viewport.full_bed()
        ws = await self._connect()
        await ws.send(json.dumps(viewport.to_dict()))
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        frame = CameraFrame.from_message(json.loads(raw))
        await self.close()
        if frame is None:
            raise RuntimeError(f"Server returned no image data: {raw[:300]}")
        return frame

    async def stream(
        self,
        viewport: Viewport | None = None,
        *,
        timeout: float = 15,
        on_frame: Callable[[CameraFrame], None] | None = None,
    ) -> AsyncIterator[CameraFrame]:
        """Yield camera frames continuously.

        Optionally calls *on_frame* for each frame (useful for side-effects
        like saving or displaying without breaking the async iteration).
        """
        viewport = viewport or Viewport.full_bed()
        ws = await self._connect()
        await ws.send(json.dumps(viewport.to_dict()))

        try:
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                frame = CameraFrame.from_message(json.loads(raw))
                if frame is None:
                    continue
                if on_frame is not None:
                    on_frame(frame)
                yield frame
        except asyncio.TimeoutError:
            return
        finally:
            await self.close()

    async def update_viewport(self, viewport: Viewport) -> None:
        """Send a new viewport to an already-open stream."""
        if self._ws is None:
            raise RuntimeError("No active WebSocket connection")
        await self._ws.send(json.dumps(viewport.to_dict()))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _extract_jpeg(message: dict) -> bytes | None:
    """Pull JPEG bytes out of a parsed WebSocket JSON message.

    The server sends ``image`` as a Node.js Buffer serialisation:
    ``{"type": "Buffer", "data": [255, 216, ...]}``.
    """
    img = message.get("image")
    if img is None:
        return None

    if isinstance(img, dict):
        data = img.get("data")
        if data is None:
            return None
        if isinstance(data, list):
            return bytes(data)
        if isinstance(data, str):
            return base64.b64decode(data)
        return data

    if isinstance(img, list):
        return bytes(img)
    if isinstance(img, (bytes, bytearray)):
        return bytes(img)
    return None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

async def _cli_info(cam: EpilogCamera) -> None:
    info = await cam.get_info()
    print(json.dumps(info.raw, indent=2))


async def _cli_probe(cam: EpilogCamera, viewport: Viewport) -> None:
    print("=== WebSocket Probe ===")
    ws = await cam._connect()
    print(f"Connected to {cam._ws_url}")

    print(f"Sending viewport: {viewport.to_dict()}")
    await ws.send(json.dumps(viewport.to_dict()))

    for i in range(3):
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=10)

            if isinstance(raw, bytes):
                print(f"\nMessage {i+1}: BINARY, {len(raw)} bytes")
                print(f"  First 100 bytes (hex): {raw[:100].hex()}")
                if raw[:2] == b"\xff\xd8":
                    print("  -> JPEG header detected")
            else:
                print(f"\nMessage {i+1}: TEXT, {len(raw)} chars")
                try:
                    parsed = json.loads(raw)
                    print(f"  JSON keys: {list(parsed.keys())}")
                    for k, v in parsed.items():
                        if k == "image" and isinstance(v, dict):
                            d = v.get("data")
                            print(f"  image.type: {v.get('type')}")
                            if isinstance(d, list):
                                print(f"  image.data: {len(d)} bytes, starts with {d[:10]}")
                        else:
                            print(f"  {k}: {v}")
                except json.JSONDecodeError:
                    print(f"  Raw (first 500): {raw[:500]}")

        except asyncio.TimeoutError:
            print(f"\nMessage {i+1}: TIMEOUT (10 s)")
            break

    await cam.close()
    print("\n=== Probe Complete ===")


async def _cli_snapshot(cam: EpilogCamera, viewport: Viewport, output: str | None) -> None:
    frame = await cam.snapshot(viewport)
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = output or f"camera_captures/capture_{ts}.jpg"
    saved = frame.save(path)
    print(f"Saved {frame.size_kb:.1f} KB  ({int(frame.full_w)}x{int(frame.full_h)})  ->  {saved}")


async def _cli_stream(
    cam: EpilogCamera,
    viewport: Viewport,
    *,
    display: bool,
    save: bool,
    max_frames: int | None,
) -> None:
    if display:
        try:
            import cv2
            import numpy as np
        except ImportError:
            print("OpenCV required for --display: pip install opencv-python")
            display = False

    out_dir = Path("camera_captures")
    if save:
        out_dir.mkdir(exist_ok=True)

    count = 0
    print("Streaming... (Ctrl+C to stop)\n")
    try:
        async for frame in cam.stream(viewport):
            count += 1
            print(f"  frame {count:>5}  {frame.size_kb:>7.1f} KB  {int(frame.full_w)}x{int(frame.full_h)}")

            if save:
                frame.save(out_dir / f"frame_{count:05d}.jpg")

            if display:
                arr = np.frombuffer(frame.jpeg, dtype=np.uint8)
                img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if img is not None:
                    cv2.imshow("Epilog eView Camera", img)
                    if cv2.waitKey(1) & 0xFF == ord("q"):
                        break

            if max_frames and count >= max_frames:
                break
    except KeyboardInterrupt:
        pass

    print(f"\n{count} frames captured.")
    if display:
        import cv2
        cv2.destroyAllWindows()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="epilog_camera",
        description="Capture camera images from an Epilog Fusion laser cutter.",
    )
    parser.add_argument("--config", type=Path, default=CONFIG_FILE, help="Path to config JSON file (default: %(default)s)")
    parser.add_argument("--ip", help="Laser IP address (overrides config file)")
    parser.add_argument("--port", type=int, help="HTTP port (overrides config file)")
    parser.add_argument("--no-force", action="store_true", help="Reuse existing camera session instead of starting a new one")

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("info", help="Print machine info as JSON")
    sub.add_parser("probe", help="Diagnostic WebSocket probe (3 messages)")

    snap = sub.add_parser("snapshot", help="Capture a single frame")
    snap.add_argument("-o", "--output", help="Output file path")

    stream = sub.add_parser("stream", help="Continuous frame capture")
    stream.add_argument("--display", action="store_true", help="Show live OpenCV window (q to quit)")
    stream.add_argument("--no-save", action="store_true", help="Don't write frames to disk")
    stream.add_argument("--max-frames", type=int, help="Stop after N frames")

    for p in (snap, stream, sub.choices["probe"]):
        p.add_argument("--x", type=float, default=0, help="Viewport X origin in inches")
        p.add_argument("--y", type=float, default=0, help="Viewport Y origin in inches")
        p.add_argument("--w", type=float, default=24, help="Viewport width in inches")
        p.add_argument("--h", type=float, default=12, help="Viewport height in inches")

    return parser


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    ip = args.ip or cfg["ip"]
    port = args.port or cfg.get("port", DEFAULT_PORT)

    cam = EpilogCamera(ip, port, force=not args.no_force)
    vp = Viewport(
        getattr(args, "x", 0),
        getattr(args, "y", 0),
        getattr(args, "w", 24),
        getattr(args, "h", 12),
    )

    if args.command == "info":
        asyncio.run(_cli_info(cam))
    elif args.command == "probe":
        asyncio.run(_cli_probe(cam, vp))
    elif args.command == "snapshot":
        asyncio.run(_cli_snapshot(cam, vp, args.output))
    elif args.command == "stream":
        asyncio.run(_cli_stream(cam, vp, display=args.display, save=not args.no_save, max_frames=args.max_frames))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
