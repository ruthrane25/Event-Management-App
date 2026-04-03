# EventFlow - Event Management App

EventFlow is a comprehensive web-based platform for creating, planning, and managing events seamlessly. Built with Flask and MongoDB, it helps users handle everything from guest lists and RSVPs to travel arrangements and expenses. EventFlow offers robust features including secure authentication, role-based access, automated email notifications, and an intuitive dashboard.

## Features

- **Authentication & Security:**
  - Standard Sign-Up/Log-In using secure password hashing (`Flask-Bcrypt`).
  - Email Verification via OTP (One Time Password).
  - Google OAuth integration for quick Sign-in.
  - Built-in CSRF protection (`Flask-WTF`) and security headers (`Flask-Talisman`).
  - Rate limiting to prevent abuse (`Flask-Limiter`).

- **Event Management:**
  - Create custom events with a unique, shareable join code.
  - Centralized **Event Dashboard** to view overviews and manage operations.
  - Role-based access control (Admin, Manager, Member).

- **Planning Tools:**
  - **Guests:** Manage guests, families, meal preferences, and RSVPs. Export guest lists to CSV.
  - **Expenses:** Track financial costs and budgets related to the event.
  - **Itinerary:** Plan and share timelines for your events.
  - **Stay & Travel:** Organize accommodation and transportation for attendees.
  - **Tasks:** Assign, organize, and track to-do items among managers.

- **Notifications:**
  - Automated transactional emails via SMTP for invites, join requests, approval states, and OTPs.
  
- **PWA Support:**
  - Includes a `manifest.json` and a Service Worker (`sw.js`) for a basic Progressive Web App experience.

## Tech Stack

- **Backend:** Python, Flask, PyMongo.
- **Frontend:** HTML, CSS, JavaScript (Vanilla), Jinja2 Templating.
- **Database:** MongoDB (optimized for serverless environments).
- **Hosting:** Ready to be deployed to Vercel (includes `vercel.json`).

## Setup and Installation

### Prerequisites
- Python 3.8+
- MongoDB instance (local or Atlas)
- An SMTP server for emails (e.g., SendGrid, Mailchimp, or Gmail SMTP)
- Google Cloud Console Project (for Google Auth OAuth 2.0 Credentials)

### 1. Clone the repository
```bash
git clone <repository_url>
cd "Event management app"
```

### 2. Install dependencies
```bash
pip install -r requirements.txt
```

### 3. Environment Variables
Create a `.env` file in the root directory and populate it with the following keys:
```env
# Flask Application Secret Key
SECRET_KEY=your_secure_secret_key_here

# MongoDB Connection String
DATABASE_URL=mongodb+srv://<username>:<password>@cluster.mongodb.net/event_app

# SMTP Configuration
MAIL_SERVER=smtp.example.com
MAIL_PORT=587
MAIL_USERNAME=your_email@example.com
MAIL_PASSWORD=your_email_app_password

# Google OAuth Configuration
GOOGLE_CLIENT_ID=your_google_client_id
GOOGLE_CLIENT_SECRET=your_google_client_secret

# Optional override (Vercel automatic proxy fix usually handles this)
# GOOGLE_REDIRECT_URI=http://localhost:5000/google-callback
```
*Please refer to `SMTP_SETUP_GUIDE.md`, `google_setup_guide.md`, and `mongodb_setup_guide.md` for detailed instructions on configuring these external services properly.*

### 4. Initialize Database Indexes
If this is the first time running the application, you need to configure the unique indexes in your MongoDB instance. Start the application and visit `/init-db` in your browser.

### 5. Run the application locally
```bash
flask run
```
The app will be accessible at `http://127.0.0.1:5000`.

## Deployment

This app is pre-configured and optimized for deployment on **Vercel** serverless environments.
- The `vercel.json` configuration points Vercel to `app.py`.
- Vercel's `certifi` handling for PyMongo is included.
- Make sure to add all `.env` variables to your Vercel Project Settings.

## Project Structure

- `app.py`: Main Flask application containing all routes, controllers, and core logic.
- `requirements.txt`: Python dependencies.
- `templates/`: Jinja2 HTML views for every aspect of the app (dashboard, expenses, guests, etc.).
- `static/`: Frontend assets (JavaScript, CSS, fonts, PWA assets).
- `instance/`: Flask instance directory used for local context.
