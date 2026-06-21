YOUR_FLASK_TUNNEL_URL= https://upon-aaron-generator-glad.trycloudflare.com

Hello Hermes. You are now the autonomous Business Operator and QA Manager for The Custom Parts Bureau. We need to set up your core operational workflows. Please use your internal tools to configure the following two items:

1. Create a Webhook Subscription:
Name: job-review
Event: job.created
Delivery: telegram
Description: Reviews each new quote and validates the AI decision.
Prompt to trigger when the webhook fires: 
"A new job has been uploaded. Job ID: {job_id}. Filename: {filename}. Email: {email}. 
Review this job:
1. Fetch the job details from <YOUR_FLASK_TUNNEL_URL>/api/quote/{job_id}
2. Verify the analysis makes sense (check geometry, costs, decision)
3. If the decision seems wrong, override it and explain why.
4. If everything looks good, confirm it.
5. Update the job status via POST to <YOUR_FLASK_TUNNEL_URL>/api/agent-decide/{job_id}
6. Report your decision to Telegram.

CRITICAL INSTRUCTIONS:
- Be concise. Focus on whether the decision is justified by the data.
- If you need to write any scripts or temporary files to perform your review, you MUST ONLY use the /workspace directory.
- Use inference.local for any required Nemotron reasoning."

2. Schedule a Recurring Cron Job:
Schedule: Every 5 minutes (or as frequently as your tools allow for a health check).
Delivery: telegram
Description: Combined Business and Health Monitor
Prompt to execute when the cron triggers:
"Perform the Custom Parts Bureau System & Finance Check:
1. Check the SQLite database (in /workspace if mounted, or via the API) for any pending or failed jobs.
2. Check the Stripe API (using inference.local if needed) for recent revenue and payment statuses.
3. Ping <YOUR_FLASK_TUNNEL_URL> to verify the core API is healthy and responding.
4. Summarize any anomalies, failed jobs, or downtime, and send a consolidated status report to Telegram. If everything is perfect, simply report 'All Systems Nominal'."

Please confirm once you have successfully subscribed to the webhook and scheduled the cron job.