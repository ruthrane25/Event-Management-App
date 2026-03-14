# SMTP Setup Guide for EventFlow

This guide explains how to configure your email settings so the app can send **OTP verification codes** and **notifications** automatically.

## 1. Get a Gmail App Password (Recommended)

Since Google no longer supports simple password login for third-party apps, you must use an **App Password**.

### Step-by-Step:
1.  Go to your [Google Account](https://myaccount.google.com/).
2.  Enable **2-Step Verification** in the **Security** tab if you haven't already.
3.  Search for "App passwords" in the search bar or go to [App passwords](https://myaccount.google.com/apppasswords).
4.  Enter a name like "EventFlow App" and click **Create**.
5.  **Copy the 16-digit code** shown in the yellow box. This is your SMTP password.

---

## 2. Update your `.env` File

Open the `.env` file in your project folder and add/update these lines with your details:

```env
# Email Settings (Gmail Example)
MAIL_SERVER=smtp.gmail.com
MAIL_PORT=587
MAIL_USERNAME=your-email@gmail.com
MAIL_PASSWORD=your-16-digit-app-password
```

> [!IMPORTANT]
> Make sure there are no spaces around the `=` signs and no quotes around the values unless they contain spaces.

---

## 3. How it Works
The app uses these settings for both:
- **Registration OTP**: Sent automatically when someone clicks "Register".
- **Notifications**: Sent when an Admin messages a member or when a member messages an Admin.

## Troubleshooting
- **Connection Error**: Ensure `MAIL_PORT` is `587` and your firewall allows outgoing SMTP.
- **Authentication Error**: Triple-check that your **App Password** is correct (it should not have spaces when pasted into `.env`).
