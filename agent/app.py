from flask import Flask, request, jsonify
import requests
import os
import json

app = Flask(__name__)

SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")  

@app.route("/enrich", methods=["POST"])
def enrich():
    print("Enriching alert")
    
    # Get raw data from the request instead of forcing JSON parsing
    raw_data = request.get_data(as_text=True)
    
    data = {}
    try:
        # Try to parse the raw data as JSON
        data = json.loads(raw_data)
        print(f"Parsed JSON data: {data}")
    except json.JSONDecodeError:
        # If it's not JSON, treat it as a plain text description
        print(f"Received non-JSON data: {raw_data}")
        data = {
            "title": "Raw Text Alert",
            "description": raw_data
        }

    data["extra_info"] = "Agent processed this alert!"
    print("Alert enriched")
    
    msg = {
        "text": f"Enriched Alert:\n*Title:* {data.get('title')}\n*Description:* {data.get('description')}\n*Extra:* {data['extra_info']}"
    }
    print("Sending to slack")
    requests.post(SLACK_WEBHOOK_URL, json=msg)

    print("Done")
    return jsonify({"status": "ok", "forwarded_to_slack": True})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8081)