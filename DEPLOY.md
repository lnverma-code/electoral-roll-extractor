# Deploying to your Hostinger VPS (Coolify)

Your server already runs **Coolify 4.1.2** on Ubuntu 24.04:

- VPS: `srv1806831.hstgr.cloud` · IP `200.97.160.149`
- Coolify dashboard: `http://200.97.160.149:8000`

This app ships with a `Dockerfile`, so Coolify builds and runs it for you and
gives you an HTTPS link automatically. Pick **Option A** (Git, recommended) or
**Option B** (no Git, straight Docker on the box).

---

## Option A — Deploy from Git via Coolify (recommended)

**1. Put this folder in a Git repo** (from the project directory):

```bash
git init && git add . && git commit -m "Electoral roll extractor"
# create an empty repo on GitHub/GitLab, then:
git remote add origin <your-repo-url>
git push -u origin main
```

`.env` is gitignored, so your API key is NOT pushed. Good.

**2. In the Coolify dashboard** (`http://200.97.160.149:8000`):

1. **+ New** → **Resource** → **Public Repository** (or Private + connect your
   Git account).
2. Paste the repo URL. Coolify auto-detects the **Dockerfile** — leave build
   pack as *Dockerfile*.
3. **Ports**: set the exposed port to **8501**.
4. **Environment variables** → add:
   - `MISTRAL_API_KEY` = your key
   - `APP_USERNAME` = the login username you want
   - `APP_PASSWORD_HASH` = output of `python make_password.py` (**required** —
     the app refuses to serve any data without it)
   - (optional) `OCR_MODEL`, `STRUCTURE_MODEL` — defaults are fine.

   > **Never put a `$` in an environment variable value here.** Coolify feeds
   > these through Docker Compose, which expands `$name` as a variable and
   > silently deletes it. That is why `APP_PASSWORD_HASH` is `:`-separated.
   > A truncated hash matches no password, and every login fails with the
   > misleading message "Invalid credentials".
5. **Deploy**. Coolify builds the image and starts it.
6. Under **Domains**, Coolify gives you a free `*.sslip.io` HTTPS URL (or set
   your own domain). **That URL is your link.**

Redeploys: `git push` → click **Redeploy** (or enable auto-deploy webhook).

---

## Option B — No Git, run Docker directly on the VPS

SSH into the box yourself (I can't log in on your behalf), then:

```bash
# copy the project up (run from your Mac, in the project folder)
scp -r . root@200.97.160.149:/opt/electoral-roll

# on the VPS
cd /opt/electoral-roll
echo "MISTRAL_API_KEY=your_key_here" > .env
docker compose up -d --build
```

App is then at `http://200.97.160.149:8501`. Put it behind Coolify's proxy or a
reverse proxy for HTTPS.

---

## After deploy

- Open the URL, upload a PDF, click **Convert & build ZIP**, download the ZIP
  (PDF + Excel + `photos/`).
- Large PDFs (> 25 pages) are OCR'd in 15-page batches automatically.
- **Rotate the root password you shared** — treat it as compromised.
