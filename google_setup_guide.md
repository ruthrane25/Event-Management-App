# Google Login Setup Guide for EventFlow

To enable Google Login/Sign-up in EventFlow, follow these steps to set up a project in the Google Cloud Console.

## Step 1: Create a Google Cloud Project
1. Go to the [Google Cloud Console](https://console.cloud.google.com/).
2. Click on the project dropdown at the top and select **"New Project"**.
3. Name your project (e.g., `EventFlow`) and click **"Create"**.

## Step 2: Configure OAuth Consent Screen
1. In the left sidebar, navigate to **APIs & Services > OAuth consent screen**.
2. Select **"External"** user type and click **"Create"**.
3. Fill in the required fields:
   - **App name**: `EventFlow`
   - **User support email**: Your email address
   - **Developer contact info**: Your email address
4. Click **"Save and Continue"** through the Scopes and Test Users screens.
5. On the Summary screen, click **"Back to Dashboard"**.
6. **Important**: Click **"Publish App"** to move it out of testing mode (otherwise only test users can login).

## Step 3: Create OAuth 2.0 Credentials
1. Navigate to **APIs & Services > Credentials**.
2. Click **"+ Create Credentials"** and select **"OAuth client ID"**.
3. Select **"Web application"** as the Application type.
4. Name it (e.g., `EventFlow Web Client`).
5. Under **Authorized redirect URIs**, click **"+ Add URI"** and enter:
   `http://127.0.0.1:5000/google-callback`
6. Click **"Create"**.
7. A modal will appear showing your **Client ID** and **Client Secret**. Copy these!

## Step 4: Configure the App
1. Create a file named `.env` in the root of your project (copy from `.env.example`).
2. Paste your credentials into the `.env` file:
   ```env
   GOOGLE_CLIENT_ID=your-client-id-here
   GOOGLE_CLIENT_SECRET=your-client-secret-here
   ```

## Step 5: How to Run
Since the app currently uses `os.environ.get`, you can run it by setting the environment variables in your terminal:

### Preparation: Install Dependencies
Open your terminal in the project folder and run:
```bash
pip install python-dotenv
```

### PowerShell (Windows):
```powershell
$env:GOOGLE_CLIENT_ID="your-id"
$env:GOOGLE_CLIENT_SECRET="your-secret"
python app.py
```

### CMD (Windows):
```cmd
set GOOGLE_CLIENT_ID=your-id
set GOOGLE_CLIENT_SECRET=your-secret
python app.py
```

---
**Note:** For a more permanent solution, you can install `python-dotenv` and add `from dotenv import load_dotenv; load_dotenv()` to the top of `app.py`.
