# epilog_camera

Capture live camera images from Epilog Fusion laser cutters over the network.

The [Epilog Camera Module](https://www.epiloglaser.com/assets/downloads/camera-calibration.pdf) uses one overhead cameras in the lid to photograph the laser bed. This library talks to the laser's built-in web server and streams those images back as JPEG frames.

> Protocol reverse-engineered from the Epilog Pulse web interface.

## Quick start

```bash
pip install -r requirements.txt

# Copy the example config and fill in your laser's IP
cp epilog_config.example.json epilog_config.json
```

Edit `epilog_config.json` with your laser's IP address:

```json
{
  "ip": "192.168.1.100",
  "port": 80
}
```

Then run any command:

```bash
python epilog_camera.py snapshot                  # single photo
python epilog_camera.py stream --max-frames 10    # continuous capture
python epilog_camera.py info                      # machine info
```

## Configuration

Connection settings are stored in `epilog_config.json`, which is gitignored so your IP stays out of version control.

An example template is provided as `epilog_config.example.json`.


| Key    | Required | Default | Description             |
| ------ | -------- | ------- | ----------------------- |
| `ip`   | yes      | --      | Your laser's IP address |
| `port` | no       | `80`    | HTTP / WebSocket port   |


You can override the config file location or individual values from the CLI:

```bash
python epilog_camera.py --config /path/to/config.json snapshot
python epilog_camera.py --ip 10.0.0.50 snapshot
```

## Requirements


| Package                      | Purpose                              |
| ---------------------------- | ------------------------------------ |
| `websockets`                 | WebSocket client for camera stream   |
| `httpx`                      | HTTP client for `/info` endpoint     |
| `opencv-python` *(optional)* | Live display window with `--display` |


```bash
pip install websockets httpx
# optional
pip install opencv-python
```

## CLI reference

```
epilog_camera [-h] [--config PATH] [--ip IP] [--port PORT] [--no-force]
              {info,probe,snapshot,stream}
```

### Global options

| Flag         | Default              | Description                                                    |
| ------------ | -------------------- | -------------------------------------------------------------- |
| `--config`   | `epilog_config.json` | Path to config file                                            |
| `--ip`       | *(from config)*      | Laser IP address (overrides config)                            |
| `--port`     | *(from config)*      | HTTP / WebSocket port (overrides config)                       |
| `--no-force` | off                  | Reuse an existing camera session instead of starting a new one |

### `info`

Print machine metadata as JSON.

```bash
python epilog_camera.py info
# {"type": 4, "bedSize": {"x": 24, "y": 12}, ...}
```

### `probe`

Connect to the WebSocket, send a viewport, and dump the first three raw messages. Useful for debugging connection issues.

```bash
python epilog_camera.py probe
python epilog_camera.py probe --x 6 --y 3 --w 12 --h 6
```

| Flag   | Default | Description                    |
| ------ | ------- | ------------------------------ |
| `--x`  | `0`     | Viewport X origin (inches)     |
| `--y`  | `0`     | Viewport Y origin (inches)     |
| `--w`  | `24`    | Viewport width (inches)        |
| `--h`  | `12`    | Viewport height (inches)       |

### `snapshot`

Capture a single JPEG frame and save it.

```bash
python epilog_camera.py snapshot
python epilog_camera.py snapshot -o my_photo.jpg
python epilog_camera.py snapshot --x 6 --y 3 --w 12 --h 6   # center quadrant
```

| Flag           | Default | Description                    |
| -------------- | ------- | ------------------------------ |
| `-o, --output` | auto    | Output file path (auto-generates a timestamped name in `camera_captures/`) |
| `--x`          | `0`     | Viewport X origin (inches)     |
| `--y`          | `0`     | Viewport Y origin (inches)     |
| `--w`          | `24`    | Viewport width (inches)        |
| `--h`          | `12`    | Viewport height (inches)       |

### `stream`

Capture frames continuously until stopped with Ctrl+C (or `q` in the display window). Images are written to `camera_captures/`.

```bash
python epilog_camera.py stream
python epilog_camera.py stream --display           # live OpenCV window
python epilog_camera.py stream --max-frames 20     # stop after 20 frames
python epilog_camera.py stream --no-save           # display only, don't save files
```

| Flag             | Default | Description                                   |
| ---------------- | ------- | --------------------------------------------- |
| `--display`      | off     | Show live preview in an OpenCV window (q to quit) |
| `--no-save`      | off     | Don't write frames to disk                    |
| `--max-frames N` | --      | Stop after N frames                           |
| `--x`            | `0`     | Viewport X origin (inches)                    |
| `--y`            | `0`     | Viewport Y origin (inches)                    |
| `--w`            | `24`    | Viewport width (inches)                       |
| `--h`            | `12`    | Viewport height (inches)                      |

## Library usage

`epilog_camera` is a single-file module you can import directly. The `ip` parameter is always required when using the library — there is no default.

### Snapshot

```python
import asyncio
from epilog_camera import EpilogCamera

async def main():
    async with EpilogCamera("192.168.1.100") as cam:
        frame = await cam.snapshot()
        frame.save("bed.jpg")
        print(f"{frame.size_kb:.0f} KB, {int(frame.full_w)}x{int(frame.full_h)}")

asyncio.run(main())
```

### Stream

```python
import asyncio
from epilog_camera import EpilogCamera, Viewport

async def main():
    cam = EpilogCamera("192.168.1.100")

    async for frame in cam.stream(Viewport(0, 0, 24, 12)):
        frame.save(f"frames/{int(frame.x)}_{int(frame.y)}.jpg")
        # break whenever you like

asyncio.run(main())
```

### Machine info

```python
import asyncio
from epilog_camera import EpilogCamera

async def main():
    async with EpilogCamera("192.168.1.100") as cam:
        info = await cam.get_info()
        print(f"Bed: {info.bed_width}\" x {info.bed_height}\"")

asyncio.run(main())
```

### Load IP from config

```python
from epilog_camera import EpilogCamera, load_config

cfg = load_config()
cam = EpilogCamera(cfg["ip"], cfg.get("port", 80))
```

### Custom viewport

Viewports are in inches, matching the bed coordinate system:

```python
from epilog_camera import Viewport

full_bed = Viewport.full_bed()           # 0, 0, 24, 12
center   = Viewport(6, 3, 12, 6)        # center quadrant
top_left = Viewport(0, 0, 12, 6)        # top-left quarter
```

### Key classes


| Class          | Description                                             |
| -------------- | ------------------------------------------------------- |
| `EpilogCamera` | Main async client -- connects, captures, streams        |
| `CameraFrame`  | JPEG bytes + position metadata, with a `.save()` method |
| `Viewport`     | Bed region in inches (`x`, `y`, `w`, `h`)               |
| `MachineInfo`  | Parsed machine info (bed size, laser config, etc.)      |


## Protocol details

The Epilog Fusion runs an [Express](https://expressjs.com/) web server on port 80. The camera works through a WebSocket sitting on the same port.

### Architecture

```
Your code   ──WebSocket──▶   Express (port 80)   ──TCP──▶   ODROID camera service (127.0.0.1:9090)
                              on the laser                    internal to the laser
```

The ODROID C1 board inside the lid drives the two USB cameras. The Express server proxies camera data to WebSocket clients.

### WebSocket protocol

1. **Connect** to `ws://<ip>/?client=<timestamp_ms>&force=<true|false>`
  - `client` -- a unique session ID (millisecond timestamp works fine)
  - `force=true` -- start a fresh camera connection (use for standalone scripts)
  - `force=false` -- piggyback on the session the Epilog Pulse web UI already opened
2. **Send** a viewport as JSON on connect:
  ```json
   {"x": 0, "y": 0, "w": 24, "h": 12}
  ```
3. **Receive** JSON messages continuously:
  ```json
   {
     "image": {"type": "Buffer", "data": [255, 216, 255, ...]},
     "x": 0, "y": 0,
     "width": 1440, "height": 720,
     "fullW": 1440, "fullH": 720
   }
  ```
  - `image.data` is a JPEG file as a byte-value array (Node.js `Buffer` serialisation)
  - Coordinates and dimensions are in pixels
4. **Send** updated viewport JSON at any time to pan / zoom.

### Required headers

The server checks the `Origin` header. Set it to `http://<laser_ip>`.

### REST endpoints


| Method | Path        | Response                                    |
| ------ | ----------- | ------------------------------------------- |
| GET    | `/info`     | Machine type, bed size, laser config        |
| GET    | `/fonts`    | Available fonts                             |
| GET    | `/licenses` | OSS license info                            |
| POST   | `/job`      | Submit a `.prn` print file (multipart form) |


## Tested on

- Epilog Fusion Maker 12 (24" x 12" bed, type 4)
- Epilog Pulse firmware (Express-based web UI)
- Python 3.12+ / Windows 11

