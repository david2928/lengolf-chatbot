import os
import json
from flask import Flask, request
from linebot.v3 import WebhookHandler
from linebot.v3.messaging import MessagingApi, Configuration as LineConfiguration, ApiClient
from linebot.v3.messaging.models import (
    TextMessage,
    TemplateMessage,
    ButtonsTemplate,
    DatetimePickerAction,
    ReplyMessageRequest,
    PushMessageRequest,
)
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent,
    PostbackEvent,
)
from linebot.v3.exceptions import InvalidSignatureError
import requests
from openai import OpenAI
from dotenv import load_dotenv
from datetime import datetime
from typing import Optional

# Load environment variables from .env file (optional)
load_dotenv()

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

# Initialize session storage (in-memory; consider persistent storage for scalability)
user_sessions = {}

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

# Helper function to prompt user for a date using LINE's date picker
def prompt_for_date(reply_token, user_id):
    # Define the date picker template
    date_picker_template = TemplateMessage(
        alt_text='Please select a date:',
        template=ButtonsTemplate(
            title='Select Date',
            text='Please choose a date to check availability:',
            actions=[
                DatetimePickerAction(
                    label='Pick a date',
                    data='action=pick_date',
                    mode='date'
                )
            ]
        )
    )

    # Send the date picker using LineBotApi
    reply_message_request = ReplyMessageRequest(
        reply_token=reply_token,
        messages=[date_picker_template]
    )
    line_bot_api.reply_message(reply_message_request)

    # Update the session to await date input
    user_sessions[user_id] = {'awaiting': 'date_input'}

# Helper function to handle date input from the date picker
def handle_date_input(user_id, date_str, reply_token):
    try:
        date = datetime.strptime(date_str, '%Y-%m-%d').date()
        # Now we proceed to handle the date as per the conversation
        # For simplicity, we'll just call the handle_message function again
        user_message = f"What is the availability on {date_str}?"
        # Simulate the message event
        event = type('Event', (object,), {
            'message': type('Message', (object,), {'text': user_message}),
            'reply_token': reply_token,
            'source': type('Source', (object,), {'user_id': user_id})
        })
        handle_message(event)
    except ValueError:
        # If the date format is incorrect, prompt again
        push_message_request = PushMessageRequest(
            to=user_id,
            messages=[TextMessage(text="Invalid date format. Please try again.")]
        )
        line_bot_api.push_message(push_message_request)
    finally:
        # Clear the session after handling
        user_sessions.pop(user_id, None)

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
        return 'Invalid signature', 400

    return 'OK'

# Message event handler
@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_message = event.message.text.strip()
    reply_token = event.reply_token
    user_id = event.source.user_id
    print(f"User ID: {user_id}, Message: {user_message}")  # Debugging

    # Check if the user is in a session awaiting date input
    if user_sessions.get(user_id, {}).get('awaiting') == 'date_input':
        handle_date_input(user_id, user_message, reply_token)
        return

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
                "Please be concise and focus on delivering the necessary information."
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
                    prompt_for_date(reply_token, user_id)
                    return
            else:
                prompt_for_date(reply_token, user_id)
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

# Postback event handler for date picker
@handler.add(PostbackEvent)
def handle_postback_event(event):
    data = event.postback.data
    params = event.postback.params  # Contains datetimepicker data

    if data == "action=pick_date" and params:
        date_str = params.get('date')  # Format: 'YYYY-MM-DD'
        user_id = event.source.user_id
        reply_token = event.reply_token

        if date_str:
            handle_date_input(user_id, date_str, reply_token)
        else:
            # If date is not provided, prompt again
            prompt_for_date(reply_token, user_id)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
