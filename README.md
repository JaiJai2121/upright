# Upright

Upright is a browser-based TikTok LIVE recorder with a dark interface. The app needs a backend server and FFmpeg, so it must be deployed as a Docker web service rather than uploaded as static HTML.

## Deploy without using a terminal

Follow **START-HERE.txt**. It walks through uploading this folder to a private GitHub repository and deploying it with the included `render.yaml` Blueprint on Render.

## Included deployment support

- `Dockerfile`: installs Python dependencies and FFmpeg inside the hosted container.
- `render.yaml`: configures the web service and persistent recording disk.
- `app.py`, `recorder.py`, `tiktok.py`: application backend.
- `static/`: browser interface.

Review Render's current service and persistent-disk pricing before deploying. Set a strong `APP_PASSWORD` during setup.
