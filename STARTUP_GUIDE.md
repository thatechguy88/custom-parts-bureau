# E2E Webhook & Sandbox Startup Guide

This guide will help you start your local environment and configure your tunnels to achieve End-to-End integration with the NemoHermes sandbox.

## 1. Start the Tunnels and Update Environment

We've provided a helper script that automatically spins up the required tunnels (Ngrok and Cloudflare) and registers the NemoClaw proxy policy. 

Run the following command in your terminal:
```bash
./start_tunnels.sh
```

**What this does:**
1. Starts `ngrok` on port 5001.
2. Starts `nemoclaw tunnel` to accept external webhooks into the sandbox.
3. Automatically writes the resulting `NGROK_URL` and `NEMOCLAW_WEBHOOK_URL` to your `.env` file.
4. Adds the ngrok network policy to NemoClaw so the agent is allowed to send callbacks.

> [!TIP]
> Wait a few seconds for the script to print out the final environment URLs. Verify they have been added to your `.env` file.

## 2. Start the Flask Application

Once the tunnels are running, start the primary Flask app on your host machine:

```bash
./start.sh
```
This will start the local web server on port 5001.

## 3. Configure the NemoHermes Sandbox Agent

The Hermes agent inside the NemoClaw sandbox needs precise instructions on how to evaluate jobs and return the decision over the callback URL.

1. Open your NemoHermes interface.
2. Open the `agent.md` file from this repository.
3. Copy the **entire contents** of `agent.md` and paste it as the agent's main system prompt or instructions.

## 4. Run an E2E Test

1. Open your browser and navigate to `http://localhost:5001`
2. Upload an STL file through the form.
3. The Flask app will analyze it and send a webhook to NemoClaw.
4. You should see the Hermes agent wake up, review the data, and execute the Python script.
5. The agent will post a callback to the Flask app via Ngrok.
6. The Flask UI will update from "analyzing" to "quoted" or "rejected".
