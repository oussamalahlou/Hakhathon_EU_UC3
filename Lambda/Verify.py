import boto3
import json
import os

# Clients
bedrock = boto3.client("bedrock-runtime", region_name=os.getenv("AWS_REGION"))
lambda_client = boto3.client("lambda")

# Better env var: set this in Lambda A configuration
# EXTRACT_FUNCTION_NAME = the name/arn of Lambda B
LAMBDA_B_NAME = os.environ.get("EXTRACT_FUNCTION_NAME", "exctract_function")

MODEL_ID = "mistral.mistral-large-2402-v1:0"

SYSTEM_PROMPT = """Tu es un classifieur.
Classe le texte ci-dessous dans UNE SEULE catégorie parmi :
- CONTRACTUALISATION
- RESILIATION
- RECLAMATION
- CHANGEMENT_OFFRE
- INFORMATION_TECHNIQUE

Contraintes :
- Réponds STRICTEMENT avec un objet JSON valide, sans commentaire, sans Markdown, sans texte additionnel.
- Clés attendues : category (string), confidence (float entre 0 et 1).
- category doit être exactement l'une des catégories listées.
- confidence représente ton degré de certitude.
"""

def lambda_handler(event, context):
# Read input
user_text = (event.get("text") or "").strip()
if not user_text:
return {"statusCode": 400, "error": "Provide 'text' in the event."}

# ---- 1) Call Bedrock (Converse) ----
messages = [
{
"role": "user",
"content": [{"text": f"Texte:\n{user_text}\n\nRenvoyer UNIQUEMENT le JSON."}]
}
]

body = {
"modelId": MODEL_ID,
"messages": messages,
"inferenceConfig": {
"temperature": 0.1,
"maxTokens": 800,
"topP": 0.9
},
"system": [{"text": SYSTEM_PROMPT}]
}

try:
response = bedrock.converse(**body)
except Exception as e:
return {"statusCode": 502, "error": "Bedrock call failed", "details": str(e)}

# Extract text from Bedrock response
content = response.get("output", {}).get("message", {}).get("content", [])
text_out = ""
if content and isinstance(content, list) and "text" in content[0]:
text_out = content[0]["text"].strip()

# Parse JSON from model output
try:
classification = json.loads(text_out)
except Exception:
return {
"statusCode": 422,
"error": "Model output wasn't valid JSON",
"model": MODEL_ID,
"raw": text_out
}

# ---- 2) Prepare payload for Lambda B ----
# Lambda B expects {"text": "..."}.
# We'll embed classification as JSON string appended to text (safe + simple).
combined_text = (
user_text
+ "\n\n---CLASSIFICATION---\n"
+ json.dumps(classification, ensure_ascii=False)
)

payload = {"text": combined_text}

# ---- 3) Invoke Lambda B synchronously ----
try:
invoke_resp = lambda_client.invoke(
FunctionName=LAMBDA_B_NAME,
InvocationType="RequestResponse",
Payload=json.dumps(payload).encode("utf-8")
)
except Exception as e:
return {"statusCode": 502, "error": "Lambda B invocation failed", "details": str(e)}

# ---- 4) Read Lambda B response payload ----
raw_payload = invoke_resp["Payload"].read().decode("utf-8") if "Payload" in invoke_resp else ""

# If Lambda B errored, AWS sets FunctionError
if "FunctionError" in invoke_resp:
# raw_payload is usually JSON containing errorMessage/errorType/stackTrace
return {
"statusCode": 500,
"error": "Lambda B returned an error",
"lambdaB_raw": raw_payload,
"bedrock_classification": classification
}

# Try parse Lambda B output as JSON; if not JSON, return as string
try:
lambda_b_result = json.loads(raw_payload) if raw_payload else None
except Exception:
lambda_b_result = raw_payload

# Final response: return Lambda B response (plus classification if you want)
return {
"statusCode": 200,
"model": MODEL_ID,
"classification": classification,
"lambdaB_response": lambda_b_result
}

 
