# MongoDB Atlas Setup Guide

This guide will walk you through creating a MongoDB database on Atlas, getting your connection string, and configuring it for both your local environment and Vercel.

## Step 1: Create a Cluster on MongoDB Atlas
1. Log in to your [MongoDB Atlas](https://www.mongodb.com/cloud/atlas) account.
2. If you don't have a project yet, click **New Project**, name it something like `EventFlow`, and create it.
3. Click the **Build a Database** button.
4. Choose the **M0 Free** (Shared) tier.
5. Provide a cluster name (e.g., `Cluster0`) and click **Create**.

## Step 2: Configure Database User & Network
1. **Security Quickstart:** Atlas will ask you how you would like to authenticate your connection. 
2. **Create a User:** Enter a **Username** and a **Password**. 
   * *Important: Copy this password and save it somewhere temporarily. You will need it for the connection string.*
3. Click **Create User**.
4. **Network Access / IP Whitelist:** Atlas will ask where you are connecting from.
5. In the "IP Address" field, you need to allow access from anywhere so that Vercel can connect to it. Enter `0.0.0.0/0` (or select "Allow access from anywhere").
6. Click **Add Entry** and then **Finish and Close**.

## Step 3: Get Your Connection String
1. Go back to your **Database** deployment screen.
2. Click the **Connect** button next to your cluster name.
3. Select **Drivers** (Connect your application).
4. Under "Driver", choose **Python** and version **3.6 or later**.
5. Copy the connection string provided. It should look something like this:
   `mongodb+srv://<username>:<password>@cluster0.abcd1.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0`

## Step 4: Configure Local `.env`
1. Open your `.env` file in VS Code.
2. Add a new line:
   ```env
   DATABASE_URL=mongodb+srv://<username>:<your_actual_password>@cluster0.abcd1.mongodb.net/event_app?retryWrites=true&w=majority
   ```
   * **Note 1:** Replace `<username>` with the username you created.
   * **Note 2:** Replace `<password>` with the password you copied earlier (remove the `< >` brackets).
   * **Note 3:** Notice that I added `event_app` right after the `.net/` before the `?` mark. This tells MongoDB to create/use a database named "event_app".

## Step 5: Configure Vercel
1. Go to your [Vercel Dashboard](https://vercel.com/dashboard).
2. Click on your Event Management project.
3. Go to **Settings** -> **Environment Variables**.
4. Add a new variable:
   * **Key:** `DATABASE_URL`
   * **Value:** The exact connection string you put in your `.env` file.
5. Click **Save**.
6. **Important:** Vercel only injects new environment variables at build time. Go to your **Deployments** tab, click the 3 dots on your latest deployment, and click **Redeploy**.

## Step 6: Initialize the Database
Whether running locally or on Vercel, when you boot up the app for the first time with the new MongoDB, go to:
`http://localhost:5000/init-db` (or `https://your-vercel-domain.vercel.app/init-db`).

This will create the necessary unique indexes for `email` and `unique_code`.
