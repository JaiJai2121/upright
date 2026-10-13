# Deploy Upright as a website

This project is a web app, not a static-only page. The server needs to stay running to record lives, so deploy it as a Docker web service.

## Deploy on Render

1. Create a private GitHub repository and upload the contents of this folder (`upright/`) to it.
2. In Render, choose **New → Blueprint** and connect that repository. Render will read `render.yaml`.
3. When prompted, set `APP_PASSWORD` to a strong, unique password. Do not leave it blank.
4. Deploy. Once the build finishes, open the `onrender.com` URL Render gives you and sign in using any username and the password you set.
5. The blueprint requests a persistent 10 GB disk mounted at `/data`, where recordings are stored. Confirm the plan and disk pricing in Render before deploying.

## Run locally

Install Python 3.10+, install FFmpeg (both `ffmpeg` and `ffprobe` must be on PATH), then run:

```bash
pip install -r requirements.txt
python app.py
```

Open `http://localhost:8000`.

## Important

- Keep `APP_PASSWORD` secret. Anyone who can access the app can start and stop recordings and manage saved files.
- The app uses unofficial TikTok endpoints; they may change or be blocked by hosting providers.
- Record only lives you have permission to record, and respect creators' rights and TikTok's terms.
- A persistent disk and an always-on service are important for long recordings. Check your hosting provider's current plan, disk, and bandwidth costs.
