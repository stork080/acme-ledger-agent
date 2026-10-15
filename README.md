# Acme Appliances Ledger Agent

FastAPI service that answers plain-English questions about the assigned order ledger.

## Local setup

1. Create a virtual environment and install packages:

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. Set `SOURCE_URL` to the full personalized `questionData` URL from the assignment. It must be the root URL ending in `path=/` (not the `/orders` URL).

   ```bash
   export SOURCE_URL='PASTE_THE_FULL_ASSIGNED_questionData_URL_HERE'
   ```

3. Start the server:

   ```bash
   uvicorn app:app --reload
   ```

4. Test:

   ```bash
   curl -X POST http://127.0.0.1:8000/ \
     -H 'Content-Type: application/json' \
     -d '{"question":"What was the total revenue in USD from the North region in March 2026?"}'
   ```

The first request loads the API root, bulk NDJSON export, and currency rates. The data is cached for the lifetime of the Python process.

## Deploy on Vercel

1. Push these files to a GitHub repository.
2. Import the repository at https://vercel.com/new.
3. In Project Settings → Environment Variables, add `SOURCE_URL` with the full personalized root URL from the assignment.
4. Redeploy after adding the environment variable.
5. The public endpoint is `https://YOUR-VERCEL-DOMAIN/`. It accepts `POST` JSON:

   ```json
   {"question":"What was the total revenue in USD from the North region in March 2026?"}
   ```

## Notes

- Duplicate order IDs are deduplicated using the latest `updated_at`.
- Revenue uses only current orders whose status is `paid`.
- Currency amounts are converted to USD using the API's `/rates` data.
- Order dates are interpreted in Asia/Kolkata.
- The answer engine uses deterministic keyword and aggregation logic; test it against the question types in your assignment before submitting.
