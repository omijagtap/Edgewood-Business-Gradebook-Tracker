# Edgewood-Education-discussion-tracker

This is the Edgewood Education Discussion Tracker application.

## Local Development
1. Install requirements:
   ```bash
   pip install -r requirements.txt
   ```
2. Run the application:
   ```bash
   python app.py
   ```

## Render Deployment
Deployed using Flask and Gunicorn.
- **Build Command**: `pip install -r requirements.txt`
- **Start Command**: `gunicorn app:app`
