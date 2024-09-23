import os
import json
from flask import Flask, request
from linebot.v3 import WebhookHandler
from linebot.v3.messaging import MessagingApi, Configuration as LineConfiguration, ApiClient
from linebot.v3.messaging.models import (
    TextMessage,
    ReplyMessageRequest,
)
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent,
)
from linebot.v3.exceptions import InvalidSignatureError
import requests
from openai import OpenAI
from datetime import datetime
from typing import Optional

# Initialize Flask app
app = Flask(__name__)

# Retrieve environment variables
LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET')
GAS_WEB_APP_URL = os.environ.get('GAS_WEB_APP_URL')
OPENAI_API_KEY = os.environ.get('OPENAI_API_KEY')

# Initialize LINE Bot API and Webhook Handler
line_config = LineConfiguration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
api_client = ApiClient(configuration=line_config)
line_bot_api = MessagingApi(api_client=api_client)
handler = WebhookHandler(channel_secret=LINE_CHANNEL_SECRET)

# Initialize OpenAI API client
client = OpenAI(api_key=OPENAI_API_KEY)

# Define the functions for OpenAI function calling
functions = [
    {
        "name": "get_availability_today",
        "description": "Retrieve the availability for today.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False
        }
    },
    {
        "name": "get_availability_tomorrow",
        "description": "Retrieve the availability for tomorrow.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False
        }
    },
    {
        "name": "get_availability_specific",
        "description": "Retrieve the availability for a specific date.",
        "parameters": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "The date to check availability for, in YYYY-MM-DD format."
                }
            },
            "required": ["date"],
            "additionalProperties": False
        }
    }
]

# Function to call Google Apps Script for availability
def call_google_apps_script(command, date: Optional[datetime.date] = None):
    payload = {"events": [{"message": {"text": command}}]}

    if date:
        payload["events"][0]["message"]["date"] = date.strftime('%Y-%m-%d')

    response = requests.post(GAS_WEB_APP_URL, json=payload)
    if response.status_code == 200:
        try:
            return response.json()
        except json.JSONDecodeError:
            return {"error": "Invalid JSON response from Google Apps Script."}
    else:
        return {"error": f"Google Apps Script returned status code {response.status_code}"}

# Webhook callback endpoint
@app.route("/callback", methods=['POST'])
def callback():
    # Get request body and signature
    signature = request.headers.get('X-Line-Signature')
    body = request.get_data(as_text=True)
    print(f"Request body: {body}")  # Debugging

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        print("Invalid signature. Check your LINE_CHANNEL_SECRET.")
        return 'Invalid signature', 400
    except Exception as e:
        print(f"Exception in callback: {e}")
        return 'Internal Server Error', 500

    return 'OK'

# Message event handler
@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_message = event.message.text.strip()
    reply_token = event.reply_token
    user_id = event.source.user_id
    print(f"User ID: {user_id}, Message: {user_message}")  # Debugging

    # Get the current date
    current_date_str = datetime.now().strftime('%Y-%m-%d')

    # Define the conversation for GPT-4o-mini with current date and instructions
    messages = [
        {
            "role": "system",
            "content": (
                f"You are a helpful assistant that can check golf bay availability and answer other questions. "
                f"Today's date is {current_date_str}. "
                "When providing availability information, present it in plain text without any markdown or special formatting characters. "
                "Please be concise and focus on delivering the necessary information. "
                "When asking the user for a date, request it in YYYY-MM-DD format."
            )
        },
        {"role": "user", "content": user_message}
    ]

    # GPT-4o-mini response with function calling
    try:
        session = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            functions=functions,
            function_call="auto"
        )
        assistant_message = session.choices[0].message

    except Exception as e:
        print(f"OpenAI API error: {e}")  # Debugging
        reply_message_request = ReplyMessageRequest(
            reply_token=reply_token,
            messages=[TextMessage(text="Sorry, I encountered an error while processing your request.")]
        )
        line_bot_api.reply_message(reply_message_request)
        return

    if assistant_message.function_call:
        function_call = assistant_message.function_call
        function_name = function_call.name
        function_args = function_call.arguments or '{}'

        # Execute the function and get the result
        function_response = ""

        if function_name == "get_availability_today":
            # Execute the function and capture the output
            response_data = call_google_apps_script('availability_today')
        elif function_name == "get_availability_tomorrow":
            response_data = call_google_apps_script('availability_tomorrow')
        elif function_name == "get_availability_specific":
            args = json.loads(function_args)
            date_str = args.get("date")
            if date_str:
                try:
                    date_obj = datetime.strptime(date_str, '%Y-%m-%d').date()
                    response_data = call_google_apps_script('availability_specific', date_obj)
                except ValueError:
                    # Send a message asking for a valid date
                    reply_message_request = ReplyMessageRequest(
                        reply_token=reply_token,
                        messages=[TextMessage(text="Please provide a valid date in YYYY-MM-DD format.")]
                    )
                    line_bot_api.reply_message(reply_message_request)
                    return
            else:
                # Ask the user to provide a date
                reply_message_request = ReplyMessageRequest(
                    reply_token=reply_token,
                    messages=[TextMessage(text="Please specify the date you want to check availability for (YYYY-MM-DD).")]
                )
                line_bot_api.reply_message(reply_message_request)
                return
        else:
            reply_message_request = ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text="I'm sorry, I can't handle that request right now.")]
            )
            line_bot_api.reply_message(reply_message_request)
            return

        # Prepare the function response as a string
        if 'error' in response_data:
            function_response = f"Error: {response_data['error']}"
        else:
            function_response = json.dumps(response_data)

        # Append the function response to the messages
        messages.append({
            "role": "assistant",
            "content": None,
            "function_call": {
                "name": function_name,
                "arguments": function_args
            }
        })

        messages.append({
            "role": "function",
            "name": function_name,
            "content": function_response
        })

        # Call the model again to get the final answer
        try:
            second_session = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages
            )
            final_response = second_session.choices[0].message.content

            # Send the assistant's response to the user
            reply_message_request = ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=final_response)]
            )
            line_bot_api.reply_message(reply_message_request)
        except Exception as e:
            print(f"OpenAI API error during second call: {e}")
            reply_message_request = ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text="Sorry, I encountered an error while processing your request.")]
            )
            line_bot_api.reply_message(reply_message_request)

    else:
        # If no function call, reply with GPT's message
        reply_text = assistant_message.content or "Sorry, I didn't understand that."
        reply_message_request = ReplyMessageRequest(
            reply_token=reply_token,
            messages=[TextMessage(text=reply_text)]
        )
        line_bot_api.reply_message(reply_message_request)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
