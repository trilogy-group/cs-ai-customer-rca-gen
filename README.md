## RCA Customer-Facing Draft Automation

This project receives a Notion Database Automation webhook when an RCA page's **Status** becomes **Awaiting QC**, then:

- Reads the RCA page content from Notion
- Uses **OpenAI `gpt-5.2`** to generate a customer-facing RCA (JSON)
- Creates a **child page** under the RCA with the customer-facing RCA
- Appends a **Customer-Facing RCA Draft** section + a link to the child page at the end of the parent RCA page
- Sets the `Customer RCA Doc` URL property on the parent page
- On re-trigger (rejected -> updated -> Awaiting QC again), regenerates a new child page and updates the link

---

## Production (AWS)

The automation runs on AWS Lambda behind API Gateway. No local server or ngrok needed.

**Production webhook URL:**

```
https://py2h3cxzsc.execute-api.us-east-1.amazonaws.com/notion-webhook
```

### Architecture

- **API Gateway HTTP API** receives the POST from Notion
- **Lambda (gateway phase)** validates the payload, returns `200 OK` immediately, and fires an async self-invocation
- **Lambda (process phase)** runs the full RCA generation pipeline (OpenAI + Notion API calls, ~30-40 seconds)
- **Secrets Manager** stores `NOTION_API_TOKEN` and `OPENAI_API_KEY` (secret name: `rca-automation/api-keys`)

### AWS Resources

| Resource | Name / ID |
|---|---|
| Lambda Function | `rca-automation-webhook` |
| IAM Role | `rca-automation-lambda-role` |
| API Gateway | `rca-automation-api` (ID: `py2h3cxzsc`) |
| Secrets Manager | `rca-automation/api-keys` |
| Region | `us-east-1` |

### Manual test (production)

```bash
curl -s -X POST "https://py2h3cxzsc.execute-api.us-east-1.amazonaws.com/notion-webhook" \
  -H "Content-Type: application/json" \
  -d '{"data":{"id":"<NOTION_PAGE_ID>"}}'
```

### Updating the Lambda code

```bash
cd rca-automation
cp lambda_handler.py rca_notion_ops.py rca_generator.py lambda_build/
cp -r prompts lambda_build/
cd lambda_build && zip -r9 ../lambda_package.zip . -x "*.pyc" "__pycache__/*" "*.dist-info/*"
cd ..
aws lambda update-function-code \
  --function-name rca-automation-webhook \
  --zip-file fileb://lambda_package.zip \
  --region us-east-1 \
  --profile devconnect-saml
```

### Viewing logs

```bash
aws logs tail /aws/lambda/rca-automation-webhook --follow --profile devconnect-saml --region us-east-1
```

---

## Local Development

### Files

- `app.py`: Flask server for local development (uses `token.json`)
- `lambda_handler.py`: AWS Lambda handler (uses Secrets Manager)
- `rca_notion_ops.py`: Notion API utilities (read blocks, create child page, append draft + link)
- `rca_generator.py`: OpenAI call (model `gpt-5.2`) that returns structured JSON
- `prompts/customer_rca_system_prompt.txt`: Externalized system prompt for the LLM
- `test_connection.py`: Sanity-check Notion access + fetch a sample page

### Prerequisites

- Python 3.10+
- A Notion **Internal Integration** connected to the RCA database
- `AI-Automations/token.json` must contain `NOTION_API_TOKEN` and `OPENAI_API_KEY`

### Install dependencies

```bash
python -m pip install -r requirements.txt
```

### Run locally

```bash
PORT=5055 python app.py
```

### Expose via ngrok (for local testing with Notion webhooks)

```bash
ngrok http 5055
```

Webhook URL: `https://<your-ngrok-domain>/notion-webhook`

---

## Notion Database Automation Setup

In the RCA database automation:

- **Trigger**: `Status` is set to `Awaiting QC`
- **Action**: `Send webhook`
  - **URL**: `https://py2h3cxzsc.execute-api.us-east-1.amazonaws.com/notion-webhook`

### Filters (currently active)

- Tags contains "khoros"
- Created time > Feb 20, 2026
