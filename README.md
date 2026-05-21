# Edgewood-Business-Gradebook-Tracker

This is the Edgewood Business Gradebook Tracker application.

## Local Development
1. Install requirements:
   ```bash
   pip install -r requirements.txt
   ```
2. Configure `.env` file with Canvas credentials:
   ```env
   CANVAS_API_URL=https://edgewood.instructure.com/api/v1
   CANVAS_ACCESS_TOKEN=your_token
   CANVAS_FALLBACK_TOKEN=your_fallback_token
   FLASK_SECRET_KEY=your_secret_key
   ```
3. Run the application:
   ```bash
   python app.py
   ```

## Render / Vercel Deployment
- **Build Command**: `pip install -r requirements.txt`
- **Start Command (Render)**: `gunicorn app:app`
